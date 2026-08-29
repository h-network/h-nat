"""NAT registration for h-memory bounded-buffer memory.

Exposes two discrete NAT function ``_type:``s:

  - ``_type: h_memory_write_turn`` — write one turn to the hot tier.
  - ``_type: h_memory_delete_chat`` — wipe one chat's hot-tier state.

Workflow YAML usage::

    workflow:
      _type: h_memory_write_turn
      redis_url: redis://localhost:6379
      pod: example-pod
      agent: example-agent
      ttl_seconds_max: 86400      # operator-set ceiling
      hot_keep_count: 50          # optional count cap

    # input (CLI ``nat run --input '{...}'`` or harness call):
    #   {"chat_id": "...", "role": "...", "content": "...", "ttl_seconds": <1..ttl_seconds_max>}

Keyspace shape per ``docs/adrs/ADR-012-redis-naming-contract.md``:
``<pod>:<agent>:chat:{chat_id}:{ts_ns}`` for turn data,
``<pod>:<agent>:chat-index:{chat_id}`` for the per-chat ZSET index.
"""
# NOTE: no `from __future__ import annotations` here. NAT 1.6.0's
# `FunctionInfo.from_fn` introspects the inner `_invoke` function via
# `typing.get_type_hints(...)` on an internally-generated stream wrapper;
# under PEP 563 deferred annotations, the resolution happens against NAT's
# own module globals (where our `WriteTurnInput` / `DeleteChatInput`
# classes are not defined), raising NameError. Eager (non-future)
# annotations resolve at definition time and avoid the issue.

import logging
from collections.abc import AsyncGenerator
from typing import Any, Callable, Optional

import redis.asyncio as aioredis
from pydantic import BaseModel, ConfigDict, Field

from .memory import BoundedBufferStore

logger = logging.getLogger(__name__)

# Try importing NAT primitives; provide functional stubs if nat is not installed
# in standalone or test environments so models and builders remain importable and testable.
try:
    from nat.builder.builder import Builder
    from nat.builder.function_info import FunctionInfo
    from nat.cli.register_workflow import register_function
    from nat.data_models.function import FunctionBaseConfig
except ImportError:
    class Builder:  # type: ignore[no-redef]
        pass

    class FunctionInfo:  # type: ignore[no-redef]
        def __init__(self, fn: Any, converters: Optional[list[Any]] = None, description: str = ""):
            self.fn = fn
            self.converters = converters or []
            self.description = description

        @classmethod
        def from_fn(cls, fn: Any, converters: Optional[list[Any]] = None, description: str = "") -> "FunctionInfo":
            return cls(fn, converters=converters, description=description)

    def register_function(config_type: Any) -> Callable[[Any], Any]:  # type: ignore[no-redef]
        def decorator(fn: Any) -> Any:
            fn._config_type = config_type
            return fn
        return decorator

    class FunctionBaseConfig(BaseModel):  # type: ignore[no-redef]
        def __init_subclass__(cls, name: Optional[str] = None, **kwargs: Any) -> None:
            super().__init_subclass__(**kwargs)
            cls._component_name = name


# Regex for the ``pod`` and ``agent`` fields. Per ADR-012, each is a single
# ``[A-Za-z0-9_-]+`` token; the ``<pod>:<agent>`` separation primitive is
# expressed as two distinct config fields rather than one combined-regex
# string. Operator-set per deployment; semantics consumer-defined.
_POD_AGENT_TOKEN_PATTERN = r"^[A-Za-z0-9_-]+$"


# ---------------------------------------------------------------------------
# Input models — strict (extra=forbid) so JSON typos surface at parse time.
# ---------------------------------------------------------------------------

class WriteTurnInput(BaseModel):
    """Per-call input for ``_type: h_memory_write_turn``.

    ``ttl_seconds`` is required per call; the operator-set ceiling
    lives on the function config (``HMemoryWriteTurnConfig.ttl_seconds_max``)
    and is enforced at function-call time inside the builder body
    (Pydantic catches values < 1 here; the upper bound depends on the
    config and so cannot live on the input model).

    ``hot_keep_count`` is an optional per-call override of the config-level
    count cap (round 70). When non-None, the post-write ``ZREMRANGEBYRANK``
    keeps the index trimmed to the most-recent N entries. When None, falls
    back to the config-level ``hot_keep_count``; if that's also None, no
    count cap is applied (backwards-compatible behavior).
    """
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    content: str
    ttl_seconds: int = Field(ge=1)
    hot_keep_count: Optional[int] = Field(default=None, ge=1)


