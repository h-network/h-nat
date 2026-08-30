import asyncio
import importlib
import json
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
    def from_fn(cls, function, description="", **kwargs):
        return SimpleNamespace(
            function=function, description=description, **kwargs
        )

    @classmethod
    def create(cls, *, single_fn, description="", **kwargs):
        return SimpleNamespace(
            function=single_fn, description=description, **kwargs
        )


class FunctionGroup:
    SEPARATOR = "__"

    @staticmethod
    def decompose(name):
        return tuple(name.split(FunctionGroup.SEPARATOR, maxsplit=1))


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

    class BoundedBufferStore:
        pass

    class AsyncSshError(Exception):
        pass

    def register_function(*, config_type):
        return lambda function: function

    _module(
        "nat.plugin_api",
        Builder=object,
        FunctionBaseConfig=FunctionBaseConfig,
        FunctionInfo=FunctionInfo,
        FunctionGroup=FunctionGroup,
        FunctionGroupRef=str,
        FunctionRef=str,
        register_function=register_function,
    )
    _module("nat.plugins.h_openshell", OpenShellClient=OpenShellClient)
    _module("nat.plugins.h_memory", BoundedBufferStore=BoundedBufferStore)
    _module(
        "asyncssh",
        Error=AsyncSshError,
        connect=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("tests replace asyncssh.connect")
        ),
    )
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


@pytest.mark.asyncio
async def test_claude_stream_handles_split_utf8_json_lines(register_module):
    module = sys.modules["nat.plugins.h_orchestrator.claude_stream"]
    payload = '{"type":"result","is_error":false,"result":"café"}\n'.encode()

    class Event:
        def __init__(self, kind, data=None, exit_code=None):
            self.kind = kind
            if kind == "stdout":
                self.stdout = SimpleNamespace(data=data)
            if kind == "exit":
                self.exit = SimpleNamespace(exit_code=exit_code)

        def WhichOneof(self, field):
            return self.kind

    class Client:
        async def exec_stream(self, *args, **kwargs):
            split = payload.index(b"\xc3") + 1
            yield Event("stdout", payload[:split])
            yield Event("stdout", payload[split:])
            yield Event("exit", exit_code=0)

    config = module.ClaudeStreamConfig(sandbox="box")
    assert await module.consume_claude_stream(Client(), config, "hello") == "café"


@pytest.mark.asyncio
async def test_claude_stream_reports_error_result(register_module):
    module = sys.modules["nat.plugins.h_orchestrator.claude_stream"]

    class Event:
        def __init__(self, kind):
            self.kind = kind
            self.stdout = SimpleNamespace(
                data=b'{"type":"result","is_error":true,"result":"denied"}\n'
            )
            self.exit = SimpleNamespace(exit_code=0)

        def WhichOneof(self, field):
            return self.kind

    class Client:
        async def exec_stream(self, *args, **kwargs):
            yield Event("stdout")
            yield Event("exit")

    config = module.ClaudeStreamConfig(sandbox="box")
    result = await module.consume_claude_stream(Client(), config, "hello")
    assert result == "[claude_stream error: denied]"


def test_chat_cycle_addressing_and_prompt(register_module):
    module = sys.modules["nat.plugins.h_orchestrator.chat_cycle"]
    config = module.HChatCycleConfig(
        dispatcher="agent", chat_id="default", pod="pod", agent="agent"
    )
    request = module.HChatCycleInput(message="next", chat_id="override")
    assert module.resolve_addressing(request, config) == (
        "override",
        "pod",
        "agent",
    )
    assert module.build_chat_prompt(
        [{"role": "user", "content": "before"}], "next"
    ) == "Previous conversation:\n[user] before\n\nCurrent message:\nnext\n"


@pytest.mark.asyncio
async def test_chat_cycle_reads_prior_turns_oldest_first(register_module):
    module = sys.modules["nat.plugins.h_orchestrator.chat_cycle"]

    class Redis:
        async def zrevrange(self, key, start, end):
            return ["new", "old"]

        async def mget(self, *keys):
            return [
                '{"role":"assistant","content":"new"}',
                '{"role":"user","content":"old"}',
            ]

    assert await module.read_prior_turns(Redis(), "p", "a", "c") == [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "new"},
    ]


def test_chat_cycle_requires_complete_addressing(register_module):
    module = sys.modules["nat.plugins.h_orchestrator.chat_cycle"]
    config = module.HChatCycleConfig(dispatcher="agent")
    request = module.HChatCycleInput(message="hello")
    with pytest.raises(ValueError, match="chat_id, pod, agent"):
        module.resolve_addressing(request, config)


