"""Asynchronous adapter for the public OpenShell gateway API."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Self

import grpc

from ._proto import openshell_pb2, openshell_pb2_grpc
from .errors import (
    ConfigurationError,
    GatewayRPCError,
    SandboxLifecycleError,
    SandboxNameError,
    SandboxTimeoutError,
)
from .models import ExecResult, Sandbox


def _looks_like_uuid(value: str) -> bool:
    parts = value.split("-")
    return len(value) == 36 and tuple(map(len, parts)) == (8, 4, 4, 4, 12)


def _validate_sandbox_name(name: str) -> None:
    """Mirror the v0.0.116 gateway's routable sandbox-name validator."""

    if not name:
        return
    byte_length = len(name.encode("utf-8"))
    if byte_length > 19:
        raise SandboxNameError(
            f"sandbox name exceeds maximum length ({byte_length} > 19 bytes)"
        )
    if not all(
        character.isascii()
        and (character.islower() or character.isdigit() or character == "-")
        for character in name
    ):
        raise SandboxNameError(
            "sandbox name must contain only lowercase ASCII letters, digits, or hyphens"
        )
    if name.startswith("-") or name.endswith("-"):
        raise SandboxNameError("sandbox name must not start or end with a hyphen")
    if "--" in name:
        raise SandboxNameError("sandbox name must not contain consecutive hyphens")


