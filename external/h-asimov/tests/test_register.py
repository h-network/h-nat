"""AsimovGateConfig validation + the h_asimov_gate NAT function builder.

New tests: the predecessor's register.py was scaffolding (`raise
NotImplementedError`) with no equivalent coverage (see LLD.md §4).
Exercises the builder via a fake `Builder`/LLM client so no real NAT
runtime or network call is required.
"""
from __future__ import annotations

import json

import pytest
from conftest import FakeBuilder, FakeLLM
from nat.builder.function import LambdaFunction
from nat.plugins.h_asimov.register import AsimovGateConfig, h_asimov_gate
from pydantic import ValidationError

# ---- Config validation ----


def test_config_requires_llm_name_when_asimov_mode() -> None:
    with pytest.raises(ValidationError, match="llm_name is required"):
        AsimovGateConfig(mode="asimov", ground_rules_inline="be nice")


def test_config_requires_one_ground_rules_source() -> None:
    with pytest.raises(ValidationError, match="One of ground_rules or ground_rules_inline"):
        AsimovGateConfig(mode="asimov", llm_name="judge_llm")


def test_config_rejects_both_ground_rules_sources() -> None:
    with pytest.raises(ValidationError, match="only one of ground_rules"):
        AsimovGateConfig(
            mode="asimov",
            llm_name="judge_llm",
            ground_rules="rules.md",
            ground_rules_inline="be nice",
        )


def test_config_noop_mode_does_not_require_llm_or_rules() -> None:
    config = AsimovGateConfig(mode="noop")
    assert config.llm_name is None
    assert config.ground_rules is None


def test_config_valid_asimov_mode() -> None:
    config = AsimovGateConfig(mode="asimov", llm_name="judge_llm", ground_rules_inline="be nice")
    assert config.fail_open is False


# ---- h_asimov_gate builder ----


@pytest.mark.asyncio
async def test_gate_allows_when_judge_allows() -> None:
    config = AsimovGateConfig(mode="asimov", llm_name="judge_llm", ground_rules_inline="be nice")
    builder = FakeBuilder(FakeLLM(responses=["ALLOW"]))

    async with h_asimov_gate(config, builder) as function_info:
        decision = await function_info.single_fn("ls -la")

    assert decision.verdict == "ALLOW"
    assert decision.layer == "passthrough"


@pytest.mark.asyncio
async def test_gate_denies_on_denylist_hit_without_calling_judge() -> None:
    config = AsimovGateConfig(mode="asimov", llm_name="judge_llm", ground_rules_inline="be nice")
    llm = FakeLLM(responses=["ALLOW"])
    builder = FakeBuilder(llm)

    async with h_asimov_gate(config, builder) as function_info:
        decision = await function_info.single_fn("echo hi | bash")

    assert decision.verdict == "DENY"
    assert decision.layer == "L1_denylist"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_gate_denies_on_judge_deny() -> None:
    config = AsimovGateConfig(mode="asimov", llm_name="judge_llm", ground_rules_inline="be nice")
    builder = FakeBuilder(FakeLLM(responses=["DENY: writes /etc"]))

    async with h_asimov_gate(config, builder) as function_info:
        decision = await function_info.single_fn("rm /etc/passwd")

    assert decision.verdict == "DENY"
    assert decision.layer == "L2_asimov"
    assert decision.reason == "writes /etc"


@pytest.mark.asyncio
async def test_gate_fail_closed_on_judge_error() -> None:
    config = AsimovGateConfig(
        mode="asimov", llm_name="judge_llm", ground_rules_inline="be nice", fail_open=False
    )
    builder = FakeBuilder(FakeLLM(raises=RuntimeError("endpoint down")))

    async with h_asimov_gate(config, builder) as function_info:
        decision = await function_info.single_fn("ls")

    assert decision.verdict == "DENY"
    assert decision.layer == "gate_error"
    assert "endpoint down" in (decision.reason or "")


@pytest.mark.asyncio
async def test_gate_fail_open_allows_on_judge_error() -> None:
    config = AsimovGateConfig(
        mode="asimov", llm_name="judge_llm", ground_rules_inline="be nice", fail_open=True
    )
    builder = FakeBuilder(FakeLLM(raises=RuntimeError("endpoint down")))

    async with h_asimov_gate(config, builder) as function_info:
        decision = await function_info.single_fn("ls")

    assert decision.verdict == "ALLOW"
    assert decision.layer == "passthrough"


