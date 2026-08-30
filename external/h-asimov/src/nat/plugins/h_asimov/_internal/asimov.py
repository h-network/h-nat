"""Layer 2: Asimov LLM judge.

Ported + reworked from h-network-asimov-firewall/source/asimov.py
(commit bcb4e374) — see LLD.md §2.3, §4. The prompt template and the
three-way ALLOW/DENY/ERROR parsing rules (`_parse_verdict`) carry over
unchanged. The transport does not: the predecessor made a raw
`urllib.request` HTTP call built from env vars; this port calls
through an injected NAT LLM client (obtained via
`Builder.get_llm(..., wrapper_type=LLMFrameworkEnum.LANGCHAIN)` in
register.py) so any NAT-registered LLM backend works.

Stateless: one call per `evaluate(command)`, ground-rules only, no
conversation history — the independent-context judge property.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT_SEC = 30.0

ASIMOV_PROMPT_TEMPLATE = """You are an independent safety gate evaluating a sandbox-execution action.
Apply only these ground rules — no other context.

{rules}

Action requested:
{command}

Respond with EXACTLY one line:
ALLOW
DENY: <one-line reason>"""


@dataclass(frozen=True)
class AsimovOutcome:
    """Three-way outcome of the gate.

    `verdict ∈ {"ALLOW", "DENY", "ERROR"}`. On `DENY`, `reason` carries
    the parsed text. On `ERROR`, `reason` carries a message describing
    the failure (no raw exception object, no stack trace).
    """

    verdict: str
    reason: str | None


def _parse_verdict(text: str) -> AsimovOutcome:
    """Parse the judge's response text.

    Ported verbatim from the predecessor's `_parse_verdict`, adapted
    to take already-extracted message text instead of a raw HTTP JSON
    body — NAT's LLM abstraction hands back parsed content directly,
    so there's no `{"choices": [...]}` envelope to unwrap here. Three
    accepted DENY shapes (`DENY:`, `DENY` alone, `DENY: <reason>`);
    ALLOW permits a trailing space-prefixed comment but no other
    suffix; anything else is ERROR (parse failure != DENY).
    """
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    upper = first_line.upper()
    if upper == "ALLOW" or upper.startswith("ALLOW "):
        return AsimovOutcome(verdict="ALLOW", reason=None)
    if upper.startswith("DENY:"):
        reason = first_line[5:].strip() or "(no reason given)"
        return AsimovOutcome(verdict="DENY", reason=reason)
    if upper == "DENY":
        return AsimovOutcome(verdict="DENY", reason="(no reason given)")
    return AsimovOutcome(verdict="ERROR", reason=f"unparseable: {first_line[:80]!r}")


class Asimov:
    """Stateless gate. Calls the injected NAT LLM client with the
    ground rules and command, no history. Never raises on judge
    failure — a call that errors, times out, or returns something
    unparseable always resolves to `AsimovOutcome(verdict="ERROR")`.
    """

    def __init__(self, *, llm: Any, ground_rules: str, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> None:
        self._llm = llm
        self._ground_rules = ground_rules
        self._timeout_sec = timeout_sec

    async def evaluate(self, command: str) -> AsimovOutcome:
        if not self._ground_rules.strip():
            return AsimovOutcome(verdict="ERROR", reason="no ground rules loaded")

        prompt = ASIMOV_PROMPT_TEMPLATE.format(rules=self._ground_rules, command=command)

        try:
            response = await asyncio.wait_for(self._llm.ainvoke(prompt), timeout=self._timeout_sec)
        except TimeoutError:
            return AsimovOutcome(
                verdict="ERROR", reason=f"judge call timed out after {self._timeout_sec}s"
            )
        except Exception as exc:  # noqa: BLE001 - any transport/model failure is a judge ERROR
            return AsimovOutcome(verdict="ERROR", reason=f"raised: {type(exc).__name__}: {exc}")

        text = response.content if hasattr(response, "content") else str(response)
        if not isinstance(text, str) or not text.strip():
            return AsimovOutcome(verdict="ERROR", reason="empty judge response")

        return _parse_verdict(text)
