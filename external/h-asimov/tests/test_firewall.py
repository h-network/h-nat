"""Firewall orchestrator: Layer 1 -> Layer 2 -> execute callback.

Ported from h-network-asimov-firewall/tests/test_firewall.py (commit
bcb4e374), import paths updated for this package's layout. Verifies
the same contract:
- Layer 1 -> Layer 2 -> execute order
- Events at each phase boundary
- Fail-closed posture sets rule_id=None (gate-internal-error), not a
  rule-based denial
- Fail-open posture proceeds; emits asimov_error_continuing
"""
from __future__ import annotations

import pytest

from conftest import FakeAsimov, FakeDenylist
from nat.plugins.h_asimov._internal.asimov import AsimovOutcome
from nat.plugins.h_asimov._internal.denylist import DenylistHit
from nat.plugins.h_asimov._internal.firewall import (
    RULE_LAYER1_DENYLIST,
    RULE_LAYER2_ASIMOV,
    AsimovFirewall,
    Verdict,
)
from nat.plugins.h_asimov._internal.firewall import (
    EV_ASIMOV_ALLOW,
    EV_ASIMOV_DENY,
    EV_ASIMOV_ERROR_CONTINUING,
    EV_ASIMOV_ERROR_FAILING,
    EV_DENYLIST_BLOCK,
    EV_GATE_ALLOW_STARTING,
)


@pytest.fixture
def emitter():
    """Records `(event_name, data)` pairs."""
    events: list[tuple[str, dict]] = []

    async def _emit(event: str, data: dict) -> None:
        events.append((event, data))

    return events, _emit


# ---- Layer 1 hit ---------------------------------------------------------


@pytest.mark.asyncio
async def test_layer1_hit_short_circuits_without_executing(emitter) -> None:
    events, emit = emitter
    fw = AsimovFirewall(
        denylist=FakeDenylist(hit=DenylistHit(pattern_name="| bash")),
        asimov=FakeAsimov(outcomes=[]),
        fail_open=False,
    )
    executed = False

    async def _execute() -> str:
        nonlocal executed
        executed = True
        return "should-not-run"

    decision, result = await fw.evaluate(
        command="echo | bash",
        task_id="t1",
        execute=_execute,
        emit_event=emit,
    )

    assert decision.verdict == Verdict.DENY
    assert decision.rule_id == RULE_LAYER1_DENYLIST
    assert "| bash" in (decision.brief or "")
    assert result is None
    assert executed is False
    assert events == [(EV_DENYLIST_BLOCK, {"rule_id": RULE_LAYER1_DENYLIST, "matched": "| bash"})]


# ---- Layer 2 ALLOW + execute --------------------------------------------


@pytest.mark.asyncio
async def test_layer2_allow_invokes_execute_and_emits_gate_allow_starting(emitter) -> None:
    events, emit = emitter
    fw = AsimovFirewall(
        denylist=FakeDenylist(hit=None),
        asimov=FakeAsimov(outcomes=[AsimovOutcome(verdict="ALLOW", reason=None)]),
        fail_open=False,
    )

    async def _execute() -> str:
        return "ran"

    decision, result = await fw.evaluate(
        command="echo hi",
        task_id="t2",
        execute=_execute,
        emit_event=emit,
        model_name="model-x",
    )

    assert decision.verdict == Verdict.ALLOW
    assert result == "ran"
    seen = [name for name, _ in events]
    assert seen == [EV_ASIMOV_ALLOW, EV_GATE_ALLOW_STARTING]
    assert events[0][1]["model"] == "model-x"


# ---- Layer 2 DENY -------------------------------------------------------


@pytest.mark.asyncio
async def test_layer2_deny_short_circuits_and_carries_reason(emitter) -> None:
    events, emit = emitter
    fw = AsimovFirewall(
        denylist=FakeDenylist(hit=None),
        asimov=FakeAsimov(outcomes=[AsimovOutcome(verdict="DENY", reason="writes /etc")]),
        fail_open=False,
    )
    executed = False

    async def _execute() -> str:
        nonlocal executed
        executed = True
        return "x"

    decision, result = await fw.evaluate(
        command="evil",
        task_id="t3",
        execute=_execute,
        emit_event=emit,
    )

    assert decision.verdict == Verdict.DENY
    assert decision.rule_id == RULE_LAYER2_ASIMOV
    assert decision.brief == "writes /etc"
    assert result is None
    assert executed is False
    assert any(name == EV_ASIMOV_DENY for name, _ in events)