class OpenShellClient:
    """One reusable, authenticated ``grpc.aio`` gateway connection."""

    def __init__(
        self,
        *,
        endpoint: str,
        ca_cert: bytes,
        client_cert: bytes,
        client_key: bytes,
        target_override: str = "localhost",
        channel_factory: Callable[..., Any] | None = None,
        stub_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        credentials = grpc.ssl_channel_credentials(
            root_certificates=ca_cert,
            certificate_chain=client_cert,
            private_key=client_key,
        )
        make_channel = channel_factory or grpc.aio.secure_channel
        self._channel = make_channel(
            endpoint,
            credentials,
            options=(("grpc.ssl_target_name_override", target_override),),
        )
        make_stub = stub_factory or openshell_pb2_grpc.OpenShellStub
        self._stub = make_stub(self._channel)
        self._endpoint = endpoint
        self._closed = False

    @classmethod
    def from_default_home(
        cls,
        gateway_home: Path | str | None = None,
        *,
        endpoint_override: str | None = None,
        target_override: str = "localhost",
        **construction_overrides: Any,
    ) -> OpenShellClient:
        """Construct from the active gateway used by the OpenShell CLI."""

        home = Path(
            gateway_home
            or os.environ.get("OPENSHELL_HOME")
            or Path.home() / ".config" / "openshell"
        )
        try:
            active = (home / "active_gateway").read_text(encoding="utf-8").strip()
            if not active:
                raise ValueError("active_gateway is empty")
            gateway_dir = home / "gateways" / active
            metadata = json.loads(
                (gateway_dir / "metadata.json").read_text(encoding="utf-8")
            )
            endpoint = endpoint_override or metadata["gateway_endpoint"]
            if "://" in endpoint:
                endpoint = endpoint.split("://", 1)[1]
            mtls = gateway_dir / "mtls"
            return cls(
                endpoint=endpoint,
                ca_cert=(mtls / "ca.crt").read_bytes(),
                client_cert=(mtls / "tls.crt").read_bytes(),
                client_key=(mtls / "tls.key").read_bytes(),
                target_override=target_override,
                **construction_overrides,
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"invalid OpenShell gateway configuration under {home}: {exc}"
            ) from exc

    @property
    def endpoint(self) -> str:
        return self._endpoint

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._channel.close()

    @staticmethod
    def _sandbox_view(message: Any) -> Sandbox:
        phase = int(message.status.phase)
        try:
            phase_name = openshell_pb2.SandboxPhase.Name(phase)
        except ValueError:
            phase_name = f"UNRECOGNIZED_{phase}"
        return Sandbox(
            id=message.metadata.id,
            name=message.metadata.name,
            workspace=message.metadata.workspace,
            phase=phase,
            phase_name=phase_name,
        )

    @staticmethod
    def _raise_rpc(operation: str, exc: BaseException) -> None:
        raise GatewayRPCError(operation, exc) from exc

    async def health(self, *, timeout: float = 5.0) -> tuple[int, str]:
        try:
            response = await self._stub.Health(
                openshell_pb2.HealthRequest(), timeout=timeout
            )
        except grpc.RpcError as exc:
            self._raise_rpc("Health", exc)
        return int(response.status), response.version

    async def get_sandbox(self, name: str, *, timeout: float = 5.0) -> Sandbox:
        try:
            response = await self._stub.GetSandbox(
                openshell_pb2.GetSandboxRequest(name=name), timeout=timeout
            )
        except grpc.RpcError as exc:
            self._raise_rpc("GetSandbox", exc)
        return self._sandbox_view(response.sandbox)

    async def _list_page(
        self, *, limit: int, offset: int, timeout: float
    ) -> list[Sandbox]:
        try:
            response = await self._stub.ListSandboxes(
                openshell_pb2.ListSandboxesRequest(limit=limit, offset=offset),
                timeout=timeout,
            )
        except grpc.RpcError as exc:
            self._raise_rpc("ListSandboxes", exc)
        return [self._sandbox_view(item) for item in response.sandboxes]

    async def list_sandboxes(
        self, *, limit: int = 100, offset: int = 0, timeout: float = 5.0
    ) -> list[Sandbox]:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset must be non-negative")
        return await self._list_page(limit=limit, offset=offset, timeout=timeout)

    async def _resolve_id(self, name_or_id: str) -> str:
        if _looks_like_uuid(name_or_id):
            return name_or_id
        return (await self.get_sandbox(name_or_id)).id

    async def _resolve_name(self, name_or_id: str) -> str:
        if not _looks_like_uuid(name_or_id):
            return name_or_id
        offset = 0
        page_size = 100
        seen: set[str] = set()
        while True:
            page = await self._list_page(
                limit=page_size, offset=offset, timeout=5.0
            )
            for sandbox in page:
                if sandbox.id == name_or_id:
                    return sandbox.name
            ids = {sandbox.id for sandbox in page}
            if len(page) < page_size or not ids or ids <= seen:
                break
            seen.update(ids)
            offset += len(page)
        raise SandboxLifecycleError(f"no sandbox with UUID {name_or_id!r}")

    async def _wait_ready(
        self,
        name: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> Sandbox:
        deadline = time.monotonic() + timeout_seconds
        last: Sandbox | None = None
        while time.monotonic() < deadline:
            last = await self.get_sandbox(name)
            if last.phase == openshell_pb2.SANDBOX_PHASE_READY:
                return last
            if last.phase == openshell_pb2.SANDBOX_PHASE_ERROR:
                raise SandboxLifecycleError(
                    f"sandbox {name!r} entered {last.phase_name}"
                )
            await asyncio.sleep(poll_interval_seconds)
        phase = last.phase_name if last else "never-observed"
        raise SandboxTimeoutError(
            f"sandbox {name!r} was not ready within {timeout_seconds}s "
            f"(last phase: {phase})"
        )

    async def create_sandbox(
        self,
        name: str,
        *,
        spec: Any | None = None,
        timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 2.0,
        rpc_timeout: float = 30.0,
    ) -> Sandbox:
        _validate_sandbox_name(name)
        offset = 0
        while True:
            page = await self._list_page(limit=100, offset=offset, timeout=5.0)
            existing = next((item for item in page if item.name == name), None)
            if existing:
                if existing.phase == openshell_pb2.SANDBOX_PHASE_READY:
                    return existing
                return await self._wait_ready(
                    name,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                )
            if len(page) < 100:
                break
            offset += len(page)

        request = openshell_pb2.CreateSandboxRequest(
            name=name,
            spec=spec if spec is not None else openshell_pb2.SandboxSpec(),
        )
        try:
            response = await self._stub.CreateSandbox(request, timeout=rpc_timeout)
        except grpc.RpcError as exc:
            self._raise_rpc("CreateSandbox", exc)
        created = self._sandbox_view(response.sandbox)
        if created.phase == openshell_pb2.SANDBOX_PHASE_READY:
            return created
        if created.phase == openshell_pb2.SANDBOX_PHASE_ERROR:
            raise SandboxLifecycleError(
                f"sandbox {name!r} entered {created.phase_name}"
            )
        return await self._wait_ready(
            name,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    async def delete_sandbox(
        self,
        name_or_id: str,
        *,
        wait: bool = True,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 1.0,
        rpc_timeout: float = 30.0,
    ) -> bool:
        name = await self._resolve_name(name_or_id)
        try:
            response = await self._stub.DeleteSandbox(
                openshell_pb2.DeleteSandboxRequest(name=name), timeout=rpc_timeout
            )
        except grpc.RpcError as exc:
            self._raise_rpc("DeleteSandbox", exc)
        if not wait:
            return bool(response.deleted)

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                await self.get_sandbox(name)
            except GatewayRPCError as exc:
                if exc.status_code == grpc.StatusCode.NOT_FOUND:
                    return True
                raise
            await asyncio.sleep(poll_interval_seconds)
        raise SandboxTimeoutError(
            f"sandbox {name!r} still existed after {timeout_seconds}s"
        )

    async def exec_stream(
        self,
        name_or_id: str,
        command: list[str],
        *,
        workdir: str = "",
        environment: dict[str, str] | None = None,
        stdin: bytes = b"",
        timeout_seconds: int = 0,
        tty: bool = False,
        rpc_timeout: float = 600.0,
    ) -> AsyncIterator[Any]:
        sandbox_id = await self._resolve_id(name_or_id)
        request = openshell_pb2.ExecSandboxRequest(
            sandbox_id=sandbox_id,
            command=command,
            workdir=workdir,
            environment=environment or {},
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            tty=tty,
        )
        try:
            async for event in self._stub.ExecSandbox(request, timeout=rpc_timeout):
                yield event
        except grpc.RpcError as exc:
            self._raise_rpc("ExecSandbox", exc)

    async def exec(self, name_or_id: str, command: list[str], **kwargs: Any) -> ExecResult:
        stdout = bytearray()
        stderr = bytearray()
        exit_code: int | None = None
        async for event in self.exec_stream(name_or_id, command, **kwargs):
            payload = event.WhichOneof("payload")
            if payload == "stdout":
                stdout.extend(event.stdout.data)
            elif payload == "stderr":
                stderr.extend(event.stderr.data)
            elif payload == "exit":
                exit_code = int(event.exit.exit_code)
        return ExecResult(exit_code, bytes(stdout), bytes(stderr))
