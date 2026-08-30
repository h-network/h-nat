"""NAT registrations for stateless agent invocation."""

# Do not enable postponed annotations here. NAT inspects the concrete
# AsyncGenerator return annotation on the nested streaming function.

import asyncio
import logging
from pathlib import Path
from typing import AsyncGenerator

from pydantic import ConfigDict, Field, model_validator

from nat.plugin_api import Builder, FunctionBaseConfig, FunctionInfo, register_function
from nat.plugins.h_openshell import OpenShellClient

from .core import build_script, with_context
from .parsers import get_parser

logger = logging.getLogger(__name__)

_PROMPT_VIA_PATTERN = r"^(arg|stdin|env:[A-Za-z_][A-Za-z0-9_]*)$"
_CLAUDE_JSON_ARGS = (
    "-p",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--tools",
    "Bash",
    "--permission-mode",
    "bypassPermissions",
    "--output-format",
    "json",
)


class AgentInvokeConfig(FunctionBaseConfig, name="h_agent_invoke"):
    model_config = ConfigDict(extra="forbid")

    gateway_home: str | None = None
    endpoint: str | None = None
    target_override: str = "localhost"
    sandbox: str = Field(min_length=1)
    rpc_timeout_seconds: float = Field(default=600.0, gt=0)
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    prompt_via: str = Field(default="arg", pattern=_PROMPT_VIA_PATTERN)
    context: str | None = None
    output_parser: str = Field(default="raw", min_length=1)


class AgentStreamConfig(AgentInvokeConfig, name="h_agent_stream"):
    pass


class ClaudeInvokeConfig(AgentInvokeConfig, name="claude_invoke"):
    command: str = "claude"
    args: list[str] = Field(default_factory=lambda: list(_CLAUDE_JSON_ARGS))
    output_parser: str = "claude_json"
    hook_settings_path: str | None = None

    @model_validator(mode="after")
    def add_hook_settings(self) -> "ClaudeInvokeConfig":
        if self.hook_settings_path is not None:
            self.args = [*self.args, "--settings", self.hook_settings_path]
        return self


def _client(config: AgentInvokeConfig) -> OpenShellClient:
    return OpenShellClient.from_default_home(
        gateway_home=Path(config.gateway_home) if config.gateway_home else None,
        endpoint_override=config.endpoint,
        target_override=config.target_override,
    )


async def _invoke_builder(config: AgentInvokeConfig, name: str):
    parser = get_parser(config.output_parser)
    client: OpenShellClient | None = None
    client_lock = asyncio.Lock()
    try:
        logger.info(
            "%s built (lazy): command=%s sandbox=%s parser=%s",
            name,
            config.command,
            config.sandbox,
            config.output_parser,
        )

        async def invoke(prompt: str) -> str:
            nonlocal client
            if client is None:
                async with client_lock:
                    if client is None:
                        client = _client(config)
            full_prompt = with_context(config.context, prompt) if config.context else prompt
            result = await parser.consume(
                client,
                config.sandbox,
                build_script(
                    command=config.command,
                    args=list(config.args),
                    prompt=full_prompt,
                    prompt_via=config.prompt_via,
                ),
                config.rpc_timeout_seconds,
                None,
            )
            if not result.ok:
                logger.warning("%s invocation failed: %s", name, result.error_message)
                return result.error_message
            return result.text

        yield FunctionInfo.from_fn(invoke, description=f"Invoke {config.command!r} in OpenShell")
    finally:
        if client is not None:
            await client.close()


async def _stream_builder(config: AgentStreamConfig, name: str):
    client: OpenShellClient | None = None
    client_lock = asyncio.Lock()
    try:
        logger.info("%s built (lazy): command=%s sandbox=%s", name, config.command, config.sandbox)

        async def stream(prompt: str) -> AsyncGenerator[str, None]:
            nonlocal client
            if client is None:
                async with client_lock:
                    if client is None:
                        client = _client(config)
            full_prompt = with_context(config.context, prompt) if config.context else prompt
            script = build_script(
                command=config.command,
                args=list(config.args),
                prompt=full_prompt,
                prompt_via=config.prompt_via,
            )
            exit_code: int | None = None
            async for event in client.exec_stream(
                config.sandbox,
                ["bash"],
                stdin=script,
                rpc_timeout=config.rpc_timeout_seconds,
            ):
                kind = event.WhichOneof("payload")
                if kind == "stdout":
                    yield event.stdout.data.decode(errors="replace")
                elif kind == "stderr":
                    stderr = event.stderr.data.decode(errors="replace")[:300]
                    logger.warning("%s stderr: %s", name, stderr)
                elif kind == "exit":
                    exit_code = event.exit.exit_code
            if exit_code != 0:
                yield f"[exit_code={exit_code if exit_code is not None else 'missing'}]"

        yield FunctionInfo.from_fn(
            stream, description=f"Stream {config.command!r} stdout from OpenShell"
        )
    finally:
        if client is not None:
            await client.close()


@register_function(config_type=AgentInvokeConfig)
async def h_agent_invoke(config: AgentInvokeConfig, builder: Builder):
    async for function in _invoke_builder(config, "h_agent_invoke"):
        yield function


@register_function(config_type=AgentStreamConfig)
async def h_agent_stream(config: AgentStreamConfig, builder: Builder):
    async for function in _stream_builder(config, "h_agent_stream"):
        yield function


@register_function(config_type=ClaudeInvokeConfig)
async def claude_invoke(config: ClaudeInvokeConfig, builder: Builder):
    async for function in _invoke_builder(config, "claude_invoke"):
        yield function


# Import additional registration modules after the shared config and client
# factory are defined. Their decorators execute when NAT loads this component.
from . import chat_cycle, claude_stream, gated_mcp, ssh_exec  # noqa: E402, F401