# ---- Layer 2 ERROR — fail-closed (default) -----------------------------


@pytest.mark.asyncio
async def test_layer2_error_fail_closed_sets_gate_internal_error(emitter) -> None:
    """Critical contract: fail-closed Asimov-LLM-error -> DENY with
    rule_id=None (caller maps to gate_internal_error, NOT firewall_denied).
    """
    events, emit = emitter
    fw = AsimovFirewall(
        denylist=FakeDenylist(hit=None),
        asimov=FakeAsimov(outcomes=[AsimovOutcome(verdict="ERROR", reason="endpoint down")]),
        fail_open=False,
    )

    async def _execute() -> str:
        return "x"

    decision, result = await fw.evaluate(
        command="ls",
        task_id="t4",
        execute=_execute,
        emit_event=emit,
    )

    assert decision.verdict == Verdict.DENY
    assert decision.rule_id is None
    assert decision.brief is None
    assert decision.gate_error_message is not None
    assert "endpoint down" in decision.gate_error_message
    assert result is None
    assert any(name == EV_ASIMOV_ERROR_FAILING for name, _ in events)


# ---- Layer 2 ERROR — fail-open override --------------------------------


@pytest.mark.asyncio
async def test_layer2_error_fail_open_proceeds_to_execute(emitter) -> None:
    events, emit = emitter
    fw = AsimovFirewall(
        denylist=FakeDenylist(hit=None),
        asimov=FakeAsimov(outcomes=[AsimovOutcome(verdict="ERROR", reason="endpoint down")]),
        fail_open=True,
    )

    async def _execute() -> str:
        return "ran"

    decision, result = await fw.evaluate(
        command="ls",
        task_id="t5",
        execute=_execute,
        emit_event=emit,
    )

    assert decision.verdict == Verdict.ALLOW
    assert result == "ran"
    seen = [name for name, _ in events]
    assert seen == [EV_ASIMOV_ERROR_CONTINUING, EV_GATE_ALLOW_STARTING]


# ---- Sanitization --------------------------------------------------------


@pytest.mark.asyncio
async def test_brief_truncates_long_deny_reason(emitter) -> None:
    events, emit = emitter
    long = "x" * 500
    fw = AsimovFirewall(
        denylist=FakeDenylist(hit=None),
        asimov=FakeAsimov(outcomes=[AsimovOutcome(verdict="DENY", reason=long)]),
        fail_open=False,
    )

    async def _execute() -> str:
        return "x"

    decision, _ = await fw.evaluate(command="x", task_id="t6", execute=_execute, emit_event=emit)
    assert len(decision.brief or "") <= 200


# ---- Closure-bound execute is the only gateway path -------------------


@pytest.mark.asyncio
async def test_execute_closure_is_only_invoked_on_full_allow() -> None:
    """The structural invariant: execute is called iff Layer 1 + Layer 2
    both clear."""
    invocations: list[str] = []

    async def _execute() -> str:
        invocations.append("called")
        return "ok"

    fw1 = AsimovFirewall(
        denylist=FakeDenylist(hit=DenylistHit(pattern_name="bad")),
        asimov=FakeAsimov(outcomes=[]),
        fail_open=False,
    )
    await fw1.evaluate(command="x", task_id="a", execute=_execute)
    assert invocations == []

    fw2 = AsimovFirewall(
        denylist=FakeDenylist(hit=None),
        asimov=FakeAsimov(outcomes=[AsimovOutcome(verdict="DENY", reason="r")]),
        fail_open=False,
    )
    await fw2.evaluate(command="x", task_id="b", execute=_execute)
    assert invocations == []

    fw3 = AsimovFirewall(
        denylist=FakeDenylist(hit=None),
        asimov=FakeAsimov(outcomes=[AsimovOutcome(verdict="ALLOW", reason=None)]),
        fail_open=False,
    )
    await fw3.evaluate(command="x", task_id="c", execute=_execute)
    assert invocations == ["called"]
