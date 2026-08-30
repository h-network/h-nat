"""Layer 2 Asimov LLM judge — parser + call behaviour.

The parsing tests are ported verbatim in intent from
h-network-asimov-firewall/tests/test_asimov.py (commit bcb4e374),
adapted to call `_parse_verdict` with plain text instead of a raw HTTP
JSON body — NAT's LLM abstraction hands back parsed message content
directly (see LLD.md §4). The `from_env`/`_call`-short-circuit tests
have no equivalent here: there's no env-var configuration or raw HTTP
transport left to test once the LLM call is NAT-managed; those
behaviours are replaced by `evaluate()`-level tests against a fake
LLM client.
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import FakeLLM
from nat.plugins.h_asimov._internal.asimov import Asimov, _parse_verdict

# ---- Parser (ported verbatim in intent) ----


def test_parse_allow() -> None:
    out = _parse_verdict("ALLOW")
    assert out.verdict == "ALLOW"
    assert out.reason is None


def test_parse_allow_with_trailing_comment() -> None:
    out = _parse_verdict("ALLOW   looks fine")
    assert out.verdict == "ALLOW"


def test_parse_deny_with_colon_reason() -> None:
    out = _parse_verdict("DENY: writes to /etc")
    assert out.verdict == "DENY"
    assert out.reason == "writes to /etc"


def test_parse_deny_no_colon() -> None:
    out = _parse_verdict("DENY")
    assert out.verdict == "DENY"
    assert out.reason == "(no reason given)"


def test_parse_deny_colon_no_reason() -> None:
    out = _parse_verdict("DENY:   ")
    assert out.verdict == "DENY"
    assert out.reason == "(no reason given)"


def test_parse_unparseable_is_error_not_deny() -> None:
    """Critical: parse failure != DENY."""
    out = _parse_verdict("¯\\_(ツ)_/¯")
    assert out.verdict == "ERROR"


def test_parse_first_non_empty_line_only() -> None:
    out = _parse_verdict("\n\n  \nALLOW\nyada yada")
    assert out.verdict == "ALLOW"


# ---- Asimov.evaluate() against a fake LLM client ----


@pytest.mark.asyncio
async def test_evaluate_allow() -> None:
    llm = FakeLLM(responses=["ALLOW"])
    a = Asimov(llm=llm, ground_rules="be nice", timeout_sec=1.0)
    out = await a.evaluate("ls -la")
    assert out.verdict == "ALLOW"
    assert "be nice" in llm.calls[0]
    assert "ls -la" in llm.calls[0]


@pytest.mark.asyncio
async def test_evaluate_deny() -> None:
    llm = FakeLLM(responses=["DENY: writes /etc"])
    a = Asimov(llm=llm, ground_rules="be nice", timeout_sec=1.0)
    out = await a.evaluate("rm /etc/passwd")
    assert out.verdict == "DENY"
    assert out.reason == "writes /etc"


@pytest.mark.asyncio
async def test_evaluate_error_when_llm_raises() -> None:
    llm = FakeLLM(raises=RuntimeError("endpoint down"))
    a = Asimov(llm=llm, ground_rules="be nice", timeout_sec=1.0)
    out = await a.evaluate("ls")
    assert out.verdict == "ERROR"
    assert "endpoint down" in (out.reason or "")


@pytest.mark.asyncio
async def test_evaluate_error_on_timeout() -> None:
    class _HangingLLM:
        async def ainvoke(self, prompt: str):
            await asyncio.sleep(10)

    a = Asimov(llm=_HangingLLM(), ground_rules="be nice", timeout_sec=0.05)
    out = await a.evaluate("ls")
    assert out.verdict == "ERROR"
    assert "timed out" in (out.reason or "")


@pytest.mark.asyncio
async def test_evaluate_error_without_ground_rules() -> None:
    llm = FakeLLM(responses=["ALLOW"])
    a = Asimov(llm=llm, ground_rules="   ", timeout_sec=1.0)
    out = await a.evaluate("ls")
    assert out.verdict == "ERROR"
    assert "ground rules" in (out.reason or "")
    assert llm.calls == []  # short-circuited before ever calling the LLM


@pytest.mark.asyncio
async def test_evaluate_error_on_empty_response() -> None:
    llm = FakeLLM(responses=["   "])
    a = Asimov(llm=llm, ground_rules="be nice", timeout_sec=1.0)
    out = await a.evaluate("ls")
    assert out.verdict == "ERROR"
    assert "empty" in (out.reason or "")
