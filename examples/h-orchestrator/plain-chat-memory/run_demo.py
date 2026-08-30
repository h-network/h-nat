"""Run ten independent NAT processes and verify Redis-backed chat continuity."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import redis

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "workflow.yaml"
POD = "plain-chat-demo"
AGENT = "assistant"
REQUIRED_ENV = (
    "H_NAT_REDIS_URL",
    "H_NAT_LLM_BASE_URL",
    "H_NAT_LLM_MODEL",
    "OPENAI_API_KEY",
)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
RESULT = re.compile(r"Workflow Result:\s*\n(.*?)\n-{10,}", re.DOTALL)


def require_environment() -> redis.Redis:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise RuntimeError("set required environment variables: " + ", ".join(missing))
    client = redis.from_url(os.environ["H_NAT_REDIS_URL"], decode_responses=True)
    client.ping()
    return client


def invoke(message: str, chat_id: str) -> str:
    request = json.dumps({"message": message, "chat_id": chat_id})
    completed = subprocess.run(
        ["nat", "run", "--config_file", str(CONFIG), "--input", request],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "NAT_TELEMETRY_ENABLED": "false"},
    )
    print(completed.stdout, end="")
    if completed.returncode:
        raise RuntimeError(f"nat run exited {completed.returncode}")
    clean = ANSI.sub("", completed.stdout)
    match = RESULT.search(clean)
    if not match:
        raise RuntimeError("could not extract Workflow Result from nat output")
    return match.group(1).strip()


def assert_contains(answer: str, *terms: str) -> None:
    lowered = answer.casefold()
    missing = [term for term in terms if term.casefold() not in lowered]
    if missing:
        raise AssertionError(f"answer missing {missing}: {answer!r}")


def main() -> int:
    client = require_environment()
    nonce = secrets.token_hex(4)
    chat_id = f"plain-chat-{nonce}"
    index_key = f"{POD}:{AGENT}:chat-index:{chat_id}"
    turns: list[tuple[str, tuple[str, ...]]] = [
        (f"My name is Mira-{nonce}. Remember it.", ()),
        ("I work as a network reliability engineer. Remember that too.", ()),
        ("My preferred network vendor is Juniper.", ()),
        ("My pet is an axolotl named Pixel.", ()),
        ("What is 17 plus 25? Answer with the number.", ("42",)),
        ("What name did I tell you?", (f"Mira-{nonce}",)),
        ("What is my job?", ("network", "reliability", "engineer")),
        ("Which network vendor did I say I prefer?", ("Juniper",)),
        ("What kind of pet do I have, and what is its name?", ("axolotl", "Pixel")),
        (
            "Summarize the four personal facts I asked you to remember.",
            (f"Mira-{nonce}", "network", "Juniper", "axolotl", "Pixel"),
        ),
    ]

    transcript = []
    for number, (message, expected) in enumerate(turns, start=1):
        prior = client.zcard(index_key)
        if prior != 2 * (number - 1):
            raise AssertionError(f"turn {number}: expected {2 * (number - 1)} prior records, got {prior}")
        print(f"\n[{number}/10] user: {message}")
        answer = invoke(message, chat_id)
        print(f"assistant: {answer}")
        assert_contains(answer, *expected)
        after = client.zcard(index_key)
        if after != 2 * number:
            raise AssertionError(f"turn {number}: expected {2 * number} stored records, got {after}")
        transcript.append((number, prior, message, answer))

    keys = client.zrange(index_key, 0, -1)
    payloads = [json.loads(value) for value in client.mget(keys) if value]
    roles = [payload.get("role") for payload in payloads]
    if roles.count("user") != 10 or roles.count("assistant") != 10:
        raise AssertionError(f"unexpected persisted roles: {roles}")

    print("\nPASS: 10 processes, 20 Redis turns, arithmetic control, and all recalls verified")
    print(f"chat_id={chat_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError, redis.RedisError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from error
