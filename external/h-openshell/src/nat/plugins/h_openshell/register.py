"""NeMo Agent Toolkit registrations for h-openshell.

This module intentionally uses eagerly evaluated annotations. NAT inspects
streaming return annotations while building type converters, and concrete
``AsyncGenerator`` objects remain resolvable across that boundary.
"""

import json
from collections.abc import AsyncGenerator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig
from pydantic import Field

from .client import OpenShellClient


class OpenShellConnectionConfig(FunctionBaseConfig):
    """Connection fields shared by every h-openshell function."""

    gateway_home: str | None = Field(
        default=None,
        description=(
            "OpenShell configuration home containing active_gateway and "
            "gateways/. Defaults to OPENSHELL_HOME or ~/.config/openshell."
        ),
    )
    endpoint: str | None = Field(
        default=None,
        description="Optional gateway host:port override for remote access.",
    )
    target_override: str = Field(
        default="localhost",
        description="TLS certificate server name; verification remains enabled.",
    )


def _build_client(config: OpenShellConnectionConfig) -> OpenShellClient:
    return OpenShellClient.from_default_home(
        Path(config.gateway_home) if config.gateway_home else None,
        endpoint_override=config.endpoint,
        target_override=config.target_override,
    )


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class OpenShellHealthConfig(OpenShellConnectionConfig, name="h_openshell_health"):
    """Probe the configured OpenShell gateway and return status JSON."""

    rpc_timeout_seconds: float = Field(default=5.0, gt=0)


@register_function(config_type=OpenShellHealthConfig)
async def h_openshell_health(config: OpenShellHealthConfig, builder: Builder):
    client = _build_client(config)
    try:
        async def _health(input: str = "") -> str:
            del input
            status, version = await client.health(timeout=config.rpc_timeout_seconds)
            return _compact_json(
                {"endpoint": client.endpoint, "status": status, "version": version}
            )

        yield FunctionInfo.from_fn(
            _health,
            description="Return OpenShell gateway endpoint, status, and version as JSON.",
        )
    finally:
        await client.close()


class OpenShellListSandboxesConfig(
    OpenShellConnectionConfig, name="h_openshell_list_sandboxes"
):
    """List one offset-based page of OpenShell sandboxes as JSON."""

    limit: int = Field(default=100, ge=1)
    offset: int = Field(default=0, ge=0)
    rpc_timeout_seconds: float = Field(default=5.0, gt=0)


@register_function(config_type=OpenShellListSandboxesConfig)
async def h_openshell_list_sandboxes(
    config: OpenShellListSandboxesConfig, builder: Builder
):
    client = _build_client(config)
    try:
        async def _list(input: str = "") -> str:
            del input
            sandboxes = await client.list_sandboxes(
                limit=config.limit,
                offset=config.offset,
                timeout=config.rpc_timeout_seconds,
            )
            return _compact_json([asdict(sandbox) for sandbox in sandboxes])

        yield FunctionInfo.from_fn(
            _list,
            description="List OpenShell sandboxes as a deterministic JSON array.",
        )
    finally:
        await client.close()


class OpenShellGetSandboxConfig(
    OpenShellConnectionConfig, name="h_openshell_get_sandbox"
):
    """Fetch an OpenShell sandbox by name or UUID and return JSON."""

    rpc_timeout_seconds: float = Field(default=5.0, gt=0)


@register_function(config_type=OpenShellGetSandboxConfig)
async def h_openshell_get_sandbox(
    config: OpenShellGetSandboxConfig, builder: Builder
):
    client = _build_client(config)
    try:
        async def _get(name_or_id: str) -> str:
            name = await client._resolve_name(name_or_id)
            sandbox = await client.get_sandbox(
                name, timeout=config.rpc_timeout_seconds
            )
            return _compact_json(asdict(sandbox))

        yield FunctionInfo.from_fn(
            _get,
            description="Get an OpenShell sandbox by name or UUID as JSON.",
        )
    finally:
        await client.close()


class OpenShellCreateSandboxConfig(
    OpenShellConnectionConfig, name="h_openshell_create_sandbox"
):
    """Create or reconcile a named sandbox, waiting until it is ready."""

    ready_timeout_seconds: float = Field(default=120.0, gt=0)
    poll_interval_seconds: float = Field(default=2.0, ge=0)
    rpc_timeout_seconds: float = Field(default=30.0, gt=0)