@pytest.mark.asyncio
async def test_chat_cycle_dispatches_writes_and_closes(register_module, monkeypatch):
    module = sys.modules["nat.plugins.h_orchestrator.chat_cycle"]
    written = []

    class Redis:
        closed = False

        async def zrevrange(self, key, start, end):
            return []

        async def aclose(self):
            self.closed = True

    redis = Redis()

    class Store:
        def __init__(self, **kwargs):
            assert kwargs["client"] is redis

        async def write_turn(
            self, chat_id, role, content, ttl_seconds, *, hot_keep_count
        ):
            written.append((chat_id, role, content, ttl_seconds, hot_keep_count))
            return f"turn:{role}"

    class Dispatcher:
        async def ainvoke(self, prompt, to_type):
            assert prompt == "hello"
            assert to_type is str
            return "answer"

    class Builder:
        async def get_function(self, name):
            assert name == "agent"
            return Dispatcher()

    monkeypatch.setattr(
        module.aioredis,
        "Redis",
        SimpleNamespace(from_url=lambda *args, **kwargs: redis),
    )
    monkeypatch.setattr(module, "BoundedBufferStore", Store)
    config = module.HChatCycleConfig(
        dispatcher="agent", chat_id="chat", pod="pod", agent="agent"
    )
    generator = module.h_chat_cycle(config, Builder())
    function_info = await anext(generator)
    assert function_info.function.__annotations__["request"] is module.HChatCycleInput
    assert function_info.function.__annotations__["return"] is module.HChatCycleOutput
    output = await function_info.function(module.HChatCycleInput(message="hello"))
    assert output.result == "answer"
    assert output.turn_id == "turn:assistant"
    assert [turn[1] for turn in written] == ["user", "assistant"]
    await generator.aclose()
    assert redis.closed


def test_ssh_exec_config_requires_deployment_credentials(register_module):
    module = sys.modules["nat.plugins.h_orchestrator.ssh_exec"]
    with pytest.raises(ValidationError, match="password or client_key"):
        module.SshExecConfig(gate_fn="gate", username="operator")
    config = module.SshExecConfig(
        gate_fn="gate", username="operator", password="secret"
    )
    assert set(module.SshExecRequest.model_json_schema()["properties"]) == {
        "host",
        "command",
    }
    assert module._string_to_ssh_request(
        '{"host":"router.example","command":"show version"}'
    ) == module.SshExecRequest(host="router.example", command="show version")
    assert "secret" not in repr(config)


@pytest.mark.asyncio
async def test_ssh_exec_denial_never_connects(register_module, monkeypatch):
    module = sys.modules["nat.plugins.h_orchestrator.ssh_exec"]

    class Gate:
        async def ainvoke(self, subject):
            assert json.loads(subject) == {
                "action": "ssh_exec",
                "host": "router.example",
                "port": 22,
                "command": "reload",
            }
            return SimpleNamespace(verdict="DENY", layer="L2_asimov", reason="unsafe")

    class Builder:
        async def get_function(self, name):
            assert name == "gate"
            return Gate()

    monkeypatch.setattr(
        module.asyncssh,
        "connect",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("SSH must not run")),
    )
    config = module.SshExecConfig(
        gate_fn="gate", username="operator", password="secret"
    )
    generator = module.h_ssh_exec(config, Builder())
    function_info = await anext(generator)
    response = await function_info.function(
        module.SshExecRequest(host="router.example", command="reload")
    )
    assert response.status == "denied"
    assert response.gate_layer == "L2_asimov"
    await generator.aclose()


@pytest.mark.asyncio
async def test_ssh_exec_allow_uses_exact_request_and_config_credentials(
    register_module, monkeypatch
):
    module = sys.modules["nat.plugins.h_orchestrator.ssh_exec"]
    calls = {}

    class Gate:
        async def ainvoke(self, subject):
            calls["subject"] = subject
            return SimpleNamespace(verdict="ALLOW", layer="passthrough", reason=None)

    class Builder:
        async def get_function(self, name):
            return Gate()

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            calls["closed"] = True

        async def run(self, command, **kwargs):
            calls["command"] = command
            calls["run_kwargs"] = kwargs
            return SimpleNamespace(stdout="output\n", stderr="", exit_status=0)

    def connect(**kwargs):
        calls["connect_kwargs"] = kwargs
        return Connection()

    monkeypatch.setattr(module.asyncssh, "connect", connect)
    config = module.SshExecConfig(
        gate_fn="gate",
        username="operator",
        password="secret",
        known_hosts="/known_hosts",
    )
    generator = module.h_ssh_exec(config, Builder())
    function_info = await anext(generator)
    request = module.SshExecRequest(host="router.example", command="show route")
    response = await function_info.function(request)
    assert response.status == "ok"
    assert response.output == "output\n"
    assert json.loads(calls["subject"]) == {
        "action": "ssh_exec",
        "host": "router.example",
        "port": 22,
        "command": "show route",
    }
    assert calls["command"] == request.command
    assert calls["connect_kwargs"]["host"] == request.host
    assert calls["connect_kwargs"]["password"] == "secret"
    assert calls["connect_kwargs"]["known_hosts"] == "/known_hosts"
    assert calls["closed"]
    await generator.aclose()


