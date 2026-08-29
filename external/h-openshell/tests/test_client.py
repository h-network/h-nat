from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import grpc
import pytest

from nat.plugins.h_openshell import (
    ConfigurationError,
    OpenShellClient,
    SandboxNameError,
    SandboxTimeoutError,
)
from nat.plugins.h_openshell._proto import openshell_pb2


class FakeChannel:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class FakeRPCError(grpc.RpcError):
    def __init__(self, status: grpc.StatusCode) -> None:
        self._status = status

    def code(self) -> grpc.StatusCode:
        return self._status

    def __str__(self) -> str:
        return self._status.name


def sandbox_message(
    name: str,
    *,
    sandbox_id: str = "00000000-0000-0000-0000-000000000001",
    phase: int = openshell_pb2.SANDBOX_PHASE_READY,
) -> Any:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            id=sandbox_id,
            name=name,
            workspace="default",
        ),
        status=SimpleNamespace(phase=phase),
    )


def make_client(stub: Any) -> tuple[OpenShellClient, FakeChannel]:
    channel = FakeChannel()
    client = OpenShellClient(
        endpoint="gateway.test:8080",
        ca_cert=b"ca",
        client_cert=b"cert",
        client_key=b"key",
        channel_factory=lambda *_args, **_kwargs: channel,
        stub_factory=lambda _channel: stub,
    )
    return client, channel


def write_gateway_home(root: Path, endpoint: str = "https://local:8080") -> None:
    gateway = root / "gateways" / "dev"
    mtls = gateway / "mtls"
    mtls.mkdir(parents=True)
    (root / "active_gateway").write_text("dev\n", encoding="utf-8")
    (gateway / "metadata.json").write_text(
        json.dumps({"gateway_endpoint": endpoint}), encoding="utf-8"
    )
    (mtls / "ca.crt").write_bytes(b"ca")
    (mtls / "tls.crt").write_bytes(b"cert")
    (mtls / "tls.key").write_bytes(b"key")


def test_from_default_home_normalizes_endpoint_and_honors_override(tmp_path: Path) -> None:
    write_gateway_home(tmp_path)
    captured: dict[str, Any] = {}
    channel = FakeChannel()

    def channel_factory(endpoint: str, _credentials: Any, *, options: Any) -> Any:
        captured.update(endpoint=endpoint, options=options)
        return channel

    client = OpenShellClient.from_default_home(
        tmp_path,
        endpoint_override="remote.test:9443",
        target_override="gateway-cert-name",
        channel_factory=channel_factory,
        stub_factory=lambda _channel: object(),
    )

    assert client.endpoint == "remote.test:9443"
    assert captured == {
        "endpoint": "remote.test:9443",
        "options": (("grpc.ssl_target_name_override", "gateway-cert-name"),),
    }


def test_from_default_home_wraps_configuration_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="active_gateway"):
        OpenShellClient.from_default_home(tmp_path)


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    client, channel = make_client(object())
    await client.close()
    await client.close()
    assert channel.close_calls == 1


@pytest.mark.asyncio
async def test_unknown_phase_is_preserved() -> None:
    class Stub:
        async def GetSandbox(self, _request: Any, *, timeout: float) -> Any:
            return SimpleNamespace(sandbox=sandbox_message("agent", phase=99))

    client, _ = make_client(Stub())
    sandbox = await client.get_sandbox("agent")
    assert sandbox.phase == 99
    assert sandbox.phase_name == "UNRECOGNIZED_99"


def test_v0116_descriptor_uses_metadata_and_nested_status() -> None:
    sandbox_fields = openshell_pb2.Sandbox.DESCRIPTOR.fields_by_name
    assert list(sandbox_fields) == ["metadata", "spec", "status"]
    assert openshell_pb2.SandboxResponse.DESCRIPTOR.fields_by_name["sandbox"].number == 1


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("a" * 20, "maximum length"),
        ("é" * 10, "maximum length"),
        ("Upper", "lowercase ASCII"),
        ("café", "lowercase ASCII"),
        ("-leading", "start or end"),
        ("trailing-", "start or end"),
        ("two--hyphens", "consecutive"),
    ],
)
@pytest.mark.asyncio
async def test_create_rejects_names_the_gateway_would_reject(
    name: str, message: str
) -> None:
    client, _ = make_client(object())
    with pytest.raises(SandboxNameError, match=message):
        await client.create_sandbox(name)


@pytest.mark.asyncio
async def test_create_sends_present_empty_spec_and_maps_v0116_response() -> None:
    captured: Any = None

    class Stub:
        async def ListSandboxes(self, _request: Any, *, timeout: float) -> Any:
            return SimpleNamespace(sandboxes=[])

        async def CreateSandbox(self, request: Any, *, timeout: float) -> Any:
            nonlocal captured
            captured = request
            return openshell_pb2.SandboxResponse(
                sandbox=openshell_pb2.Sandbox(
                    metadata={"id": "id-1", "name": "agent", "workspace": "default"},
                    status={"phase": openshell_pb2.SANDBOX_PHASE_READY},
                )
            )

    client, _ = make_client(Stub())
    sandbox = await client.create_sandbox("agent")

    assert captured.HasField("spec")
    assert captured.spec == openshell_pb2.SandboxSpec()
    assert sandbox.id == "id-1"
    assert sandbox.name == "agent"
    assert sandbox.workspace == "default"
    assert sandbox.phase_name == "SANDBOX_PHASE_READY"


