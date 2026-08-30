"""Dispatcher-agnostic, Redis-backed chat cycle."""

import asyncio
import json
import logging
import time
from typing import Any

import redis.asyncio as aioredis
from nat.plugin_api import (
    Builder,
    FunctionBaseConfig,
    FunctionInfo,
    FunctionRef,
    register_function,
)
from nat.plugins.h_memory import BoundedBufferStore
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"


class HChatCycleConfig(FunctionBaseConfig, name="h_chat_cycle"):
    model_config = ConfigDict(extra="forbid")

    dispatcher: FunctionRef
    chat_id: str | None = Field(default=None, min_length=1)
    pod: str | None = Field(default=None, pattern=_TOKEN_PATTERN)
    agent: str | None = Field(default=None, pattern=_TOKEN_PATTERN)
    redis_url: str = "redis://localhost:6379"
    hot_keep_count: int = Field(default=20, ge=1)
    ttl_seconds: int = Field(default=86_400, ge=1)


class HChatCycleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    chat_id: str | None = Field(default=None, min_length=1)
    pod: str | None = Field(default=None, pattern=_TOKEN_PATTERN)
    agent: str | None = Field(default=None, pattern=_TOKEN_PATTERN)
    metadata: dict[str, Any] | None = None


class HChatCycleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: str
    chat_id: str
    prior_turn_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    turn_id: str


def string_to_chat_input(value: str) -> HChatCycleInput:
    """Interpret JSON-object strings as typed requests, otherwise as messages."""

    if value.strip().startswith("{"):
        return HChatCycleInput.model_validate_json(value)
    return HChatCycleInput(message=value)


def chat_output_to_string(value: HChatCycleOutput) -> str:
    return value.result


def resolve_addressing(
    request: HChatCycleInput, config: HChatCycleConfig
) -> tuple[str, str, str]:
    chat_id = request.chat_id or config.chat_id
    pod = request.pod or config.pod
    agent = request.agent or config.agent
    values = {"chat_id": chat_id, "pod": pod, "agent": agent}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(
            "h_chat_cycle missing required addressing fields: "
            + ", ".join(missing)
        )
    assert chat_id is not None and pod is not None and agent is not None
    return chat_id, pod, agent


async def read_prior_turns(
    client: Any, pod: str, agent: str, chat_id: str
) -> list[dict[str, Any]]:
    """Read live indexed turns oldest-first from the h-memory keyspace."""

    keys = await client.zrevrange(f"{pod}:{agent}:chat-index:{chat_id}", 0, -1)
    if not keys:
        return []
    payloads = await client.mget(*keys)
    turns: list[dict[str, Any]] = []
    for payload in reversed(payloads):
        if not payload:
            continue
        try:
            turn = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(turn, dict):
            turns.append(turn)
    return turns


def build_chat_prompt(turns: list[dict[str, Any]], message: str) -> str:
    if not turns:
        return message
    history = "\n".join(
        f"[{turn.get('role', '?')}] {turn.get('content', '')}" for turn in turns
    )
    return f"Previous conversation:\n{history}\n\nCurrent message:\n{message}\n"


@register_function(config_type=HChatCycleConfig)
async def h_chat_cycle(config: HChatCycleConfig, builder: Builder):
    """Build the lazy memory-read, dispatch, memory-write composite."""

    redis_client: Any | None = None
    dispatcher: Any | None = None
    state_lock = asyncio.Lock()
    try:
        logger.info(
            "h_chat_cycle built (lazy): dispatcher=%s redis_url=%s",
            config.dispatcher,
            config.redis_url,
        )

        async def invoke(request: HChatCycleInput) -> HChatCycleOutput:
            nonlocal redis_client, dispatcher
            chat_id, pod, agent = resolve_addressing(request, config)
            if redis_client is None or dispatcher is None:
                async with state_lock:
                    if redis_client is None:
                        redis_client = aioredis.Redis.from_url(
                            config.redis_url, decode_responses=True
                        )
                    if dispatcher is None:
                        dispatcher = await builder.get_function(config.dispatcher)

            prior = await read_prior_turns(redis_client, pod, agent, chat_id)
            prompt = build_chat_prompt(prior, request.message)
            started = time.perf_counter()
            try:
                reply = await dispatcher.ainvoke(prompt, to_type=str)
            except Exception as exc:
                logger.warning("h_chat_cycle dispatcher failed", exc_info=True)
                return HChatCycleOutput(
                    result=(
                        "[h_chat_cycle dispatcher error: "
                        f"{type(exc).__name__}: {exc}]"
                    ),
                    chat_id=chat_id,
                    prior_turn_count=len(prior),
                    duration_ms=0,
                    turn_id="",
                )
            duration_ms = int((time.perf_counter() - started) * 1000)
            reply = str(reply)
            store = BoundedBufferStore(
                client=redis_client,
                pod=pod,
                agent=agent,
                ttl_seconds_max=config.ttl_seconds,
            )
            await store.write_turn(
                chat_id,
                "user",
                request.message,
                config.ttl_seconds,
                hot_keep_count=config.hot_keep_count,
            )
            turn_id = await store.write_turn(
                chat_id,
                "assistant",
                reply,
                config.ttl_seconds,
                hot_keep_count=config.hot_keep_count,
            )
            return HChatCycleOutput(
                result=reply,
                chat_id=chat_id,
                prior_turn_count=len(prior),
                duration_ms=duration_ms,
                turn_id=turn_id,
            )

        yield FunctionInfo.from_fn(
            invoke,
            converters=[string_to_chat_input, chat_output_to_string],
            description="Read bounded history, dispatch a prompt, and persist the turn",
        )
    finally:
        if redis_client is not None:
            await redis_client.aclose()