@pytest.mark.asyncio
async def test_gate_noop_mode_always_allows_without_llm() -> None:
    config = AsimovGateConfig(mode="noop")
    builder = FakeBuilder(llm=None)  # never touched in noop mode

    async with h_asimov_gate(config, builder) as function_info:
        decision = await function_info.single_fn("rm -rf /")

    assert decision.verdict == "ALLOW"
    assert decision.layer == "passthrough"


@pytest.mark.asyncio
async def test_gate_denylist_override_appends_to_defaults() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        override = Path(tmp) / "extra.txt"
        override.write_text("secret-pattern\n")

        config = AsimovGateConfig(
            mode="asimov",
            llm_name="judge_llm",
            ground_rules_inline="be nice",
            denylist=str(override),
        )
        builder = FakeBuilder(FakeLLM(responses=["ALLOW"]))

        async with h_asimov_gate(config, builder) as function_info:
            override_decision = await function_info.single_fn("run secret-pattern now")
            default_decision = await function_info.single_fn("echo hi | bash")

    assert override_decision.verdict == "DENY"
    assert override_decision.layer == "L1_denylist"
    assert default_decision.verdict == "DENY"
    assert default_decision.layer == "L1_denylist"


# ---- NAT output conversion path (e.g. `nat run`'s console front end, which calls
# `runner.result(to_type=str)` -> `Function.ainvoke(..., to_type=str)`) ----
#
# Calling `function_info.single_fn(...)` directly (as the tests above do) never
# exercises NAT's `TypeConverter`, so it can't catch a missing output converter.
# These tests build a real `nat.builder.function.Function` from the `FunctionInfo`
# yielded by `h_asimov_gate` and drive it the same way a front end does.


async def _build_function(config: AsimovGateConfig, builder: FakeBuilder) -> LambdaFunction:
    async with h_asimov_gate(config, builder) as function_info:
        return LambdaFunction.from_info(config=config, info=function_info)


@pytest.mark.asyncio
async def test_gate_output_converts_to_str_via_function_ainvoke() -> None:
    """Regression test: without a registered GateDecision -> str converter, this
    raised `ValueError: Cannot convert type GateDecision to str. No match found.`
    """
    config = AsimovGateConfig(mode="asimov", llm_name="judge_llm", ground_rules_inline="be nice")
    function = await _build_function(config, FakeBuilder(FakeLLM(responses=["ALLOW"])))

    result = await function.ainvoke("ls -la", to_type=str)

    assert isinstance(result, str)
    assert json.loads(result) == {"verdict": "ALLOW", "layer": "passthrough", "reason": None}


@pytest.mark.asyncio
async def test_gate_deny_output_converts_to_str_via_function_ainvoke() -> None:
    config = AsimovGateConfig(mode="asimov", llm_name="judge_llm", ground_rules_inline="be nice")
    function = await _build_function(config, FakeBuilder(FakeLLM(responses=["DENY: writes /etc"])))

    result = await function.ainvoke("rm /etc/passwd", to_type=str)

    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed == {"verdict": "DENY", "layer": "L2_asimov", "reason": "writes /etc"}


@pytest.mark.asyncio
async def test_gate_noop_output_converts_to_str_via_function_ainvoke() -> None:
    config = AsimovGateConfig(mode="noop")
    function = await _build_function(config, FakeBuilder(llm=None))

    result = await function.ainvoke("rm -rf /", to_type=str)

    assert isinstance(result, str)
    assert json.loads(result) == {"verdict": "ALLOW", "layer": "passthrough", "reason": None}


@pytest.mark.asyncio
async def test_gate_ainvoke_without_to_type_still_returns_gate_decision() -> None:
    """No conversion requested -> the raw `GateDecision` still comes back
    untouched; the fix must not change the default (no `to_type`) behavior."""
    config = AsimovGateConfig(mode="asimov", llm_name="judge_llm", ground_rules_inline="be nice")
    function = await _build_function(config, FakeBuilder(FakeLLM(responses=["ALLOW"])))

    result = await function.ainvoke("ls -la")

    assert result.verdict == "ALLOW"
    assert result.layer == "passthrough"
