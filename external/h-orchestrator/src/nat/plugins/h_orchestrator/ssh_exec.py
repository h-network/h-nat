"""Direct, safety-gated SSH command execution."""

# Do not postpone annotations. NAT resolves the nested function's local model
# annotations while constructing FunctionInfo.

import json
import logging
from pathlib import Path
from typing import Literal

import asyncssh
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from nat.plugin_api import Builder, FunctionBaseConfig, FunctionInfo, FunctionRef, register_function

logger = logging.getLogger(__name__)


class SshExecConfig(FunctionBaseConfig, name="h_ssh_exec"):
    """Deployment-only connection and authorization configuration."""

    model_config = ConfigDict(extra="forbid")

    gate_fn: FunctionRef = Field(description="Configured h_asimov_gate instance to consult.")
    username: str = Field(min_length=1)
    password: SecretStr | None = None
    client_key: str | None = Field(default=None, min_length=1)
    client_key_passphrase: SecretStr | None = None
    port: int = Field(default=22, ge=1, le=65535)
    known_hosts: str = Field(default="~/.ssh/known_hosts", min_length=1)
    verify_host_key: bool = True
    connect_timeout_seconds: float = Field(default=15.0, gt=0)
    command_timeout_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def validate_credentials(self) -> "SshExecConfig":
        if self.password is None and self.client_key is None:
            raise ValueError("one of password or client_key is required")
        if self.client_key_passphrase is not None and self.client_key is None:
            raise ValueError("client_key_passphrase requires client_key")
        return self


class SshExecRequest(BaseModel):
    """Agent-visible request. Credentials are deliberately absent."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, description="Target SSH hostname or IP address.")
    command: str = Field(min_length=1, description="Exact command to execute on the target.")


class SshExecResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "denied", "error"]
    output: str = ""
    stderr: str = ""
    exit_status: int | None = None
    reason: str | None = None
    gate_layer: str | None = None


def _string_to_ssh_request(value: str) -> SshExecRequest:
    """Accept a JSON object from string-oriented NAT front ends."""

    return SshExecRequest.model_validate_json(value)


def _ssh_response_to_str(response: SshExecResponse) -> str:
    """Serialize typed results for string-oriented NAT front ends."""

    return response.model_dump_json()


def _gate_subject(host: str, port: int, command: str) -> str:
    """Build the single immutable representation presented to the gate."""

    return json.dumps(
        {"action": "ssh_exec", "host": host, "port": port, "command": command},
        ensure_ascii=False,
        separators=(",", ":"),
    )


@register_function(config_type=SshExecConfig)
async def h_ssh_exec(config: SshExecConfig, builder: Builder):
    """Build a direct SSH executor which always consults its configured gate."""

    gate = await builder.get_function(config.gate_fn)

    async def invoke(request: SshExecRequest) -> SshExecResponse:
        host = request.host
        command = request.command
        gate_subject = _gate_subject(host, config.port, command)
        decision = await gate.ainvoke(gate_subject)
        if getattr(decision, "verdict", None) != "ALLOW":
            layer = str(getattr(decision, "layer", "gate_error"))
            reason = getattr(decision, "reason", None) or "SSH command was not authorized"
            logger.warning("h_ssh_exec denied target=%s layer=%s reason=%s", host, layer, reason)
            return SshExecResponse(status="denied", reason=str(reason), gate_layer=layer)

        connect_options: dict = {
            "host": host,
            "port": config.port,
            "username": config.username,
            "connect_timeout": config.connect_timeout_seconds,
            "known_hosts": (
                str(Path(config.known_hosts).expanduser()) if config.verify_host_key else None
            ),
        }
        if config.password is not None:
            connect_options["password"] = config.password.get_secret_value()
        if config.client_key is not None:
            connect_options["client_keys"] = [str(Path(config.client_key).expanduser())]
        if config.client_key_passphrase is not None:
            connect_options["passphrase"] = config.client_key_passphrase.get_secret_value()

        try:
            async with asyncssh.connect(**connect_options) as connection:
                result = await connection.run(
                    command,
                    check=False,
                    timeout=config.command_timeout_seconds,
                )
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            logger.warning("h_ssh_exec failed target=%s error_type=%s", host, type(exc).__name__)
            return SshExecResponse(status="error", reason=f"{type(exc).__name__}: {exc}")

        exit_status = result.exit_status if isinstance(result.exit_status, int) else None
        if exit_status != 0:
            return SshExecResponse(
                status="error",
                output=str(result.stdout or ""),
                stderr=str(result.stderr or ""),
                exit_status=exit_status,
                reason=f"SSH command exited with status {exit_status}",
            )
        return SshExecResponse(
            status="ok",
            output=str(result.stdout or ""),
            stderr=str(result.stderr or ""),
            exit_status=exit_status,
        )

    yield FunctionInfo.from_fn(
        invoke,
        description="Execute an h_asimov_gate-authorized command over direct SSH.",
        converters=[_string_to_ssh_request, _ssh_response_to_str],
    )