class DeleteChatInput(BaseModel):
    """Per-call input for ``_type: h_memory_delete_chat``."""
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Type converters — let ``nat run --input '<JSON>'`` (which passes the raw
# string through to the workflow) resolve to our Pydantic input models.
# Without these, NAT's CLI front-end can't bridge str -> input model and
# raises an opaque conversion error mid-call.
# ---------------------------------------------------------------------------

def _json_str_to_write_turn_input(value: str) -> WriteTurnInput:
    return WriteTurnInput.model_validate_json(value)


def _json_str_to_delete_chat_input(value: str) -> DeleteChatInput:
    return DeleteChatInput.model_validate_json(value)


def _int_to_str(value: int) -> str:
    """``runner.result(to_type=str)`` (used by ``nat run``'s console front-end)
    coerces every workflow output to str. ``h_memory_delete_chat`` returns
    int (count of keys deleted); register the converter so the CLI display
    works without changing the function's return shape."""
    return str(value)


# ---------------------------------------------------------------------------
# h_memory_write_turn — write one turn (the load-bearing primitive)
# ---------------------------------------------------------------------------

class HMemoryWriteTurnConfig(FunctionBaseConfig, name="h_memory_write_turn"):
    """Workflow-config for ``_type: h_memory_write_turn``.

    Per-workflow operator settings: where Redis is, the
    ``<pod>:<agent>`` multi-tenancy primitive (per ADR-012) under
    which keys are written, and the operator-set ceiling on
    ``ttl_seconds`` that any single call may request.
    """

    redis_url: str = Field(
        default="redis://localhost:6379",
        description="Redis URL. Defaults to localhost.",
    )
    pod: str = Field(
        ...,
        pattern=_POD_AGENT_TOKEN_PATTERN,
        description=(
            "ADR-012 first primitive segment of the multi-tenancy "
            "primitive ``<pod>:<agent>``. Required; no default. "
            "Operator-set per deployment; semantics consumer-defined."
        ),
    )
    agent: str = Field(
        ...,
        pattern=_POD_AGENT_TOKEN_PATTERN,
        description=(
            "ADR-012 second primitive segment of ``<pod>:<agent>``. "
            "Required; no default. Operator-set per deployment; "
            "semantics consumer-defined."
        ),
    )
    ttl_seconds_max: int = Field(
        default=2592000,  # 30 days
        ge=1,
        description=(
            "Operator-set ceiling on per-call ttl_seconds. Default 30 "
            "days (2592000s). Per-call ttl_seconds is validated against "
            "[1, ttl_seconds_max] inside the function body. The ZSET "
            "index TTL is refreshed to this value on every write so the "
            "index outlives the longest possible turn."
        ),
    )
    hot_keep_count: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Optional count-based bound on the per-chat ZSET index. "
            "When set (>=1), the write pipeline trims the index to the "
            "most-recent N entries via ZREMRANGEBYRANK after each "
            "write. When None (default), no count cap is applied — the "
            "index is bounded only by the conservative "
            "ZREMRANGEBYSCORE cleanup against ``ttl_seconds_max``. "
            "Round-70 feature; backwards-compatible default. Per-call "
            "override available on ``WriteTurnInput.hot_keep_count``."
        ),
    )


