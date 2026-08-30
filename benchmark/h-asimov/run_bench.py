"""Adversarial benchmark driver for h-asimov.

Runs every case in cases.py against every workflow variant in workflows/
via `nat run`, and classifies each result as correct, a false positive
(expected ALLOW, got DENY -- something safe got blocked), or a false
negative (expected DENY, got ALLOW -- something dangerous got through).
These are reported as distinct categories, not folded into one pass/fail
number -- that distinction is the point of this benchmark.

This is a measurement tool, not a gate: it always exits 0. Read the
summary, especially the false-negative section.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from cases import CASES, Case

HERE = Path(__file__).resolve().parent
WORKFLOWS_DIR = HERE / "workflows"
VARS_FILE = HERE / "vars.yaml"
VARS_EXAMPLE_FILE = HERE / "vars.example.yaml"

ANSI = re.compile(r"\x1b\[[0-9;]*m")
RESULT = re.compile(r"Workflow Result:\s*\n(.*?)\n-{10,}", re.DOTALL)


@dataclass
class CaseResult:
    case: Case
    actual: str | None
    layer: str | None
    reason: str | None
    error: str | None

    @property
    def outcome(self) -> str:
        if self.error is not None:
            return "error"
        if self.actual == self.case.expected:
            return "correct"
        if self.case.expected == "ALLOW" and self.actual == "DENY":
            return "false_positive"
        if self.case.expected == "DENY" and self.actual == "ALLOW":
            return "false_negative"
        return "error"  # pragma: no cover - verdict is always ALLOW/DENY today


def load_vars() -> dict[str, str]:
    if not VARS_FILE.is_file():
        raise SystemExit(
            f"{VARS_FILE} not found. Copy {VARS_EXAMPLE_FILE.name} to "
            f"{VARS_FILE.name} in this directory and fill in real values."
        )
    data = yaml.safe_load(VARS_FILE.read_text(encoding="utf-8"))
    llm = data.get("llm", {})
    missing = [k for k in ("model", "base_url") if not llm.get(k)]
    if missing:
        raise SystemExit(f"{VARS_FILE} is missing required llm field(s): {missing}")
    return {
        "H_NAT_LLM_MODEL": str(llm["model"]),
        "H_NAT_LLM_BASE_URL": str(llm["base_url"]),
        "OPENAI_API_KEY": str(llm.get("api_key") or "EMPTY"),
    }


def invoke_nat(config_path: Path, command: str, env_vars: dict[str, str]) -> Any:
    env = dict(os.environ)
    env.update(env_vars)
    env["NAT_TELEMETRY_ENABLED"] = "false"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "nat.cli.main",
            "run",
            "--config_file",
            str(config_path),
            "--input",
            json.dumps({"command": command}),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        check=False,
    )
    clean = ANSI.sub("", completed.stdout)
    if completed.returncode != 0:
        raise RuntimeError(f"nat run failed (exit code {completed.returncode}):\n{clean}")

    match = RESULT.search(clean)
    if not match:
        raise RuntimeError(f"Could not extract 'Workflow Result' from nat output:\n{clean}")

    return json.loads(match.group(1).strip())


def run_case(config_path: Path, case: Case, env_vars: dict[str, str]) -> CaseResult:
    try:
        decision = invoke_nat(config_path, case.command, env_vars)
    except Exception as exc:  # noqa: BLE001 - a harness failure is itself a reportable result
        return CaseResult(case=case, actual=None, layer=None, reason=None, error=str(exc))
    return CaseResult(
        case=case,
        actual=decision.get("verdict"),
        layer=decision.get("layer"),
        reason=decision.get("reason"),
        error=None,
    )


def print_case_result(workflow_name: str, result: CaseResult) -> None:
    c = result.case
    tag = {
        "correct": "OK  ",
        "false_positive": "FP! ",
        "false_negative": "FN!!",
        "error": "ERR ",
    }[result.outcome]
    print(
        f"  [{tag}] {c.id:<16} ({c.category}) "
        f"expected={c.expected:<5} actual={result.actual or 'n/a':<5} "
        f"layer={result.layer or 'n/a'}"
    )
    if result.outcome in ("false_positive", "false_negative"):
        print(f"         command: {c.command!r}")
        print(f"         reason:  {result.reason!r}")
        print(f"         note:    {c.note}")
    if result.error:
        print(f"         error:   {result.error}")


def summarize(workflow_name: str, results: list[CaseResult]) -> dict[str, int]:
    counts = {"correct": 0, "false_positive": 0, "false_negative": 0, "error": 0}
    for r in results:
        counts[r.outcome] += 1
    print(f"\n--- Summary: {workflow_name} ---")
    print(f"  total={len(results)} correct={counts['correct']} "
          f"false_positives={counts['false_positive']} "
          f"false_negatives={counts['false_negative']} "
          f"errors={counts['error']}")

    by_category: dict[str, dict[str, int]] = {}
    for r in results:
        cat = by_category.setdefault(r.case.category, {"total": 0, "correct": 0})
        cat["total"] += 1
        if r.outcome == "correct":
            cat["correct"] += 1
    print("  by category:")
    for cat, c in by_category.items():
        print(f"    {cat:<16} {c['correct']}/{c['total']}")

    return counts


def main() -> None:
    env_vars = load_vars()
    workflow_files = sorted(WORKFLOWS_DIR.glob("*.yaml"))
    if not workflow_files:
        raise SystemExit(f"No workflow YAMLs found in {WORKFLOWS_DIR}")

    print("=" * 70)
    print("  h-asimov adversarial benchmark")
    print("=" * 70)
    print(f"Judge model: {env_vars['H_NAT_LLM_MODEL']} @ {env_vars['H_NAT_LLM_BASE_URL']}")
    print(f"Workflows:   {[f.name for f in workflow_files]}")
    print(f"Cases:       {len(CASES)}")

    all_false_negatives: list[tuple[str, CaseResult]] = []

    for workflow_file in workflow_files:
        print(f"\n{'=' * 70}\n  Workflow: {workflow_file.name}\n{'=' * 70}")
        results = []
        for case in CASES:
            result = run_case(workflow_file, case, env_vars)
            print_case_result(workflow_file.name, result)
            results.append(result)
            if result.outcome == "false_negative":
                all_false_negatives.append((workflow_file.name, result))
        summarize(workflow_file.name, results)

    print(f"\n{'=' * 70}")
    if all_false_negatives:
        print(f"  {len(all_false_negatives)} FALSE NEGATIVE(S) -- dangerous input(s) were ALLOWed:")
        for workflow_name, r in all_false_negatives:
            print(f"    [{workflow_name}] {r.case.id}: {r.case.command!r}")
    else:
        print("  No false negatives across any workflow variant.")
    print("=" * 70)


if __name__ == "__main__":
    main()
