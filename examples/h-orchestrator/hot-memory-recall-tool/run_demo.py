"""Drive the production YAML through hot-memory and recall-tool paths."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import redis

HERE = Path(__file__).resolve().parent
REQUIRED_ENV = (
    "H_NAT_REDIS_URL",
    "H_NAT_LLM_BASE_URL",
    "H_NAT_LLM_MODEL",
    "OPENAI_API_KEY",
)
TOOL_CALL_PATTERN = re.compile(
    r"Calling tools?:[^\n]*\brecall_search\b", re.IGNORECASE
)


def run(config: str, prompt: str) -> str:
    command = [
        "nat",
        "run",
        "--config_file",
        str(HERE / config),
        "--input",
        prompt,
    ]
    environment = {**os.environ, "NAT_TELEMETRY_ENABLED": "false"}
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    print(completed.stdout, end="")
    if completed.returncode:
        raise RuntimeError(f"{' '.join(command[:4])} exited {completed.returncode}")
    return completed.stdout


def require_environment() -> None:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise RuntimeError("set required environment variables: " + ", ".join(missing))
    modules = redis.from_url(os.environ["H_NAT_REDIS_URL"]).module_list()
    names = {
        str(module.get(b"name", module.get("name", ""))).lower()
        for module in modules
    }
    if not any("search" in name for name in names) or not any(
        "json" in name or "rejson" in name for name in names
    ):
        raise RuntimeError("H_NAT_REDIS_URL must point to Redis Stack with Search and JSON")


def main() -> int:
    require_environment()
    codeword = "ORBIT-" + secrets.token_hex(6).upper()

    print(f"\n[1/5] Write a fact through h_chat_cycle hot memory: {codeword}")
    run("workflow.yaml", f"Remember this exact codeword for later: {codeword}")

    print("\n[2/5] Let the fact age, then migrate and vectorize it")
    time.sleep(2)
    run("sweep.yaml", '{"chat_ids":["recall-demo-chat"]}')
    run("vectorize.yaml", "{}")

    print("\n[3/5] General knowledge should not call recall_search")
    direct_trace = run("workflow.yaml", "What is two plus two? Answer with one number.")
    if TOOL_CALL_PATTERN.search(direct_trace):
        raise AssertionError("the agent called recall_search for a self-contained question")

    print("\n[4/5] The migrated fact should require recall_search")
    recall_trace = run("workflow.yaml", "What exact codeword did I ask you to remember?")
    if not TOOL_CALL_PATTERN.search(recall_trace):
        raise AssertionError("the agent did not call recall_search for the migrated fact")
    if codeword not in recall_trace:
        raise AssertionError("the recalled answer did not contain the seeded codeword")

    print("\n[5/5] PASS: hot write, migration, conditional tool use, and recall verified")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError, redis.RedisError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from error

