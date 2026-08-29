from __future__ import annotations

import importlib.metadata
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nat.cli.type_registry import GlobalTypeRegistry
from nat.builder.workflow_builder import WorkflowBuilder
from nat.plugins.h_openshell import ExecResult, Sandbox
from nat.plugins.h_openshell._proto import openshell_pb2
from nat.plugins.h_openshell import register
from nat.runtime.loader import load_config


CANONICAL_TYPES = {
    "h_openshell_create_sandbox",
    "h_openshell_delete_sandbox",
    "h_openshell_exec",
    "h_openshell_exec_stream",
    "h_openshell_get_sandbox",
    "h_openshell_health",
    "h_openshell_list_sandboxes",
}


class FakeClient:
    endpoint = "fake.test:8080"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def health(self, **kwargs: Any) -> tuple[int, str]:
        self.calls.append(("health", kwargs))
        return 1, "0.0.36"

    async def list_sandboxes(self, **kwargs: Any) -> list[Sandbox]:
        self.calls.append(("list", kwargs))
        return [Sandbox("id-1", "agent", "default", 2, "SANDBOX_PHASE_READY")]

    async def _resolve_name(self, name_or_id: str) -> str:
        self.calls.append(("resolve_name", name_or_id))
        return "agent"

    async def get_sandbox(self, name: str, **kwargs: Any) -> Sandbox:
        self.calls.append(("get", (name, kwargs)))
        return Sandbox("id-1", name, "default", 2, "SANDBOX_PHASE_READY")

    async def create_sandbox(self, name: str, **kwargs: Any) -> Sandbox:
        self.calls.append(("create", (name, kwargs)))
        return Sandbox("id-1", name, "default", 2, "SANDBOX_PHASE_READY")

    async def delete_sandbox(self, name_or_id: str, **kwargs: Any) -> bool:
        self.calls.append(("delete", (name_or_id, kwargs)))
        return True

    async def exec(self, sandbox: str, command: list[str], **kwargs: Any) -> ExecResult:
        self.calls.append(("exec", (sandbox, command, kwargs)))
        return ExecResult(7, b"out", b"err")

    async def exec_stream(
        self, sandbox: str, command: list[str], **kwargs: Any
    ) -> AsyncIterator[Any]:
        self.calls.append(("exec_stream", (sandbox, command, kwargs)))
        yield openshell_pb2.ExecSandboxEvent(
            stdout=openshell_pb2.ExecSandboxStdout(data=b"out")
        )
        yield openshell_pb2.ExecSandboxEvent(
            stderr=openshell_pb2.ExecSandboxStderr(data=b"err")
        )
        yield openshell_pb2.ExecSandboxEvent(
            exit=openshell_pb2.ExecSandboxExit(exit_code=7)
        )


def test_distribution_exposes_one_nat_entry_point() -> None:
    entries = [
        entry
        for entry in importlib.metadata.entry_points(group="nat.components")
        if entry.name == "h_openshell"
    ]
    assert [(entry.name, entry.value) for entry in entries] == [
        ("h_openshell", "nat.plugins.h_openshell.register")
    ]


def test_registry_contains_exact_canonical_function_names() -> None:
    registered = {
        info.discovery_metadata.component_name
        for info in GlobalTypeRegistry.get().get_registered_functions()
        if info.module_name == "nat.plugins.h_openshell"
    }
    assert registered == CANONICAL_TYPES


