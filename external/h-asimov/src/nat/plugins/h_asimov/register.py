"""NAT registration for ``h_asimov_gate``.

``h_asimov_gate`` is a NAT function — workflow-callable from any agent
toolkit YAML flow under ``_type: h_asimov_gate``. It evaluates a single
command against a configurable ground-rules document and returns a
typed allow/deny decision. It does not execute anything itself: the
caller decides what to do with the verdict (see LLD.md §3 for why this
function is a pure judge rather than one that also runs the gated
action).

### Workflow YAML shape

    llms:
      judge_llm:
        _type: openai
        base_url: ...
        model_name: ...

    functions:
      bgp_gate:
        _type: h_asimov_gate
        llm_name: judge_llm
        ground_rules: defaults/bgp.md
        # ground_rules_inline: "Allowed: ..."
        denylist: defaults/denylist.default.txt
        fail_open: false

    # Opt-out (audited, not silent — see LLD.md §5 for why this
    # `mode` field exists beyond what the original spec docstring
    # showed):
    functions:
      bgp_gate:
        _type: h_asimov_gate
        mode: noop

See LLD.md for the full port history and what carried over from
``h-network-asimov-firewall`` vs. what's new here.

Note: this module intentionally does not use
``from __future__ import annotations``. NAT 1.6+'s ``FunctionInfo.from_fn``
resolves ``_gate``'s type hints via ``typing.get_type_hints()`` at
registration time; deferred (string) annotations raise ``NameError`` on
``GateDecision`` there, since NAT resolves them outside this module's
own execution flow. Non-deferred annotations resolve at module
definition time and avoid this.
"""
import logging
import uuid
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic import model_validator

from nat.builder.builder import Builder
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.component_ref import LLMRef
from nat.data_models.function import FunctionBaseConfig
from pydantic import BaseModel

from ._internal.asimov import Asimov
from ._internal.asimov import DEFAULT_TIMEOUT_SEC
from ._internal.denylist import Denylist
from ._internal.firewall import RULE_LAYER1_DENYLIST
from ._internal.firewall import RULE_LAYER2_ASIMOV
from ._internal.firewall import AsimovFirewall
from ._internal.firewall import Decision
from ._internal.firewall import Firewall
from ._internal.firewall import Verdict
from ._internal.noop import NoopFirewall

logger = logging.getLogger(__name__)

_DEFAULTS_PACKAGE = "nat.plugins.h_asimov.defaults"


class AsimovGateConfig(FunctionBaseConfig, name="h_asimov_gate"):
    """Configuration for the ``h_asimov_gate`` pre-flight safety gate."""

    mode: Literal["asimov", "noop"] = Field(
        default="asimov",
        description=(
            "'asimov': two-layer gate (denylist + LLM judge). 'noop': always ALLOW, "
            "audited opt-out for dev/test deployments or operators running their own "
            "safety layer."
        ),
    )
    llm_name: LLMRef | None = Field(
        default=None,
        description="NAT-registered LLM to use as the Layer 2 judge. Required when mode='asimov'.",
    )
    ground_rules: str | None = Field(
        default=None,
        description=(
            "Path to the ground-rules document the judge evaluates against. Mutually "
            "exclusive with ground_rules_inline. One of the two is required when "
            "mode='asimov'."
        ),
    )
    ground_rules_inline: str | None = Field(
        default=None,
        description="Inline ground-rules text, as an alternative to ground_rules.",
    )
    denylist: str | None = Field(
        default=None,
        description=(
            "Path to additional Layer 1 denylist patterns, one per line. Appended to "
            "the packaged default patterns, never replaces them."
        ),
    )
    fail_open: bool = Field(
        default=False,
        description=(
            "Judge-error posture. False (default): a judge error (unreachable, timeout, "
            "unparseable response) DENYs, fail-closed. True: ALLOWs and continues, with "
            "a distinct audit event marking the fallback. Has no effect on a "
            "judge-produced DENY."
        ),
    )
    timeout_sec: float = Field(
        default=DEFAULT_TIMEOUT_SEC,
        gt=0,
        description="Timeout in seconds for the Layer 2 judge call.",
    )

    @model_validator(mode="after")
    def _validate_asimov_fields(self) -> "AsimovGateConfig":
        if self.mode != "asimov":
            return self
        if self.llm_name is None:
            raise ValueError("llm_name is required when mode='asimov'.")
        if self.ground_rules and self.ground_rules_inline:
            raise ValueError("Set only one of ground_rules or ground_rules_inline, not both.")
        if not self.ground_rules and not self.ground_rules_inline:
            raise ValueError("One of ground_rules or ground_rules_inline is required when mode='asimov'.")
        return self


