"""Run end-to-end demonstration of h-recall: planting hot turns, sweeping, vectorizing, and hybrid searching."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from nat.plugins.h_memory import BoundedBufferStore

HERE = Path(__file__).resolve().parent
CONFIG_SWEEP = HERE / "sweep.yaml"
CONFIG_VEC = HERE / "vectorize.yaml"
CONFIG_SEARCH = HERE / "search.yaml"

POD = "recall_demo"
AGENT = "assistant"
DEFAULT_REDIS_URL = "redis://172.16.10.102:6379"

ANSI = re.compile(r"\x1b\[[0-9;]*m")
RESULT = re.compile(r"Workflow Result:\s*\n(.*?)\n-{10,}", re.DOTALL)


def get_redis_url() -> str:
    return os.environ.get("H_NAT_REDIS_URL", DEFAULT_REDIS_URL)


def invoke_nat(config_path: Path, input_payload: dict[str, Any]) -> Any:
    """Execute a NAT workflow via `nat run` CLI and extract the JSON output."""
    env = {
        **os.environ,
        "H_NAT_REDIS_URL": get_redis_url(),
        "NAT_TELEMETRY_ENABLED": "false",
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "nat.cli.main",
            "run",
            "--config_file",
            str(config_path),
            "--input",
            json.dumps(input_payload),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"nat run failed (exit code {completed.returncode}):\n{completed.stdout}"
        )

    clean = ANSI.sub("", completed.stdout)
    match = RESULT.search(clean)
    if not match:
        raise RuntimeError(
            f"Could not extract 'Workflow Result' from nat output:\n{clean}"
        )

    raw_result = match.group(1).strip()
    try:
        return json.loads(raw_result)
    except json.JSONDecodeError:
        return raw_result


async def check_redis_stack(redis_url: str) -> None:
    """Verify that Redis is reachable and has RediSearch and RedisJSON loaded."""
    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    try:
        await client.ping()
        modules = await client.module_list()
        module_names = [
            m.get("name") if isinstance(m, dict) else m[1]
            for m in modules
            if isinstance(m, (dict, list))
        ]
        # RediSearch is 'search' or 'redisearch'; RedisJSON is 'ReJSON' or 'json'
        has_search = any("search" in str(n).lower() for n in module_names)
        has_json = any("json" in str(n).lower() for n in module_names)
        if not (has_search and has_json):
            print(
                f"[WARNING] Target Redis at {redis_url} may lack required modules (found: {module_names}).",
                file=sys.stderr,
            )
    finally:
        await client.aclose()


async def plant_conversation_turns(
    redis_url: str, chat_id: str, facts: list[tuple[str, str]]
) -> list[str]:
    """Plant fresh conversation turns into hot memory using h-memory."""
    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    planted_keys = []
    try:
        mem_store = BoundedBufferStore(
            client, pod=POD, agent=AGENT, ttl_seconds_max=86400
        )
        for user_msg, assistant_msg in facts:
            ukey = await mem_store.write_turn(
                chat_id=chat_id,
                role="user",
                content=user_msg,
                ttl_seconds=86400,
            )
            planted_keys.append(ukey)
            akey = await mem_store.write_turn(
                chat_id=chat_id,
                role="assistant",
                content=assistant_msg,
                ttl_seconds=86400,
            )
            planted_keys.append(akey)
    finally:
        await client.aclose()
    return planted_keys


async def inspect_audit_doc(redis_url: str, audit_key: str) -> dict[str, Any] | None:
    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    try:
        return await client.json().get(audit_key)
    finally:
        await client.aclose()


async def count_hot_index(redis_url: str, chat_id: str) -> int:
    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    try:
        index_key = f"{POD}:{AGENT}:chat-index:{chat_id}"
        return await client.zcard(index_key)
    finally:
        await client.aclose()


def main() -> int:
    redis_url = get_redis_url()
    print("=" * 70)
    print("  h-recall End-to-End Fill & Search Demonstration")
    print("=" * 70)
    print(f"Target Redis URL: {redis_url}")
    print(f"Pod: {POD} | Agent: {AGENT}")

    asyncio.run(check_redis_stack(redis_url))

    nonce = secrets.token_hex(4)
    chat_id = f"recall-demo-{nonce}"
    print(f"\n[Generated Test Session] chat_id = {chat_id}")

    # 4 distinct facts to plant
    facts = [
        (
            f"Project Nova-{nonce} uses quantum-resistant lattice cryptography for all internal key exchange.",
            f"Acknowledged that Project Nova-{nonce} uses lattice cryptography.",
        ),
        (
            "The production database failover cluster is scheduled for maintenance every Tuesday at 04:00 UTC.",
            "Recorded database maintenance schedule for Tuesday at 04:00 UTC.",
        ),
        (
            f"Our secondary disaster recovery datacenter is located in Reykjavik, Iceland (Facility ICE-{nonce}).",
            f"Noted secondary datacenter location in Reykjavik (ICE-{nonce}).",
        ),
        (
            "The lead architect for the distributed storage engine is Dr. Elena Rostova.",
            "Stored lead storage architect name Dr. Elena Rostova.",
        ),
    ]

    # -----------------------------------------------------------------------
    # Step 1: Plant conversation turns in hot memory (h-memory)
    # -----------------------------------------------------------------------
    print("\n--- [Step 1/4] Planting 4 conversation turns (8 turns total) into hot memory ---")
    planted_keys = asyncio.run(
        plant_conversation_turns(redis_url, chat_id, facts)
    )
    for k in planted_keys:
        print(f"  + Planted hot key: {k}")

    initial_hot_count = asyncio.run(count_hot_index(redis_url, chat_id))
    print(f"  -> Hot ZSET index count: {initial_hot_count} records")
    assert initial_hot_count == len(planted_keys), "Hot turns count mismatch"

    # Allow 1.1s to elapse so turns exceed migration_threshold_sec=1
    print("\n  (Waiting 1.2s for turns to satisfy migration threshold)...")
    time.sleep(1.2)

    # -----------------------------------------------------------------------
    # Step 2: Migrate hot turns to audit tier via h_semantic_sweep
    # -----------------------------------------------------------------------
    print("\n--- [Step 2/4] Executing h_semantic_sweep migration ---")
    sweep_input = {"chat_ids": [chat_id]}
    sweep_res = invoke_nat(CONFIG_SWEEP, sweep_input)
    print(f"  -> Sweep result: {sweep_res}")
    assert sweep_res.get("migrated") == len(planted_keys), (
        f"Expected {len(planted_keys)} migrated docs, got {sweep_res}"
    )

    remaining_hot_count = asyncio.run(count_hot_index(redis_url, chat_id))
    print(f"  -> Remaining hot ZSET count after sweep: {remaining_hot_count} (expected 0)")
    assert remaining_hot_count == 0, "Hot turns were not cleaned up after migration"

    # Verify audit document state (sentinel embedding + pending flag)
    sample_audit_key = planted_keys[0].replace(":chat:", ":chat-audit:")
    sample_doc = asyncio.run(inspect_audit_doc(redis_url, sample_audit_key))
    assert sample_doc is not None, f"Audit doc missing: {sample_audit_key}"
    assert sample_doc.get("pending_vectorize") == "1", "Pending flag missing"
    assert sample_doc.get("embedding") == [0.0] * 384, "Sentinel embedding mismatch"
    print(f"  -> Verified audit doc: sentinel embedding [0.0]*384 and pending_vectorize='1' present")

    # -----------------------------------------------------------------------
    # Step 3: Embed audit documents via h_semantic_vectorize
    # -----------------------------------------------------------------------
    print("\n--- [Step 3/4] Executing h_semantic_vectorize batch embedding ---")
    vec_input = {"batch_size": 32, "max_per_cycle": 100}
    vec_res = invoke_nat(CONFIG_VEC, vec_input)
    print(f"  -> Vectorize result: {vec_res}")
    assert vec_res.get("vectorized", 0) >= len(planted_keys), (
        f"Expected at least {len(planted_keys)} docs vectorized, got {vec_res}"
    )

    # Verify real embedding and flag clearance
    sample_doc_after = asyncio.run(inspect_audit_doc(redis_url, sample_audit_key))
    assert "pending_vectorize" not in sample_doc_after, "Pending flag was not cleared"
    assert any(v != 0.0 for v in sample_doc_after.get("embedding", [])), "Real vector not set"
    print(f"  -> Verified audit doc: real 384d vector embedded, pending flag cleared")

    # -----------------------------------------------------------------------
    # Step 4: Hybrid search retrieval via h_semantic_search
    # -----------------------------------------------------------------------
    print("\n--- [Step 4/4] Executing hybrid retrieval queries via h_semantic_search ---")
    queries = [
        (
            f"What kind of cryptography is used in Project Nova-{nonce}?",
            f"lattice cryptography",
            "Project Nova",
        ),
        (
            "When is the database failover cluster maintenance scheduled?",
            "Tuesday at 04:00 UTC",
            "Tuesday",
        ),
        (
            "Where is our secondary disaster recovery site?",
            f"Reykjavik, Iceland (Facility ICE-{nonce})",
            "Reykjavik",
        ),
        (
            "Who is the lead architect for distributed storage?",
            "Dr. Elena Rostova",
            "Elena Rostova",
        ),
    ]

    for idx, (query_text, expected_fact, keyword) in enumerate(queries, start=1):
        print(f"\n  [Query {idx}/4]: {query_text}")
        search_input = {
            "chat_id": chat_id,
            "query": query_text,
            "top_k": 3,
            "mode": "hybrid",
        }
        results = invoke_nat(CONFIG_SEARCH, search_input)
        if not isinstance(results, list) or not results:
            raise AssertionError(f"No search results returned for query: {query_text}")

        top_hit = results[0]
        top_content = top_hit.get("content", "")
        top_role = top_hit.get("role", "")
        print(f"    Rank 0 [{top_role}]: \"{top_content}\"")
        assert keyword.casefold() in top_content.casefold(), (
            f"Top search hit did not contain expected keyword '{keyword}'. Hit: {top_content}"
        )

    print("\n" + "=" * 70)
    print("  PASS: End-to-end h-recall pipeline verified successfully!")
    print(f"  Planted: 8 turns | Migrated: {sweep_res.get('migrated')} | Vectorized: {vec_res.get('vectorized')} | 4/4 Queries Accurate")
    print(f"  Session ID: {chat_id}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\n[DEMO FAILED]: {exc}", file=sys.stderr)
        sys.exit(1)