@pytest.mark.asyncio
async def test_nat_workflow_builds_all_functions_without_gateway_contact(
    tmp_path: Path,
) -> None:
    home = tmp_path / "openshell"
    gateway = home / "gateways" / "test"
    mtls = gateway / "mtls"
    mtls.mkdir(parents=True)
    (home / "active_gateway").write_text("test\n", encoding="utf-8")
    (gateway / "metadata.json").write_text(
        json.dumps({"gateway_endpoint": "127.0.0.1:1"}), encoding="utf-8"
    )
    (mtls / "ca.crt").write_bytes(b"not-a-real-ca")
    (mtls / "tls.crt").write_bytes(b"not-a-real-cert")
    (mtls / "tls.key").write_bytes(b"not-a-real-key")

    path = tmp_path / "workflow.yaml"
    path.write_text(
        f"""
functions:
  health:
    _type: h_openshell_health
    gateway_home: {home}
  create:
    _type: h_openshell_create_sandbox
    gateway_home: {home}
  delete:
    _type: h_openshell_delete_sandbox
    gateway_home: {home}
  get:
    _type: h_openshell_get_sandbox
    gateway_home: {home}
  list:
    _type: h_openshell_list_sandboxes
    gateway_home: {home}
  exec:
    _type: h_openshell_exec
    gateway_home: {home}
    sandbox: agent
  exec_stream:
    _type: h_openshell_exec_stream
    gateway_home: {home}
    sandbox: agent
workflow:
  _type: h_openshell_health
  gateway_home: {home}
""".lstrip(),
        encoding="utf-8",
    )

    config = load_config(str(path))
    async with WorkflowBuilder.from_config(config) as builder:
        for name in (
            "health",
            "create",
            "delete",
            "get",
            "list",
            "exec",
            "exec_stream",
        ):
            assert await builder.get_function(name) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("builder", "config"),
    [
        (register.h_openshell_health, register.OpenShellHealthConfig()),
        (
            register.h_openshell_list_sandboxes,
            register.OpenShellListSandboxesConfig(),
        ),
        (register.h_openshell_get_sandbox, register.OpenShellGetSandboxConfig()),
        (
            register.h_openshell_create_sandbox,
            register.OpenShellCreateSandboxConfig(),
        ),
        (
            register.h_openshell_delete_sandbox,
            register.OpenShellDeleteSandboxConfig(),
        ),
        (
            register.h_openshell_exec,
            register.OpenShellExecConfig(sandbox="agent"),
        ),
        (
            register.h_openshell_exec_stream,
            register.OpenShellExecStreamConfig(sandbox="agent"),
        ),
    ],
)
async def test_builders_are_lazy_and_close_on_teardown(
    monkeypatch: pytest.MonkeyPatch, builder: Any, config: Any
) -> None:
    client = FakeClient()
    monkeypatch.setattr(register, "_build_client", lambda _config: client)

    generator = builder.__wrapped__(config, SimpleNamespace())
    info = await anext(generator)

    assert info.single_fn is not None or info.stream_fn is not None
    assert client.calls == []
    await generator.aclose()
    assert client.closed


@pytest.mark.asyncio
async def test_unary_exec_returns_deterministic_complete_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    monkeypatch.setattr(register, "_build_client", lambda _config: client)
    config = register.OpenShellExecConfig(sandbox="agent")
    generator = register.h_openshell_exec.__wrapped__(config, SimpleNamespace())
    info = await anext(generator)

    assert info.single_fn is not None
    output = await info.single_fn("printf hello")
    assert output == '{"exit_code":7,"stderr":"err","stdout":"out"}'
    assert client.calls == [
        (
            "exec",
            (
                "agent",
                ["bash", "-c", "printf hello"],
                {"rpc_timeout": 600.0, "timeout_seconds": 0},
            ),
        )
    ]
    await generator.aclose()


@pytest.mark.asyncio
async def test_streaming_exec_frames_stdout_stderr_and_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    monkeypatch.setattr(register, "_build_client", lambda _config: client)
    config = register.OpenShellExecStreamConfig(sandbox="agent")
    generator = register.h_openshell_exec_stream.__wrapped__(
        config, SimpleNamespace()
    )
    info = await anext(generator)

    assert info.stream_fn is not None
    chunks = [chunk async for chunk in info.stream_fn("run")]
    assert [json.loads(chunk) for chunk in chunks] == [
        {"data": "out", "type": "stdout"},
        {"data": "err", "type": "stderr"},
        {"exit_code": 7, "type": "exit"},
    ]
    assert all(chunk.endswith("\n") for chunk in chunks)
    await generator.aclose()
