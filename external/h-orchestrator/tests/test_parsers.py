from dataclasses import dataclass

import pytest
from nat.plugins.h_orchestrator.parsers import get_parser, known_parsers
from nat.plugins.h_orchestrator.parsers.claude_json import ClaudeJsonParser
from nat.plugins.h_orchestrator.parsers.raw import RawParser


@dataclass
class Result:
    exit_code: int
    stdout_text: str = ""
    stderr_text: str = ""


class Client:
    def __init__(self, result):
        self.result = result
        self.call = None

    async def exec(self, sandbox, command, *, stdin, rpc_timeout):
        self.call = (sandbox, command, stdin, rpc_timeout)
        return self.result


@pytest.mark.asyncio
async def test_raw_parser_preserves_stdout_and_exec_contract():
    client = Client(Result(0, stdout_text="ok\n"))
    parsed = await RawParser().consume(client, "box", b"script", 3.5, None)
    assert parsed.ok and parsed.text == "ok\n"
    assert client.call == ("box", ["bash"], b"script", 3.5)


@pytest.mark.asyncio
async def test_raw_parser_returns_stderr_on_failure():
    parsed = await RawParser().consume(Client(Result(7, stderr_text="bad")), "box", b"x", 1, None)
    assert not parsed.ok and parsed.error_message == "bad"


@pytest.mark.asyncio
async def test_claude_parser_uses_last_json_object():
    stdout = 'noise\n{"result":"old"}\ntrace\n{"result":"answer","is_error":false}\n'
    parsed = await ClaudeJsonParser().consume(Client(Result(0, stdout)), "box", b"x", 1, None)
    assert parsed.ok and parsed.text == "answer"
    assert parsed.raw == {"result": "answer", "is_error": False}


@pytest.mark.asyncio
async def test_claude_parser_rejects_missing_envelope():
    parsed = await ClaudeJsonParser().consume(Client(Result(0, "not json")), "box", b"x", 1, None)
    assert not parsed.ok and parsed.error_message == "not json"


def test_builtin_registry():
    assert known_parsers() == ["claude_json", "raw"]
    assert isinstance(get_parser("raw"), RawParser)
    with pytest.raises(KeyError, match="unknown output_parser"):
        get_parser("missing")