@register_function(config_type=OpenShellCreateSandboxConfig)
async def h_openshell_create_sandbox(
    config: OpenShellCreateSandboxConfig, builder: Builder
):
    client = _build_client(config)
    try:
        async def _create(name: str) -> str:
            sandbox = await client.create_sandbox(
                name,
                timeout_seconds=config.ready_timeout_seconds,
                poll_interval_seconds=config.poll_interval_seconds,
                rpc_timeout=config.rpc_timeout_seconds,
            )
            return _compact_json(asdict(sandbox))

        yield FunctionInfo.from_fn(
            _create,
            description="Create or reconcile an OpenShell sandbox and return JSON.",
        )
    finally:
        await client.close()


class OpenShellDeleteSandboxConfig(
    OpenShellConnectionConfig, name="h_openshell_delete_sandbox"
):
    """Delete a sandbox by name or UUID, optionally confirming absence."""

    wait: bool = Field(default=True)
    delete_timeout_seconds: float = Field(default=30.0, gt=0)
    poll_interval_seconds: float = Field(default=1.0, ge=0)
    rpc_timeout_seconds: float = Field(default=30.0, gt=0)


@register_function(config_type=OpenShellDeleteSandboxConfig)
async def h_openshell_delete_sandbox(
    config: OpenShellDeleteSandboxConfig, builder: Builder
):
    client = _build_client(config)
    try:
        async def _delete(name_or_id: str) -> str:
            deleted = await client.delete_sandbox(
                name_or_id,
                wait=config.wait,
                timeout_seconds=config.delete_timeout_seconds,
                poll_interval_seconds=config.poll_interval_seconds,
                rpc_timeout=config.rpc_timeout_seconds,
            )
            return _compact_json({"deleted": deleted, "name_or_id": name_or_id})

        yield FunctionInfo.from_fn(
            _delete,
            description="Delete an OpenShell sandbox by name or UUID and return JSON.",
        )
    finally:
        await client.close()


class OpenShellExecConfig(OpenShellConnectionConfig, name="h_openshell_exec"):
    """Run a shell command in a sandbox and return result JSON."""

    sandbox: str = Field(description="Sandbox name or UUID.")
    rpc_timeout_seconds: float = Field(default=600.0, gt=0)
    command_timeout_seconds: int = Field(default=0, ge=0)


@register_function(config_type=OpenShellExecConfig)
async def h_openshell_exec(config: OpenShellExecConfig, builder: Builder):
    client = _build_client(config)
    try:
        async def _exec(command: str) -> str:
            result = await client.exec(
                config.sandbox,
                ["bash", "-c", command],
                timeout_seconds=config.command_timeout_seconds,
                rpc_timeout=config.rpc_timeout_seconds,
            )
            return _compact_json(
                {
                    "exit_code": result.exit_code,
                    "stderr": result.stderr_text,
                    "stdout": result.stdout_text,
                }
            )

        yield FunctionInfo.from_fn(
            _exec,
            description=(
                "Run a shell command in an OpenShell sandbox and return "
                "exit_code/stdout/stderr JSON."
            ),
        )
    finally:
        await client.close()


class OpenShellExecStreamConfig(
    OpenShellConnectionConfig, name="h_openshell_exec_stream"
):
    """Stream shell stdout, stderr, and exit as newline-delimited JSON."""

    sandbox: str = Field(description="Sandbox name or UUID.")
    rpc_timeout_seconds: float = Field(default=600.0, gt=0)
    command_timeout_seconds: int = Field(default=0, ge=0)


@register_function(config_type=OpenShellExecStreamConfig)
async def h_openshell_exec_stream(
    config: OpenShellExecStreamConfig, builder: Builder
):
    client = _build_client(config)
    try:
        async def _exec_stream(command: str) -> AsyncGenerator[str, None]:
            async for event in client.exec_stream(
                config.sandbox,
                ["bash", "-c", command],
                timeout_seconds=config.command_timeout_seconds,
                rpc_timeout=config.rpc_timeout_seconds,
            ):
                payload = event.WhichOneof("payload")
                if payload == "stdout":
                    value = {
                        "data": event.stdout.data.decode(errors="replace"),
                        "type": "stdout",
                    }
                elif payload == "stderr":
                    value = {
                        "data": event.stderr.data.decode(errors="replace"),
                        "type": "stderr",
                    }
                elif payload == "exit":
                    value = {"exit_code": int(event.exit.exit_code), "type": "exit"}
                else:
                    continue
                yield _compact_json(value) + "\n"

        yield FunctionInfo.from_fn(
            _exec_stream,
            description=(
                "Run a shell command and stream newline-delimited JSON "
                "stdout, stderr, and exit events."
            ),
        )
    finally:
        await client.close()
