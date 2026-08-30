"""Shared test stubs.

`FakeAsimov` / `FakeDenylist` are carried over from the predecessor's
`tests/conftest.py` (h-network-asimov-firewall, commit bcb4e374) for
`test_firewall.py`. `FakeLLM` and `FakeBuilder` are new: they stand in
for NAT's LLM client and `Builder` so `_internal/asimov.py` and
`register.py` are testable without a network dependency or a full NAT
runtime.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class FakeAsimov:
    """Stub Layer-2 returning canned outcomes."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._queue = list(outcomes)

    async def evaluate(self, command: str):
        return self._queue.pop(0)


class FakeDenylist:
    """Stub Layer-1 returning a fixed result."""

    def __init__(self, hit: Any | None = None) -> None:
        self._hit = hit

    def check(self, command: str):
        return self._hit


class FakeLLM:
    """Stub NAT LLM client — mimics a langchain `BaseChatModel`'s
    `ainvoke`, returning an object with a `.content` attribute.
    """

    def __init__(self, responses: list[str] | None = None, *, raises: Exception | None = None) -> None:
        self._responses = list(responses or [])
        self._raises = raises
        self.calls: list[str] = []

    async def ainvoke(self, prompt: str):
        self.calls.append(prompt)
        if self._raises is not None:
            raise self._raises
        text = self._responses.pop(0) if self._responses else "ALLOW"
        return SimpleNamespace(content=text)


class FakeBuilder:
    """Stub `nat.builder.builder.Builder` — only `get_llm` is used by
    `register.py`."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    async def get_llm(self, llm_name, wrapper_type):
        del llm_name, wrapper_type
        return self._llm
