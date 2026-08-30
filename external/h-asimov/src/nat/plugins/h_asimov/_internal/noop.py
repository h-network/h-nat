"""No-op firewall — always-ALLOW.

Ported from h-network-asimov-firewall/source/noop.py (commit
bcb4e374) — see LLD.md §2.4, §4. Behaviour unchanged; `from_env` is
dropped since selection is a config field (`mode: noop`) here, not an
env var.

Use cases:
- Dev/test deployments where the LLM judge is overhead, not value.
- Deployments with their own external safety layer.
- Benchmarking without gate latency in the way.

Audit posture: emits a single `gate_skipped` event per call so the
absence of a real gate decision is itself an observable event.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from .firewall import Decision, Verdict

T = TypeVar("T")

EV_GATE_SKIPPED = "gate_skipped"


class NoopFirewall:
    """Firewall that always ALLOWs and runs the executor directly.

    Behaviour:
    - emits `gate_skipped` event with `reason=firewall=noop`
    - awaits `execute()` and returns its result
    - executor exceptions propagate (same contract as AsimovFirewall)
    - returns `Decision(verdict=ALLOW, rule_id=None)`
    """

    async def evaluate(
        self,
        *,
        command: str,
        task_id: str,
        execute: Callable[[], Awaitable[T]],
        emit_event: Callable[[str, dict], Awaitable[None]] | None = None,
        model_name: str | None = None,
    ) -> tuple[Decision, T | None]:
        del command, task_id, model_name  # unused — noop has no judge
        if emit_event is not None:
            await emit_event(EV_GATE_SKIPPED, {"reason": "firewall=noop"})
        result = await execute()
        return Decision(verdict=Verdict.ALLOW), result
