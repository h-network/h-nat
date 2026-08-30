"""NoopFirewall — always-ALLOW variant.

Ported from h-network-asimov-firewall/tests/test_noop_firewall.py
(commit bcb4e374), keeping only the `NoopFirewall.evaluate` behaviour
tests. The predecessor's `firewall_from_env`/`VALID_FIREWALL_KINDS`
env-based selection and its dispatcher-integration tests (already
skip-marked as "requires bridge dispatcher; out of repo scope" in the
predecessor) have no equivalent here: selection is the `mode` config
field on `AsimovGateConfig` (see `test_register.py`), not an env var,
and there's no bridge dispatcher in this repo to integrate against.
"""
from __future__ import annotations

import pytest
from nat.plugins.h_asimov._internal.firewall import Verdict
from nat.plugins.h_asimov._internal.noop import EV_GATE_SKIPPED, NoopFirewall


@pytest.mark.asyncio
async def test_noop_evaluate_allows_and_runs_executor() -> None:
    fw = NoopFirewall()

    async def _executor() -> str:
        return "executor-ran"

    decision, result = await fw.evaluate(
        command="rm -rf /",  # the noop firewall has no judge — proves it
        task_id="t-1",
        execute=_executor,
    )
    assert decision.verdict == Verdict.ALLOW
    assert decision.rule_id is None
    assert result == "executor-ran"


@pytest.mark.asyncio
async def test_noop_evaluate_emits_single_gate_skipped_event() -> None:
    fw = NoopFirewall()
    events: list[tuple[str, dict]] = []

    async def _emit(event: str, data: dict) -> None:
        events.append((event, data))

    async def _executor() -> int:
        return 42

    await fw.evaluate(command="anything", task_id="t-1", execute=_executor, emit_event=_emit)
    assert events == [(EV_GATE_SKIPPED, {"reason": "firewall=noop"})]


@pytest.mark.asyncio
async def test_noop_evaluate_propagates_executor_exceptions() -> None:
    fw = NoopFirewall()

    class _BoomError(Exception):
        pass

    async def _executor() -> None:
        raise _BoomError("kaboom")

    with pytest.raises(_BoomError, match="kaboom"):
        await fw.evaluate(command="x", task_id="t-1", execute=_executor)


@pytest.mark.asyncio
async def test_noop_evaluate_no_emit_event_callback_no_crash() -> None:
    fw = NoopFirewall()

    async def _executor() -> str:
        return "ok"

    decision, result = await fw.evaluate(
        command="x", task_id="t-1", execute=_executor, emit_event=None
    )
    assert decision.verdict == Verdict.ALLOW
    assert result == "ok"