class GateDecision(BaseModel):
    """Typed allow/deny decision returned by ``h_asimov_gate``.

    `layer` is one of `L1_denylist` / `L2_asimov` / `passthrough`
    (cleared, or noop mode) / `gate_error` (the judge itself failed —
    distinct from a judged deny; see `Decision.rule_id` in
    `_internal/firewall.py`).
    """

    verdict: Literal["ALLOW", "DENY"]
    layer: Literal["L1_denylist", "L2_asimov", "passthrough", "gate_error"]
    reason: str | None = None


def _to_gate_decision(decision: Decision) -> GateDecision:
    if decision.verdict == Verdict.ALLOW:
        return GateDecision(verdict="ALLOW", layer="passthrough", reason=None)
    if decision.rule_id == RULE_LAYER1_DENYLIST:
        return GateDecision(verdict="DENY", layer="L1_denylist", reason=decision.brief)
    if decision.rule_id == RULE_LAYER2_ASIMOV:
        return GateDecision(verdict="DENY", layer="L2_asimov", reason=decision.brief)
    return GateDecision(verdict="DENY", layer="gate_error", reason=decision.gate_error_message)


def _load_ground_rules(config: AsimovGateConfig) -> str:
    if config.ground_rules_inline:
        return config.ground_rules_inline
    assert config.ground_rules is not None  # enforced by _validate_asimov_fields
    path = Path(config.ground_rules)
    if not path.is_file():
        raise RuntimeError(f"ground_rules path not found: {path}")
    return path.read_text(encoding="utf-8")


def _load_denylist(config: AsimovGateConfig) -> Denylist:
    default_text = resources.files(_DEFAULTS_PACKAGE).joinpath("denylist.default.txt").read_text(
        encoding="utf-8"
    )
    return Denylist.from_texts(default_text=default_text, override_path=config.denylist)


async def _build_firewall(config: AsimovGateConfig, builder: Builder) -> Firewall:
    if config.mode == "noop":
        return NoopFirewall()

    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)
    ground_rules = _load_ground_rules(config)
    denylist = _load_denylist(config)
    asimov = Asimov(llm=llm, ground_rules=ground_rules, timeout_sec=config.timeout_sec)
    return AsimovFirewall(denylist=denylist, asimov=asimov, fail_open=config.fail_open)


@register_function(config_type=AsimovGateConfig)
async def h_asimov_gate(config: AsimovGateConfig, builder: Builder):
    firewall = await _build_firewall(config, builder)
    model_name = str(config.llm_name) if config.mode == "asimov" else None

    async def _no_execute() -> None:
        return None

    async def _emit_event(event: str, data: dict) -> None:
        logger.info("h_asimov_gate event=%s data=%s", event, data)

    async def _gate(command: str) -> GateDecision:
        decision, _ = await firewall.evaluate(
            command=command,
            task_id=str(uuid.uuid4()),
            execute=_no_execute,
            emit_event=_emit_event,
            model_name=model_name,
        )
        return _to_gate_decision(decision)

    logger.info(
        "h_asimov_gate name=%s constructed mode=%s fail_open=%s",
        config.name or "h_asimov_gate",
        config.mode,
        config.fail_open,
    )

    yield FunctionInfo.from_fn(
        _gate,
        description=(
            "Pre-flight safety gate: evaluates a command against a denylist and an LLM "
            "judge, returning ALLOW/DENY before anything executes."
        ),
    )
