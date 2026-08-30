"""Claude stream-json NAT function."""

import asyncio
import codecs
import json
import logging
from typing import Any

from nat.plugin_api import Builder, FunctionInfo, register_function
from pydantic import Field, model_validator

from .core import build_script, with_context
from .register import AgentInvokeConfig, _client

logger = logging.getLogger(__name__)

_CLAUDE_STREAM_ARGS = (
    "-p",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--tools",
    "Bash",
    "--permission-mode",
    "bypassPermissions",
    "--output-format",
    "stream-json",
    "--verbose",
)


class ClaudeStreamConfig(AgentInvokeConfig, name="claude_stream"):
    """Claude specialization that consumes stream-json incrementally."""

    command: str = "claude"
    args: list[str] = Field(default_factory=lambda: list(_CLAUDE_STREAM_ARGS))
    output_parser: str = "raw"
    hook_settings_path: str | None = None

    @model_validator(mode="after")
    def add_hook_settings(self) -> "ClaudeStreamConfig":
        if self.hook_settings_path is not None:
            self.args = [*self.args, "--settings", self.hook_settings_path]
        return self


async def consume_claude_stream(
    client: Any,
    config: ClaudeStreamConfig,
    prompt: str,
) -> str:
    """Consume arbitrarily chunked UTF-8 JSON lines and return final text."""

    script = build_script(
        command=config.command,
        args=list(config.args),
        prompt=prompt,
        prompt_via=config.prompt_via,
    )
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text_buffer = ""
    final_text = ""
    stream_error = ""
    exit_code: int | None = None

    async for event in client.exec_stream(
        config.sandbox,
        ["bash"],
        stdin=script,
        rpc_timeout=config.rpc_timeout_seconds,
    ):
        kind = event.WhichOneof("payload")
        if kind == "stdout":
            text_buffer += decoder.decode(event.stdout.data)
            complete_lines, text_buffer = _take_complete_lines(text_buffer)
            for line in complete_lines:
                final_text, stream_error = _apply_event(
                    line, final_text, stream_error
                )
        elif kind == "stderr":
            stderr = event.stderr.data.decode(errors="replace")[:300]
            logger.warning("claude_stream stderr: %s", stderr)
        elif kind == "exit":
            exit_code = int(event.exit.exit_code)

    text_buffer += decoder.decode(b"", final=True)
    if text_buffer.strip():
        final_text, stream_error = _apply_event(
            text_buffer, final_text, stream_error
        )
    if exit_code is None:
        return "[claude_stream exit_code=missing]"
    if exit_code != 0:
        return f"[claude_stream exit_code={exit_code}]"
    if stream_error:
        return f"[claude_stream error: {stream_error}]"
    if not final_text:
        return "[claude_stream missing result]"
    return final_text


def _take_complete_lines(buffer: str) -> tuple[list[str], str]:
    parts = buffer.splitlines(keepends=True)
    if not parts or not parts[-1].endswith(("\n", "\r")):
        remainder = parts.pop() if parts else buffer
    else:
        remainder = ""
    return [part.strip() for part in parts if part.strip()], remainder


def _apply_event(
    line: str, final_text: str, stream_error: str
) -> tuple[str, str]:
    try:
        event: Any = json.loads(line)
    except json.JSONDecodeError:
        return final_text, stream_error
    if not isinstance(event, dict) or event.get("type") != "result":
        return final_text, stream_error
    if event.get("is_error"):
        error = event.get("error") or event.get("result") or "unknown error"
        return final_text, str(error)[:500]
    return str(event.get("result") or final_text), stream_error


@register_function(config_type=ClaudeStreamConfig)
async def claude_stream(config: ClaudeStreamConfig, builder: Builder):
    """Build a lazy Claude stream-json consumer."""

    client: Any | None = None
    client_lock = asyncio.Lock()
    try:
        logger.info("claude_stream built (lazy): sandbox=%s", config.sandbox)

        async def invoke(prompt: str) -> str:
            nonlocal client
            if client is None:
                async with client_lock:
                    if client is None:
                        client = _client(config)
            full_prompt = (
                with_context(config.context, prompt) if config.context else prompt
            )
            return await consume_claude_stream(client, config, full_prompt)

        yield FunctionInfo.from_fn(
            invoke,
            description="Consume Claude stream-json and return final assistant text",
        )
    finally:
        if client is not None:
            await client.close()