@register_function(config_type=HMemoryWriteTurnConfig)
async def h_memory_write_turn(
    config: HMemoryWriteTurnConfig,
    builder: Optional[Builder] = None,
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the ``h_memory_write_turn`` NAT function.

    Lifecycle:

      1. Open a redis async client; eager ``ping`` so misconfiguration
         fails at workflow build, not mid-call.
      2. Construct a :class:`BoundedBufferStore` against the operator's
         ``pod`` / ``agent`` (ADR-012 primitive) and ``ttl_seconds_max``.
      3. Emit the load-bearing connect log line (substrings pinned for
         acceptance verifies).
      4. Yield the inner ``_invoke(input: WriteTurnInput) -> str``
         that validates the per-call ``ttl_seconds`` against the
         config ceiling, writes the turn, and returns the full turn
         key.
      5. ``aclose`` the redis client on teardown.
    """
    client = aioredis.Redis.from_url(config.redis_url, decode_responses=True)
    try:
        await client.ping()
        store = BoundedBufferStore(
            client=client,
            pod=config.pod,
            agent=config.agent,
            ttl_seconds_max=config.ttl_seconds_max,
        )
        logger.info(
            "h_memory_write_turn connected: redis_url=%s pod=%s agent=%s "
            "ttl_seconds_max=%d hot_keep_count=%s",
            config.redis_url,
            config.pod,
            config.agent,
            config.ttl_seconds_max,
            config.hot_keep_count,
        )

        async def _invoke(input: WriteTurnInput) -> str:
            if input.ttl_seconds > config.ttl_seconds_max:
                raise ValueError(
                    f"ttl_seconds={input.ttl_seconds} out of range "
                    f"[1, {config.ttl_seconds_max}]"
                )
            # Per-call override wins; otherwise fall back to config; if
            # neither set, None is passed and the store applies no cap.
            effective_hot_keep_count = (
                input.hot_keep_count
                if input.hot_keep_count is not None
                else config.hot_keep_count
            )
            return await store.write_turn(
                chat_id=input.chat_id,
                role=input.role,
                content=input.content,
                ttl_seconds=input.ttl_seconds,
                hot_keep_count=effective_hot_keep_count,
            )

        yield FunctionInfo.from_fn(
            _invoke,
            converters=[_json_str_to_write_turn_input],
            description=(
                "Write one turn to the h-network-memory hot tier. "
                "Per-call ttl_seconds is validated against the "
                "operator-set ttl_seconds_max ceiling. Returns the full "
                "Redis turn key written. Keyspace shape per ADR-012: "
                "<pod>:<agent>:chat:{chat_id}:{ts_ns}."
            ),
        )
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# h_memory_delete_chat — wipe one chat's hot-tier state
# ---------------------------------------------------------------------------

class HMemoryDeleteChatConfig(FunctionBaseConfig, name="h_memory_delete_chat"):
    """Workflow-config for ``_type: h_memory_delete_chat``.

    Per-workflow operator settings: where Redis is and the
    ``<pod>:<agent>`` multi-tenancy primitive (per ADR-012) within
    which to operate. No ``ttl_seconds_max`` (deletion does not write
    keys; the TTL ceiling is irrelevant).
    """

    redis_url: str = Field(
        default="redis://localhost:6379",
        description="Redis URL. Defaults to localhost.",
    )
    pod: str = Field(
        ...,
        pattern=_POD_AGENT_TOKEN_PATTERN,
        description=(
            "ADR-012 first primitive segment of ``<pod>:<agent>``. "
            "Required; no default."
        ),
    )
    agent: str = Field(
        ...,
        pattern=_POD_AGENT_TOKEN_PATTERN,
        description=(
            "ADR-012 second primitive segment of ``<pod>:<agent>``. "
            "Required; no default."
        ),
    )


@register_function(config_type=HMemoryDeleteChatConfig)
async def h_memory_delete_chat(
    config: HMemoryDeleteChatConfig,
    builder: Optional[Builder] = None,
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the ``h_memory_delete_chat`` NAT function.

    Lifecycle mirrors ``h_memory_write_turn`` (eager ping, pinned
    connect log line, store construction, ``aclose`` on teardown).
    The store is reused via its ``delete_chat`` method.

    Note: the store's constructor requires ``ttl_seconds_max`` even
    though delete doesn't use it. We pass a sentinel (1) since the
    store never writes during delete; this avoids polluting the
    delete config with a TTL knob that has no semantic meaning.
    """
    client = aioredis.Redis.from_url(config.redis_url, decode_responses=True)
    try:
        await client.ping()
        store = BoundedBufferStore(
            client=client,
            pod=config.pod,
            agent=config.agent,
            ttl_seconds_max=1,  # unused on the delete path; sentinel
        )
        logger.info(
            "h_memory_delete_chat connected: redis_url=%s pod=%s agent=%s",
            config.redis_url,
            config.pod,
            config.agent,
        )

        async def _invoke(input: DeleteChatInput) -> int:
            return await store.delete_chat(input.chat_id)

        yield FunctionInfo.from_fn(
            _invoke,
            converters=[_json_str_to_delete_chat_input, _int_to_str],
            description=(
                "Wipe one chat's h-network-memory hot-tier state. "
                "Returns count of Redis keys deleted (still-live data "
                "keys + the per-chat ZSET index). Already-expired keys "
                "are already gone via Redis TTL; the count reflects "
                "what was actually still resident."
            ),
        )
    finally:
        await client.aclose()
