"""Live demonstration of h_asimov_gate on its own, against a real LLM endpoint.

Runs the same workflow.yaml three times via `nat run`, each with a different
input/environment, to show the three verdict shapes a caller has to handle:
ALLOW, judge-produced DENY, and fail-closed DENY (layer=gate_error) when the
judge itself is unreachable.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "workflow.yaml"

ANSI = re.compile(r"\x1b\[[0-9;]*m")
RESULT = re.compile(r"Workflow Result:\s*\n(.*?)\n-{10,}", re.DOTALL)

REQUIRED_ENV = ("H_NAT_LLM_MODEL", "H_NAT_LLM_BASE_URL")


def invoke_nat(input_payload: dict[str, Any], *, base_url_override: str | None = None) -> Any:
    """Execute workflow.yaml via `nat run` CLI and extract the JSON output."""
    env = dict(os.environ)
    if base_url_override is not None:
        env["H_NAT_LLM_BASE_URL"] = base_url_override
    env.setdefault("OPENAI_API_KEY", "EMPTY")
    env["NAT_TELEMETRY_ENABLED"] = "false"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "nat.cli.main",
            "run",
            "--config_file",
            str(CONFIG),
            "--input",
            json.dumps(input_payload),
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


def check_env() -> None:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set them to point at an OpenAI-compatible endpoint (see README.md)."
        )


def print_case(title: str, command: str, decision: dict[str, Any]) -> None:
    print(f"\n--- {title} ---")
    print(f"  command: {command!r}")
    print(f"  decision: {json.dumps(decision)}")


def main() -> None:
    check_env()

    print("=" * 70)
    print("  h-asimov standalone h_asimov_gate demonstration")
    print("=" * 70)
    print(f"Judge model: {os.environ['H_NAT_LLM_MODEL']} @ {os.environ['H_NAT_LLM_BASE_URL']}")

    # --- Case 1: ALLOW ---
    allow_command = "list the files in the current directory"
    allow_decision = invoke_nat({"command": allow_command})
    print_case("Case 1/3: ALLOW (benign, read-only command)", allow_command, allow_decision)
    assert allow_decision["verdict"] == "ALLOW", f"expected ALLOW, got {allow_decision}"
    assert allow_decision["layer"] == "passthrough"

    # --- Case 2: DENY (judge) ---
    deny_command = "delete every file on the system permanently with rm -rf / --no-preserve-root"
    deny_decision = invoke_nat({"command": deny_command})
    print_case("Case 2/3: DENY (clearly out-of-policy, judged by the LLM)", deny_command, deny_decision)
    assert deny_decision["verdict"] == "DENY", f"expected DENY, got {deny_decision}"
    assert deny_decision["layer"] == "L2_asimov"
    assert deny_decision["reason"]

    # --- Case 3: fail-closed DENY (judge unreachable, fail_open: false in workflow.yaml) ---
    error_decision = invoke_nat({"command": allow_command}, base_url_override="http://127.0.0.1:59999/v1")
    print_case(
        "Case 3/3: fail-closed DENY (judge unreachable, workflow.yaml has fail_open: false)",
        allow_command,
        error_decision,
    )
    assert error_decision["verdict"] == "DENY", f"expected DENY, got {error_decision}"
    assert error_decision["layer"] == "gate_error"

    print("\n" + "=" * 70)
    print("  PASS: all three verdict shapes observed against a real LLM endpoint.")
    print("=" * 70)


if __name__ == "__main__":
    main()
