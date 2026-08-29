import asyncio
import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError


class FunctionBaseConfig(BaseModel):
    def __init_subclass__(cls, name=None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.nat_name = name


class FunctionInfo:
    @classmethod
    def from_fn(cls, function, description=""):
        return SimpleNamespace(function=function, description=description)


def _module(name, **attributes):
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module


@pytest.fixture(scope="module")
def register_module():
    class OpenShellClient:
        @classmethod
        def from_default_home(cls, **kwargs):
            raise AssertionError("tests replace the client factory")

    def register_function(*, config_type):
        return lambda function: function

    _module(
        "nat.plugin_api",
        Builder=object,
        FunctionBaseConfig=FunctionBaseConfig,
        FunctionInfo=FunctionInfo,
        register_function=register_function,
    )
    _module("nat.plugins.h_openshell", OpenShellClient=OpenShellClient)
    sys.modules.pop("nat.plugins.h_orchestrator.register", None)
    return importlib.import_module("nat.plugins.h_orchestrator.register")


def test_configs_are_strict_and_validate_delivery(register_module):
    config = register_module.AgentInvokeConfig(sandbox="box", command="agent")
    assert config.output_parser == "raw"
    with pytest.raises(ValidationError):
        register_module.AgentInvokeConfig(sandbox="box", command="agent", stale=True)
    with pytest.raises(ValidationError):
        register_module.AgentInvokeConfig(
            sandbox="box", command="agent", prompt_via="env:BAD-NAME"
        )


def test_claude_wrapper_defaults_and_settings(register_module):
    config = register_module.ClaudeInvokeConfig(
        sandbox="box", hook_settings_path="/sandbox/settings.json"
    )
    assert config.command == "claude"
    assert config.output_parser == "claude_json"
    assert config.args[-2:] == ["--settings", "/sandbox/settings.json"]
    assert "--no-session-persistence" in config.args


@pytest.mark.asyncio
async def test_unary_builder_is_lazy_and_closes_client(register_module, monkeypatch):
    class Client:
        closed = False

        async def exec(self, sandbox, command, *, stdin, rpc_timeout):
            return SimpleNamespace(exit_code=0, stdout_text="answer", stderr_text="")

        async def close(self):
            self.closed = True

    client = Client()
    calls = []
    monkeypatch.setattr(register_module, "_client", lambda config: calls.append(config) or client)
    config = register_module.AgentInvokeConfig(sandbox="box", command="agent")
    generator = register_module._invoke_builder(config, "h_agent_invoke")
    function_info = await anext(generator)
    assert calls == []
    assert await function_info.function("hello") == "answer"
    assert len(calls) == 1
    await generator.aclose()
    assert client.closed


@pytest.mark.asyncio
async def test_concurrent_first_invocations_share_one_client(register_module, monkeypatch):
    class Client:
        async def exec(self, sandbox, command, *, stdin, rpc_timeout):
            await asyncio.sleep(0)
            return SimpleNamespace(exit_code=0, stdout_text="answer", stderr_text="")

        async def close(self):
            pass

    calls = []
    monkeypatch.setattr(
        register_module, "_client", lambda config: calls.append(config) or Client()
    )
    config = register_module.AgentInvokeConfig(sandbox="box", command="agent")
    generator = register_module._invoke_builder(config, "h_agent_invoke")
    function_info = await anext(generator)
    assert await asyncio.gather(
        function_info.function("one"), function_info.function("two")
    ) == ["answer", "answer"]
    assert len(calls) == 1
    await generator.aclose()


@pytest.mark.asyncio
async def test_stream_builder_reports_missing_exit(register_module, monkeypatch):
    class Event:
        stdout = SimpleNamespace(data=b"chunk")

        def WhichOneof(self, field):
            return "stdout"

    class Client:
        async def exec_stream(self, *args, **kwargs):
            yield Event()

        async def close(self):
            pass

    monkeypatch.setattr(register_module, "_client", lambda config: Client())
    config = register_module.AgentStreamConfig(sandbox="box", command="agent")
    generator = register_module._stream_builder(config, "h_agent_stream")
    function_info = await anext(generator)
    chunks = [chunk async for chunk in function_info.function("hello")]
    assert chunks == ["chunk", "[exit_code=missing]"]
    await generator.aclose()