def test_v0116_sandbox_response_round_trips_on_the_wire() -> None:
    encoded = openshell_pb2.SandboxResponse(
        sandbox=openshell_pb2.Sandbox(
            metadata={
                "id": "id-1",
                "name": "agent",
                "workspace": "default",
                "resource_version": 7,
            },
            status={"phase": openshell_pb2.SANDBOX_PHASE_PROVISIONING},
        )
    ).SerializeToString()

    decoded = openshell_pb2.SandboxResponse.FromString(encoded)
    view = OpenShellClient._sandbox_view(decoded.sandbox)

    assert view.id == "id-1"
    assert view.name == "agent"
    assert view.workspace == "default"
    assert view.phase_name == "SANDBOX_PHASE_PROVISIONING"


@pytest.mark.asyncio
async def test_uuid_resolution_paginates() -> None:
    wanted = "11111111-1111-1111-1111-111111111111"

    class Stub:
        async def ListSandboxes(self, request: Any, *, timeout: float) -> Any:
            if request.offset == 0:
                items = [
                    sandbox_message(
                        f"agent-{index}",
                        sandbox_id=f"00000000-0000-0000-0000-{index:012d}",
                    )
                    for index in range(100)
                ]
            else:
                items = [sandbox_message("wanted", sandbox_id=wanted)]
            return SimpleNamespace(sandboxes=items)

    client, _ = make_client(Stub())
    assert await client._resolve_name(wanted) == "wanted"


@pytest.mark.asyncio
async def test_exec_resolves_name_on_every_call_and_preserves_binary_output() -> None:
    ids = iter(
        [
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        ]
    )
    executed_ids: list[str] = []

    class Stub:
        async def GetSandbox(self, _request: Any, *, timeout: float) -> Any:
            return SimpleNamespace(
                sandbox=sandbox_message("agent", sandbox_id=next(ids))
            )

        async def ExecSandbox(
            self, request: Any, *, timeout: float
        ) -> AsyncIterator[Any]:
            executed_ids.append(request.sandbox_id)
            yield openshell_pb2.ExecSandboxEvent(
                stdout=openshell_pb2.ExecSandboxStdout(data=b"a\xff")
            )
            yield openshell_pb2.ExecSandboxEvent(
                stderr=openshell_pb2.ExecSandboxStderr(data=b"warn")
            )
            yield openshell_pb2.ExecSandboxEvent(
                exit=openshell_pb2.ExecSandboxExit(exit_code=7)
            )

    client, _ = make_client(Stub())
    first = await client.exec("agent", ["test"])
    await client.exec("agent", ["test"])

    assert executed_ids == [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    assert first.exit_code == 7
    assert first.stdout == b"a\xff"
    assert first.stdout_text == "a�"
    assert first.stderr == b"warn"


@pytest.mark.asyncio
async def test_exec_distinguishes_missing_exit_event() -> None:
    class Stub:
        async def ExecSandbox(
            self, _request: Any, *, timeout: float
        ) -> AsyncIterator[Any]:
            yield openshell_pb2.ExecSandboxEvent(
                stdout=openshell_pb2.ExecSandboxStdout(data=b"partial")
            )

    client, _ = make_client(Stub())
    result = await client.exec(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", ["test"]
    )
    assert result.exit_code is None


@pytest.mark.asyncio
async def test_delete_waits_for_not_found() -> None:
    calls = 0

    class Stub:
        async def DeleteSandbox(self, _request: Any, *, timeout: float) -> Any:
            return SimpleNamespace(deleted=True)

        async def GetSandbox(self, _request: Any, *, timeout: float) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                return SimpleNamespace(sandbox=sandbox_message("agent"))
            raise FakeRPCError(grpc.StatusCode.NOT_FOUND)

    client, _ = make_client(Stub())
    assert await client.delete_sandbox(
        "agent", timeout_seconds=1, poll_interval_seconds=0
    )
    assert calls == 2


@pytest.mark.asyncio
async def test_delete_timeout_is_not_false_success() -> None:
    class Stub:
        async def DeleteSandbox(self, _request: Any, *, timeout: float) -> Any:
            return SimpleNamespace(deleted=True)

        async def GetSandbox(self, _request: Any, *, timeout: float) -> Any:
            return SimpleNamespace(sandbox=sandbox_message("agent"))

    client, _ = make_client(Stub())
    with pytest.raises(SandboxTimeoutError, match="still existed"):
        await client.delete_sandbox(
            "agent", timeout_seconds=0, poll_interval_seconds=0
        )