@pytest.mark.asyncio
async def test_gated_mcp_preserves_schema_and_denies_without_raw_call(register_module):
    module = sys.modules["nat.plugins.h_orchestrator.gated_mcp"]

    class ToolInput(BaseModel):
        router_name: str
        command: str
        timeout: int = 360

    class RawFunction:
        input_schema = ToolInput
        description = "Execute a device command"

        async def ainvoke(self, request, to_type):
            raise AssertionError("denied calls must not reach the raw MCP tool")

    class Group:
        def get_config(self):
            return SimpleNamespace(include=["get_router_list"])

        async def get_all_functions(self):
            return {"junos__execute_junos_command": RawFunction()}

    class Gate:
        async def ainvoke(self, subject):
            assert json.loads(subject) == {
                "action": "mcp_tool_call",
                "tool": "execute_junos_command",
                "arguments": {
                    "router_name": "R1",
                    "command": "clear bgp neighbor all",
                    "timeout": 360,
                },
            }
            return SimpleNamespace(verdict="DENY", layer="L2_asimov", reason="disruptive")

    class Builder:
        async def get_function_group(self, name):
            assert name == "junos"
            return Group()

        async def get_function(self, name):
            assert name == "gate"
            return Gate()

    config = module.GatedMcpToolConfig(
        mcp_group="junos", gate_fn="gate", mcp_tool_name="execute_junos_command"
    )
    generator = module.h_gated_mcp_tool(config, Builder())
    function_info = await anext(generator)
    assert function_info.input_schema is ToolInput
    response = await function_info.function(
        ToolInput(router_name="R1", command="clear bgp neighbor all")
    )
    assert response.status == "denied"
    assert response.gate_layer == "L2_asimov"
    await generator.aclose()


@pytest.mark.asyncio
async def test_gated_mcp_allow_invokes_hidden_raw_function(register_module):
    module = sys.modules["nat.plugins.h_orchestrator.gated_mcp"]

    class ToolInput(BaseModel):
        router_name: str
        config_text: str

    calls = []

    class RawFunction:
        input_schema = ToolInput
        description = "Commit configuration"

        async def ainvoke(self, request, to_type):
            calls.append((request, to_type))
            return "commit complete"

    class Group:
        def get_config(self):
            return SimpleNamespace(include=["get_junos_config"])

        async def get_all_functions(self):
            return {"junos__load_and_commit_config": RawFunction()}

    class Gate:
        async def ainvoke(self, subject):
            return SimpleNamespace(verdict="ALLOW", layer="passthrough", reason=None)

    class Builder:
        async def get_function_group(self, name):
            return Group()

        async def get_function(self, name):
            return Gate()

    config = module.GatedMcpToolConfig(
        mcp_group="junos", gate_fn="gate", mcp_tool_name="load_and_commit_config"
    )
    generator = module.h_gated_mcp_tool(config, Builder())
    function_info = await anext(generator)
    request = ToolInput(router_name="R2", config_text="set system services ssh")
    response = await function_info.function(request)
    assert response.status == "ok"
    assert response.output == "commit complete"
    assert calls == [(request, str)]
    await generator.aclose()


@pytest.mark.asyncio
async def test_gated_mcp_rejects_public_raw_tool(register_module):
    module = sys.modules["nat.plugins.h_orchestrator.gated_mcp"]

    class Group:
        def get_config(self):
            return SimpleNamespace(include=["execute_junos_command"])

    class Builder:
        async def get_function_group(self, name):
            return Group()

    config = module.GatedMcpToolConfig(
        mcp_group="junos", gate_fn="gate", mcp_tool_name="execute_junos_command"
    )
    generator = module.h_gated_mcp_tool(config, Builder())
    with pytest.raises(ValueError, match="publicly included"):
        await anext(generator)


@pytest.mark.asyncio
async def test_chat_cycle_keeps_hot_context_across_turns(register_module, monkeypatch):
    module = sys.modules["nat.plugins.h_orchestrator.chat_cycle"]
    payloads = {}
    index = []

    class Redis:
        async def zrevrange(self, key, start, end):
            return list(reversed(index))

        async def mget(self, *keys):
            return [payloads[key] for key in keys]

        async def aclose(self):
            pass

    redis = Redis()

    class Store:
        def __init__(self, **kwargs):
            pass

        async def write_turn(
            self, chat_id, role, content, ttl_seconds, *, hot_keep_count
        ):
            import json

            key = f"turn:{len(index)}"
            payloads[key] = json.dumps({"role": role, "content": content})
            index.append(key)
            return key

    prompts = []

    class Dispatcher:
        async def ainvoke(self, prompt, to_type):
            prompts.append(prompt)
            return "first answer" if len(prompts) == 1 else "second answer"

    class Builder:
        async def get_function(self, name):
            return Dispatcher()

    monkeypatch.setattr(
        module.aioredis,
        "Redis",
        SimpleNamespace(from_url=lambda *args, **kwargs: redis),
    )
    monkeypatch.setattr(module, "BoundedBufferStore", Store)
    config = module.HChatCycleConfig(
        dispatcher="agent", chat_id="chat", pod="pod", agent="agent"
    )
    generator = module.h_chat_cycle(config, Builder())
    function_info = await anext(generator)
    await function_info.function(module.HChatCycleInput(message="first question"))
    await function_info.function(module.HChatCycleInput(message="second question"))
    assert prompts == [
        "first question",
        "Previous conversation:\n"
        "[user] first question\n"
        "[assistant] first answer\n\n"
        "Current message:\nsecond question\n",
    ]
    await generator.aclose()
