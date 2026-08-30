"""Asimov firewall — top-level evaluator.

Ported from h-network-asimov-firewall/source/firewall.py (commit
bcb4e374) — see LLD.md §2.1, §4. Logic is unchanged; the only removal
is the env-driven `from_env` constructor, replaced by NAT's
config-driven construction in register.py.

Independent-context LLM judge with no conversation history, evaluated
against a single ground-rules document at call time. Layer 1
(denylist) -> Layer 2 (Asimov LLM) -> execute callback.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeVar

from .asimov import Asimov, AsimovOutcome
from .denylist import Denylist

T = TypeVar("T")

RULE_LAYER1_DENYLIST = "layer1.denylist"
RULE_LAYER2_ASIMOV = "layer2.asimov"

EV_DENYLIST_BLOCK = "denylist_block"
EV_ASIMOV_ALLOW = "asimov_allow"
EV_ASIMOV_DENY = "asimov_deny"
EV_ASIMOV_ERROR_CONTINUING = "asimov_error_continuing"
EV_ASIMOV_ERROR_FAILING = "asimov_error_failing"
EV_GATE_ALLOW_STARTING = "gate_allow_starting"

_BRIEF_MAX = 200


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True)
class Decision:
    """Verdict + structured detail.

    `rule_id` and `brief` are populated only when `verdict == DENY`
    AND the deny was rule-based (Layer 1 hit or Layer 2 DENY). The
    fail-closed Asimov-LLM-error case sets `verdict=DENY` with
    `rule_id=None` — the caller maps that to a gate-internal-error
    condition, NOT a rule-based denial.
    """

    verdict: Verdict
    rule_id: str | None = None
    brief: str | None = None
    # Set on the gate-internal-error path only. Bounded so a verbose
    # judge/endpoint exception can't bloat the caller's payload.
    gate_error_message: str | None = None


def _sanitize_brief(text: str) -> str:
    """Truncate to 200 chars; strip control chars except space."""
    cleaned = "".join(ch for ch in text if ch == " " or ch.isprintable())
    if len(cleaned) > _BRIEF_MAX:
        return cleaned[:_BRIEF_MAX]
    return cleaned


def _sanitize_message(text: str) -> str:
    """Same shape as `_sanitize_brief` — keeps gate error messages
    bounded so a verbose endpoint exception can't bloat the envelope."""
    return _sanitize_brief(text)


class Firewall(Protocol):
    """Gate implementations satisfy this Protocol. `AsimovFirewall`
    (L1 denylist + L2 LLM judge) is the default; `NoopFirewall`
    (always-ALLOW) is provided for dev/test deployments and operators
    running their own safety layer.

    Contracts every implementation honours:
    - Never raise on gate failure. Return a ``Decision`` describing
      DENY (with a populated ``rule_id`` for rule-based denials, or
      ``rule_id=None`` for gate-internal errors).
    - Executor exceptions propagate to the caller. Don't swallow them.
    - When the gate ALLOWs, await ``execute()`` and return its result
      in the second tuple slot.
    - When provided, ``emit_event(name, data)`` should be invoked at
      phase boundaries so the audit trail reflects gate decisions —
      including "deliberately skipped" (NoopFirewall) so the absence
      of a real gate decision is itself an observable event.
    """

    async def evaluate(
        self,
        *,
        command: str,
        task_id: str,
        execute: Callable[[], Awaitable[T]],
        emit_event: Callable[[str, dict], Awaitable[None]] | None = None,
        model_name: str | None = None,
    ) -> tuple[Decision, T | None]: ...


class AsimovFirewall:
    """Two-layer gate. Construction is dependency-injected for
    testability; register.py is the production factory.
    """

    def __init__(
        self,
        *,
        denylist: Denylist,
        asimov: Asimov,
        fail_open: bool,
    ) -> None:
        self._denylist = denylist
        self._asimov = asimov
        self._fail_open = fail_open

    async def evaluate(
        self,
        *,
        command: str,
        task_id: str,
        execute: Callable[[], Awaitable[T]],
        emit_event: Callable[[str, dict], Awaitable[None]] | None = None,
        model_name: str | None = None,
    ) -> tuple[Decision, T | None]:
        """Run Layer 1 then Layer 2; on full ALLOW, await `execute()`.

        Never raises on gate failure. Executor exceptions propagate to
        the caller.
        """

        async def _emit(event: str, data: dict) -> None:
            if emit_event is not None:
                await emit_event(event, data)

        # Layer 1: pattern denylist.
        hit = self._denylist.check(command)
        if hit is not None:
            await _emit(
                EV_DENYLIST_BLOCK,
                {"rule_id": RULE_LAYER1_DENYLIST, "matched": hit.pattern_name},
            )
            return (
                Decision(
                    verdict=Verdict.DENY,
                    rule_id=RULE_LAYER1_DENYLIST,
                    brief=_sanitize_brief(
                        f"matched pattern '{hit.pattern_name}'"
                    ),
                ),
                None,
            )

        # Layer 2: Asimov LLM judge.
        t0 = time.monotonic()
        outcome: AsimovOutcome = await self._asimov.evaluate(command)
        latency_ms = max(0, int((time.monotonic() - t0) * 1000))

        if outcome.verdict == "ALLOW":
            await _emit(
                EV_ASIMOV_ALLOW,
                {
                    "rule_refs": [],
                    "model": model_name or "",
                    "latency_ms": latency_ms,
                },
            )
        elif outcome.verdict == "DENY":
            reason = outcome.reason or "(no reason given)"
            await _emit(
                EV_ASIMOV_DENY,
                {
                    "rule_refs": [],
                    "model": model_name or "",
                    "reason": _sanitize_brief(reason),
                    "latency_ms": latency_ms,
                },
            )
            return (
                Decision(
                    verdict=Verdict.DENY,
                    rule_id=RULE_LAYER2_ASIMOV,
                    brief=_sanitize_brief(reason),
                ),
                None,
            )
        else:  # ERROR
            sanitized = _sanitize_message(outcome.reason or "asimov endpoint error")
            if self._fail_open:
                await _emit(
                    EV_ASIMOV_ERROR_CONTINUING,
                    {
                        "stage": "asimov_call",
                        "message": sanitized,
                        "fallback": "allow",
                        "latency_ms": latency_ms,
                    },
                )
                # Fall through to execute.
            else:
                await _emit(
                    EV_ASIMOV_ERROR_FAILING,
                    {
                        "stage": "asimov_call",
                        "message": sanitized,
                        "fallback": "deny",
                        "latency_ms": latency_ms,
                    },
                )
                return (
                    Decision(
                        verdict=Verdict.DENY,
                        rule_id=None,  # gate produced no verdict
                        brief=None,
                        gate_error_message=sanitized,
                    ),
                    None,
                )

        # Gate cleared — about to execute.
        await _emit(EV_GATE_ALLOW_STARTING, {})
        result = await execute()
        return Decision(verdict=Verdict.ALLOW), result
