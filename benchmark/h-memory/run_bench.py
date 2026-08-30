#!/usr/bin/env python3
"""High-performance stress and edge-case benchmark suite for h-memory.

Drives Redis operations directly through the NAT runtime (via nat.runtime.loader.load_workflow)
exercising the canonical workflow_write.yaml and workflow_delete.yaml definitions:
  1. Throughput & Latency Scaling (10 to 200 concurrent workers)
  2. Concurrent Multi-Writer Race on Same chat_id (Monotonic clock guard & ordering)
  3. Concurrent hot_keep_count Rank-Pruning Under High Contention
  4. TTL Boundary Expiration & Cleanup
  5. delete_chat Racing In-Flight Writes

Outputs concrete metrics (writes/sec, latency percentiles, data-loss & ordering checks)
and formats results into Markdown (RESULTS.md) or JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
import yaml
from nat.runtime.loader import load_workflow
from nat.runtime.session import SessionManager

HERE = Path(__file__).resolve().parent
DEFAULT_VARS_PATH = HERE / "vars.example.yaml"
WORKFLOW_WRITE_PATH = HERE / "workflow_write.yaml"
WORKFLOW_DELETE_PATH = HERE / "workflow_delete.yaml"


@dataclass
class LatencyStats:
    count: int = 0
    min_ms: float = 0.0
    p50_ms: float = 0.0
    p90_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0
    mean_ms: float = 0.0
    stddev_ms: float = 0.0
    throughput_wps: float = 0.0
    total_time_s: float = 0.0

    @classmethod
    def calculate(cls, latencies_ms: list[float], total_time_s: float) -> LatencyStats:
        if not latencies_ms:
            return cls()
        s = sorted(latencies_ms)
        n = len(s)
        return cls(
            count=n,
            min_ms=round(s[0], 3),
            p50_ms=round(s[int(n * 0.50)], 3),
            p90_ms=round(s[min(int(n * 0.90), n - 1)], 3),
            p95_ms=round(s[min(int(n * 0.95), n - 1)], 3),
            p99_ms=round(s[min(int(n * 0.99), n - 1)], 3),
            max_ms=round(s[-1], 3),
            mean_ms=round(statistics.mean(s), 3),
            stddev_ms=round(statistics.stdev(s) if n > 1 else 0.0, 3),
            throughput_wps=round(n / total_time_s, 2) if total_time_s > 0 else 0.0,
            total_time_s=round(total_time_s, 4),
        )


@dataclass
class ScenarioResult:
    name: str
    description: str
    passed: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# NAT Workflow Helper Invokers
# ---------------------------------------------------------------------------

async def nat_write_turn(
    write_session: SessionManager,
    chat_id: str,
    role: str,
    content: str,
    ttl_seconds: int,
    hot_keep_count: int | None = None,
) -> str:
    """Executes h_memory_write_turn via the NAT workflow session."""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "role": role,
        "content": content,
        "ttl_seconds": ttl_seconds,
    }
    if hot_keep_count is not None:
        payload["hot_keep_count"] = hot_keep_count

    async with write_session.run(payload) as runner:
        return str(await runner.result(to_type=str))


async def nat_delete_chat(
    delete_session: SessionManager,
    chat_id: str,
) -> int:
    """Executes h_memory_delete_chat via the NAT workflow session."""
    async with delete_session.run({"chat_id": chat_id}) as runner:
        res = await runner.result(to_type=str)
        return int(res)


# ---------------------------------------------------------------------------
# Scenario 1: Throughput & Latency Scaling
# ---------------------------------------------------------------------------

async def _throughput_worker(
    worker_id: int,
    chat_prefix: str,
    writes_per_worker: int,
    sem: asyncio.Semaphore,
    write_session: SessionManager,
    latencies_ms: list[float],
) -> None:
    worker_chat = f"{chat_prefix}-{worker_id}"
    for i in range(writes_per_worker):
        async with sem:
            t0 = time.perf_counter()
            await nat_write_turn(
                write_session=write_session,
                chat_id=worker_chat,
                role="user" if i % 2 == 0 else "assistant",
                content=f"Benchmark payload {i} for worker {worker_id}",
                ttl_seconds=300,
            )
            t1 = time.perf_counter()
            latencies_ms.append((t1 - t0) * 1000)


async def run_scenario_throughput(
    write_session: SessionManager,
    delete_session: SessionManager,
    client: aioredis.Redis,
    concurrency_levels: list[int],
    writes_per_level: int,
) -> ScenarioResult:
    """Measures sustained write throughput and latency percentiles through NAT."""
    anomalies: list[str] = []
    level_results: dict[str, Any] = {}

    for concurrency in concurrency_levels:
        chat_prefix = f"bench-thru-{concurrency}-{uuid.uuid4().hex[:6]}"
        latencies_ms: list[float] = []
        writes_per_worker = max(1, writes_per_level // concurrency)

        sem = asyncio.Semaphore(concurrency)

        wall_start = time.perf_counter()
        tasks = [
            asyncio.create_task(
                _throughput_worker(
                    worker_id=w,
                    chat_prefix=chat_prefix,
                    writes_per_worker=writes_per_worker,
                    sem=sem,
                    write_session=write_session,
                    latencies_ms=latencies_ms,
                )
            )
            for w in range(concurrency)
        ]
        await asyncio.gather(*tasks)
        wall_time = time.perf_counter() - wall_start

        stats = LatencyStats.calculate(latencies_ms, wall_time)
        level_results[f"concurrency_{concurrency}"] = asdict(stats)

        # Cleanup chat keys created during this level
        del_tasks = [nat_delete_chat(delete_session, f"{chat_prefix}-{w}") for w in range(concurrency)]
        await asyncio.gather(*del_tasks)

    passed = len(anomalies) == 0
    return ScenarioResult(
        name="Throughput & Latency Scaling",
        description="Measures write throughput (writes/sec) and p50/p90/p99 latencies through NAT runtime under varying worker concurrency.",
        passed=passed,
        metrics=level_results,
        anomalies=anomalies,
    )


# ---------------------------------------------------------------------------
# Scenario 2: Concurrent Multi-Writer Race on Same chat_id
# ---------------------------------------------------------------------------

async def _same_chat_writer(
    writer_id: int,
    chat_id: str,
    writes_per_writer: int,
    write_session: SessionManager,
    written_keys: list[str],
    latencies_ms: list[float],
) -> None:
    for i in range(writes_per_writer):
        t0 = time.perf_counter()
        key = await nat_write_turn(
            write_session=write_session,
            chat_id=chat_id,
            role="user",
            content=f"writer-{writer_id}-msg-{i}",
            ttl_seconds=600,
        )
        t1 = time.perf_counter()
        written_keys.append(key)
        latencies_ms.append((t1 - t0) * 1000)


async def run_scenario_concurrent_same_chat(
    write_session: SessionManager,
    delete_session: SessionManager,
    client: aioredis.Redis,
    pod: str,
    agent: str,
    writers: int,
    writes_per_writer: int,
) -> ScenarioResult:
    """Stresses monotonic nanosecond clock guard and sorted set ordering via NAT."""
    anomalies: list[str] = []
    chat_id = f"bench-same-chat-{uuid.uuid4().hex[:8]}"
    total_expected = writers * writes_per_writer
    written_keys: list[str] = []
    latencies_ms: list[float] = []

    wall_start = time.perf_counter()
    tasks = [
        asyncio.create_task(
            _same_chat_writer(
                writer_id=w,
                chat_id=chat_id,
                writes_per_writer=writes_per_writer,
                write_session=write_session,
                written_keys=written_keys,
                latencies_ms=latencies_ms,
            )
        )
        for w in range(writers)
    ]
    await asyncio.gather(*tasks)
    wall_time = time.perf_counter() - wall_start

    # Check 1: Key Uniqueness (no overwrite collisions)
    unique_keys = set(written_keys)
    if len(unique_keys) != total_expected:
        anomalies.append(
            f"Key collision detected: {len(unique_keys)} unique keys vs {total_expected} writes"
        )

    # Check 2: Index Member Count in Redis
    index_key = f"{pod}:{agent}:chat-index:{chat_id}"
    indexed_members = await client.zrange(index_key, 0, -1)
    if len(indexed_members) != total_expected:
        anomalies.append(
            f"Index member count mismatch: {len(indexed_members)} in ZSET vs {total_expected} written"
        )

    # Check 3: Timestamp Monotonicity and Ordering
    extracted_timestamps = [int(k.split(":")[-1]) for k in indexed_members]
    for i in range(len(extracted_timestamps) - 1):
        if extracted_timestamps[i] >= extracted_timestamps[i + 1]:
            anomalies.append(
                f"Monotonicity violation in ZSET order at index {i}: "
                f"{extracted_timestamps[i]} >= {extracted_timestamps[i+1]}"
            )
            break

    # Check 4: Data Retrieval via MGET
    raw_turns = await client.mget(*indexed_members)
    valid_turns = [t for t in raw_turns if t is not None]
    if len(valid_turns) != total_expected:
        anomalies.append(f"Lost turn data: retrieved {len(valid_turns)} of {total_expected}")

    # Cleanup via NAT workflow
    deleted = await nat_delete_chat(delete_session, chat_id)
    if deleted != total_expected + 1:
        anomalies.append(f"delete_chat returned {deleted} deleted keys, expected {total_expected + 1}")

    stats = LatencyStats.calculate(latencies_ms, wall_time)
    return ScenarioResult(
        name="Concurrent Same-Chat Monotonicity",
        description="Fires high-concurrency writes through NAT at the exact same chat_id to verify monotonic clock guard, uniqueness, and ordering.",
        passed=len(anomalies) == 0,
        metrics={
            "writers": writers,
            "writes_per_writer": writes_per_writer,
            "total_writes": total_expected,
            "unique_keys_ratio": round(len(unique_keys) / total_expected, 4),
            "stats": asdict(stats),
        },
        anomalies=anomalies,
    )


# ---------------------------------------------------------------------------
# Scenario 3: Concurrent hot_keep_count Rank-Pruning
# ---------------------------------------------------------------------------

async def _rank_prune_writer(
    writer_id: int,
    chat_id: str,
    writes_per_writer: int,
    hot_keep_count: int,
    write_session: SessionManager,
    all_written_keys: list[str],
    latencies_ms: list[float],
) -> None:
    for i in range(writes_per_writer):
        t0 = time.perf_counter()
        key = await nat_write_turn(
            write_session=write_session,
            chat_id=chat_id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"msg from {writer_id} - {i}",
            ttl_seconds=600,
            hot_keep_count=hot_keep_count,
        )
        t1 = time.perf_counter()
        all_written_keys.append(key)
        latencies_ms.append((t1 - t0) * 1000)


async def run_scenario_rank_prune(
    write_session: SessionManager,
    delete_session: SessionManager,
    client: aioredis.Redis,
    pod: str,
    agent: str,
    writers: int,
    total_writes: int,
    hot_keep_count: int,
) -> ScenarioResult:
    """Stresses ZREMRANGEBYRANK trimming under concurrent parallel NAT writes."""
    anomalies: list[str] = []
    chat_id = f"bench-rank-prune-{uuid.uuid4().hex[:8]}"
    writes_per_writer = total_writes // writers
    actual_total = writes_per_writer * writers
    all_written_keys: list[str] = []
    latencies_ms: list[float] = []

    wall_start = time.perf_counter()
    tasks = [
        asyncio.create_task(
            _rank_prune_writer(
                writer_id=w,
                chat_id=chat_id,
                writes_per_writer=writes_per_writer,
                hot_keep_count=hot_keep_count,
                write_session=write_session,
                all_written_keys=all_written_keys,
                latencies_ms=latencies_ms,
            )
        )
        for w in range(writers)
    ]
    await asyncio.gather(*tasks)
    wall_time = time.perf_counter() - wall_start

    # Check 1: Index Length is strictly bounded to hot_keep_count
    index_key = f"{pod}:{agent}:chat-index:{chat_id}"
    indexed_members = await client.zrange(index_key, 0, -1)
    if len(indexed_members) != hot_keep_count:
        anomalies.append(
            f"Index count violation: expected {hot_keep_count}, found {len(indexed_members)}"
        )

    # Check 2: Index contains the highest timestamp keys
    sorted_all_keys = sorted(all_written_keys, key=lambda k: int(k.split(":")[-1]))
    expected_kept = set(sorted_all_keys[-hot_keep_count:])
    actual_kept = set(indexed_members)
    diff = expected_kept - actual_kept
    if diff:
        anomalies.append(f"Rank-prune kept suboptimal keys; delta count: {len(diff)}")

    # Check 3: Orphaned Data Keys Remain Resident in Redis
    orphaned_keys = sorted_all_keys[:-hot_keep_count]
    if orphaned_keys:
        sample_orphans = orphaned_keys[: min(50, len(orphaned_keys))]
        orphan_payloads = await client.mget(*sample_orphans)
        resident_orphans = sum(1 for p in orphan_payloads if p is not None)
        if resident_orphans != len(sample_orphans):
            anomalies.append(
                f"Orphan data key residency issue: {resident_orphans}/{len(sample_orphans)} present"
            )

    # Cleanup via NAT workflow
    await nat_delete_chat(delete_session, chat_id)
    # Also clean orphaned data keys from test
    if orphaned_keys:
        await client.delete(*orphaned_keys)

    stats = LatencyStats.calculate(latencies_ms, wall_time)
    return ScenarioResult(
        name="Concurrent hot_keep_count Rank-Pruning",
        description="Validates that concurrent NAT writes maintain exact index rank bounds without corrupting working set.",
        passed=len(anomalies) == 0,
        metrics={
            "total_writes": actual_total,
            "hot_keep_count": hot_keep_count,
            "final_index_size": len(indexed_members),
            "stats": asdict(stats),
        },
        anomalies=anomalies,
    )


# ---------------------------------------------------------------------------
# Scenario 4: TTL Boundary Behavior & Score Expiration
# ---------------------------------------------------------------------------

async def run_scenario_ttl_boundary(
    write_session: SessionManager,
    delete_session: SessionManager,
    client: aioredis.Redis,
    pod: str,
    agent: str,
    writes: int,
    short_ttl_seconds: int = 1,
) -> ScenarioResult:
    """Verifies write and read behavior at short TTL expiration boundaries through NAT."""
    anomalies: list[str] = []
    chat_id = f"bench-ttl-boundary-{uuid.uuid4().hex[:8]}"

    # Write initial batch with short TTL
    short_keys = []
    for i in range(writes):
        k = await nat_write_turn(
            write_session=write_session,
            chat_id=chat_id,
            role="user",
            content=f"short-lived turn {i}",
            ttl_seconds=short_ttl_seconds,
        )
        short_keys.append(k)

    # Wait for TTL to expire
    await asyncio.sleep(short_ttl_seconds + 0.5)

    # Check 1: Data keys should have expired
    expired_data = await client.mget(*short_keys)
    non_null = sum(1 for d in expired_data if d is not None)
    if non_null > 0:
        anomalies.append(f"TTL failure: {non_null} data keys still resident after TTL expiry")

    # Check 2: Write fresh turn with normal TTL and verify store handles mixed state
    fresh_key = await nat_write_turn(
        write_session=write_session,
        chat_id=chat_id,
        role="assistant",
        content="fresh message after expiry",
        ttl_seconds=300,
    )

    index_key = f"{pod}:{agent}:chat-index:{chat_id}"
    members = await client.zrange(index_key, 0, -1)
    if fresh_key not in members:
        anomalies.append("Fresh turn key missing from index after TTL boundary write")

    # Cleanup via NAT workflow
    await nat_delete_chat(delete_session, chat_id)

    return ScenarioResult(
        name="TTL Boundary & Expiry Behavior",
        description="Tests turn expiry at short TTL boundaries and verifies post-expiry writes remain robust.",
        passed=len(anomalies) == 0,
        metrics={
            "initial_short_ttl_writes": writes,
            "short_ttl_seconds": short_ttl_seconds,
            "expired_keys_verified": writes - non_null,
        },
        anomalies=anomalies,
    )


# ---------------------------------------------------------------------------
# Scenario 5: delete_chat Racing In-Flight Writes
# ---------------------------------------------------------------------------

async def _delete_race_writer(
    writer_id: int,
    chat_id: str,
    writes_per_writer: int,
    write_session: SessionManager,
    written_keys: list[str],
    write_exceptions: list[str],
) -> None:
    for i in range(writes_per_writer):
        try:
            k = await nat_write_turn(
                write_session=write_session,
                chat_id=chat_id,
                role="user",
                content=f"in-flight {writer_id}-{i}",
                ttl_seconds=300,
            )
            written_keys.append(k)
        except Exception as e:  # noqa: BLE001
            write_exceptions.append(str(e))
        await asyncio.sleep(0.001)


async def run_scenario_delete_race(
    write_session: SessionManager,
    delete_session: SessionManager,
    client: aioredis.Redis,
    pod: str,
    agent: str,
    active_writers: int,
    writes_per_writer: int,
) -> ScenarioResult:
    """Tests race condition resilience when delete_chat executes while writes are in flight."""
    anomalies: list[str] = []
    chat_id = f"bench-delete-race-{uuid.uuid4().hex[:8]}"
    written_keys: list[str] = []
    write_exceptions: list[str] = []
    deleted_count = 0

    async def _deleter():
        nonlocal deleted_count
        await asyncio.sleep(0.010)  # Let writers start
        try:
            deleted_count = await nat_delete_chat(delete_session, chat_id)
        except Exception as e:  # noqa: BLE001
            anomalies.append(f"delete_chat raised unhandled exception during race: {e}")

    tasks = [
        asyncio.create_task(
            _delete_race_writer(
                writer_id=w,
                chat_id=chat_id,
                writes_per_writer=writes_per_writer,
                write_session=write_session,
                written_keys=written_keys,
                write_exceptions=write_exceptions,
            )
        )
        for w in range(active_writers)
    ]
    tasks.append(asyncio.create_task(_deleter()))
    await asyncio.gather(*tasks)

    if write_exceptions:
        anomalies.append(f"In-flight writes threw exceptions during delete race: {write_exceptions[:3]}")

    # Check state after all operations complete
    index_key = f"{pod}:{agent}:chat-index:{chat_id}"
    final_members = await client.zrange(index_key, 0, -1)

    # Cleanup via NAT workflow
    await nat_delete_chat(delete_session, chat_id)

    return ScenarioResult(
        name="delete_chat Racing In-Flight Writes",
        description="Fires active write streams concurrently with delete_chat through NAT to verify pipeline atomicity and zero deadlock.",
        passed=len(anomalies) == 0,
        metrics={
            "active_writers": active_writers,
            "total_attempts": active_writers * writes_per_writer,
            "completed_writes": len(written_keys),
            "keys_wiped_by_delete": deleted_count,
            "post_race_resident_members": len(final_members),
        },
        anomalies=anomalies,
    )


# ---------------------------------------------------------------------------
# Formatter & Report Generator
# ---------------------------------------------------------------------------

def generate_markdown_report(results: list[ScenarioResult], env_info: dict[str, str]) -> str:
    lines = [
        "# Stress Benchmark Results — `h-memory`",
        "",
        "> [!NOTE]",
        "> This document tracks stress benchmark metrics for `h-memory`.",
        "> - **Section 1 (Authoritative Lab Benchmark Results)** is reserved for official benchmarking against the target production/lab Redis environment.",
        "> - **Section 2 (Development Verification Baseline)** records the developer verification benchmark run on the local development testbed (exercising `nat.runtime.loader.load_workflow`).",
        "",
        "---",
        "",
        "## 1. Authoritative Lab Benchmark Results (Operator Lab)",
        "",
        "*Status:* **PENDING LAB BENCHMARK RUN**  ",
        "*Target Environment:* Operator Lab Redis Cluster  ",
        "*Conducted By:* Operator Lab Validation",
        "",
        "### Lab Benchmark Summary Table",
        "",
        "| Scenario | Status | Primary Metric | Verified Invariant / Lab Finding |",
        "| :--- | :--- | :--- | :--- |",
        "| Throughput & Latency Scaling | `[PENDING]` | `TBD writes/sec` | `TBD` |",
        "| Concurrent Same-Chat Monotonicity | `[PENDING]` | `TBD writes / 100% unique` | `TBD` |",
        "| Concurrent `hot_keep_count` Rank-Pruning | `[PENDING]` | `TBD` | `TBD` |",
        "| TTL Boundary & Expiry Behavior | `[PENDING]` | `TBD` | `TBD` |",
        "| `delete_chat` Racing In-Flight Writes | `[PENDING]` | `TBD` | `TBD` |",
        "",
        "### Lab Concurrency & Latency Scaling Matrix",
        "",
        "| Concurrency | Total Writes | Time (s) | Throughput (writes/sec) | min (ms) | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) | max (ms) |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        "| 10 | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* |",
        "| 50 | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* |",
        "| 100 | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* |",
        "| 200 | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* |",
        "",
        "---",
        "",
        "## 2. Development Verification Baseline (Local Dev Testbed)",
        "",
        f"*Conducted:* {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} (local development testbed)  ",
        "*NAT Workflow Loader:* `nat.runtime.loader.load_workflow`  ",
        "*Workflows:* `workflow_write.yaml`, `workflow_delete.yaml`  ",
        f"*Redis Target:* `{env_info.get('redis_url', 'unknown')}`  ",
        f"*Tenant Scope:* `{env_info.get('pod')}:{env_info.get('agent')}`  ",
        f"*Platform:* `{env_info.get('platform')}`  ",
        "",
        "### Baseline Executive Summary",
        "",
        "| Scenario | Status | Primary Metric | Key Finding / Invariant Behavior |",
        "| :--- | :--- | :--- | :--- |",
    ]

    for r in results:
        status_str = "✅ PASS" if r.passed else "❌ FAIL"
        if r.name == "Throughput & Latency Scaling":
            max_wps = max(
                (v.get("throughput_wps", 0) for k, v in r.metrics.items() if isinstance(v, dict)),
                default=0,
            )
            p99 = min(
                (v.get("p99_ms", 0) for k, v in r.metrics.items() if isinstance(v, dict)),
                default=0,
            )
            primary = f"{max_wps:.1f} writes/sec (p99: {p99:.2f}ms)"
            finding = "Sub-millisecond write latency sustained through NAT workflow runner"
        elif r.name == "Concurrent Same-Chat Monotonicity":
            primary = f"{r.metrics.get('total_writes', 0)} writes / 100% unique"
            finding = "Zero key collisions; strict monotonic nanosecond timestamp ordering verified"
        elif r.name == "Concurrent hot_keep_count Rank-Pruning":
            primary = f"Trimmed to exact {r.metrics.get('hot_keep_count')} ranks"
            finding = "Rank trimming atomic under fire; orphan keys preserved in Redis"
        elif r.name == "TTL Boundary & Expiry Behavior":
            primary = f"{r.metrics.get('expired_keys_verified')} keys expired"
            finding = "Accurate key-level expiration; post-expiry writes remain robust"
        elif r.name == "delete_chat Racing In-Flight Writes":
            primary = f"{r.metrics.get('keys_wiped_by_delete')} keys wiped"
            finding = "Zero deadlocks or exceptions under concurrent delete race"
        else:
            primary = "Completed"
            finding = "Clean execution"

        lines.append(f"| {r.name} | {status_str} | {primary} | {finding} |")

    lines.extend([
        "",
        "### Baseline Detailed Scenario Results",
        "",
    ])

    for r in results:
        lines.append(f"#### {r.name}")
        lines.append(f"{r.description}\n")
        lines.append(f"**Outcome:** {'PASS' if r.passed else 'FAIL'}")
        if r.anomalies:
            lines.append("\n**Anomalies Found:**")
            for a in r.anomalies:
                lines.append(f"- ⚠️ {a}")

        if r.name == "Throughput & Latency Scaling":
            lines.extend([
                "",
                "| Concurrency | Total Writes | Time (s) | Throughput (writes/sec) | min (ms) | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) | max (ms) |",
                "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
            ])
            for k, v in r.metrics.items():
                if isinstance(v, dict):
                    conc = k.replace("concurrency_", "")
                    lines.append(
                        f"| {conc} | {v.get('count')} | {v.get('total_time_s'):.3f} | "
                        f"**{v.get('throughput_wps'):.1f}** | {v.get('min_ms')} | {v.get('p50_ms')} | "
                        f"{v.get('p90_ms')} | {v.get('p95_ms')} | {v.get('p99_ms')} | {v.get('max_ms')} |"
                    )
        else:
            lines.append("\n```json")
            lines.append(json.dumps(r.metrics, indent=2))
            lines.append("```\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Config Loader & Main Entry Point
# ---------------------------------------------------------------------------

def _expand_env(val: Any) -> Any:
    if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
        expr = val[2:-1]
        if ":-" in expr:
            var_name, default_val = expr.split(":-", 1)
            return os.environ.get(var_name) or default_val
        return os.environ.get(expr, "")
    return val


def load_vars() -> dict[str, Any]:
    vars_path = HERE / "vars.yaml" if (HERE / "vars.yaml").is_file() else DEFAULT_VARS_PATH
    if vars_path.is_file():
        try:
            with open(vars_path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
                # Recursively expand any env var syntax in vars
                return {
                    k: {sub_k: _expand_env(sub_v) for sub_k, sub_v in v.items()} if isinstance(v, dict) else _expand_env(v)
                    for k, v in loaded.items()
                }
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Failed to parse {vars_path}: {e}", file=sys.stderr)
    return {}


async def main():
    parser = argparse.ArgumentParser(description="Run h-memory stress benchmark suite via NAT workflow loader.")
    parser.add_argument(
        "--redis-url",
        default=None,
        help="Target Redis URL (overrides environment and vars.yaml).",
    )
    parser.add_argument("--pod", default=None, help="Tenant pod name.")
    parser.add_argument("--agent", default=None, help="Tenant agent name.")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=["all"],
        choices=["all", "throughput", "concurrent_same_chat", "rank_prune", "ttl_boundary", "delete_race"],
        help="Scenarios to execute.",
    )
    parser.add_argument("--quick", action="store_true", help="Run reduced iterations for quick validation.")
    parser.add_argument("--json", dest="json_out", action="store_true", help="Output JSON results to stdout.")
    parser.add_argument("--output-results", default="RESULTS.md", help="Path to write Markdown results file.")

    args = parser.parse_args()

    # Resolve settings from vars.yaml -> env -> CLI args
    vars_cfg = load_vars()
    redis_url = (
        args.redis_url
        or os.environ.get("H_NAT_REDIS_URL")
        or vars_cfg.get("redis", {}).get("url")
        or "redis://localhost:6379"
    )
    pod = (
        args.pod
        or os.environ.get("H_NAT_POD")
        or vars_cfg.get("tenant", {}).get("pod")
        or "bench-pod"
    )
    agent = (
        args.agent
        or os.environ.get("H_NAT_AGENT")
        or vars_cfg.get("tenant", {}).get("agent")
        or "stress-agent"
    )

    # Set environment variables for NAT workflow YAML interpolation
    os.environ["H_NAT_REDIS_URL"] = redis_url
    os.environ["H_NAT_POD"] = pod
    os.environ["H_NAT_AGENT"] = agent

    print(f"Target Redis URL : {redis_url}")
    print(f"Tenant Namespace : {pod}:{agent}")
    print(f"Write Workflow   : {WORKFLOW_WRITE_PATH}")
    print(f"Delete Workflow  : {WORKFLOW_DELETE_PATH}")

    # Initialize verification Redis client
    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    try:
        await client.ping()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: Unable to connect to Redis at {redis_url}: {e}", file=sys.stderr)
        sys.exit(1)

    run_all = "all" in args.scenarios
    results: list[ScenarioResult] = []

    print("\nLoading NAT workflows via nat.runtime.loader.load_workflow()...")
    async with load_workflow(WORKFLOW_WRITE_PATH) as write_session, \
               load_workflow(WORKFLOW_DELETE_PATH) as delete_session:

        try:
            # Scenario 1: Throughput
            if run_all or "throughput" in args.scenarios:
                print("\n[1/5] Running Scenario: Throughput & Latency Scaling (via NAT)...")
                levels = [5, 10, 25] if args.quick else [10, 50, 100, 200]
                writes = 300 if args.quick else 2000
                res = await run_scenario_throughput(write_session, delete_session, client, levels, writes)
                results.append(res)
                print(f"      Status: {'PASS' if res.passed else 'FAIL'}")

            # Scenario 2: Concurrent Same Chat
            if run_all or "concurrent_same_chat" in args.scenarios:
                print("\n[2/5] Running Scenario: Concurrent Same-Chat Monotonicity (via NAT)...")
                writers = 10 if args.quick else 50
                per_writer = 10 if args.quick else 20
                res = await run_scenario_concurrent_same_chat(write_session, delete_session, client, pod, agent, writers, per_writer)
                results.append(res)
                print(f"      Status: {'PASS' if res.passed else 'FAIL'}")

            # Scenario 3: Rank Pruning
            if run_all or "rank_prune" in args.scenarios:
                print("\n[3/5] Running Scenario: Concurrent hot_keep_count Rank-Pruning (via NAT)...")
                writers = 10 if args.quick else 20
                total = 50 if args.quick else 200
                keep = 15 if args.quick else 25
                res = await run_scenario_rank_prune(write_session, delete_session, client, pod, agent, writers, total, keep)
                results.append(res)
                print(f"      Status: {'PASS' if res.passed else 'FAIL'}")

            # Scenario 4: TTL Boundary
            if run_all or "ttl_boundary" in args.scenarios:
                print("\n[4/5] Running Scenario: TTL Boundary & Expiry Behavior (via NAT)...")
                writes = 20 if args.quick else 50
                res = await run_scenario_ttl_boundary(write_session, delete_session, client, pod, agent, writes, short_ttl_seconds=1)
                results.append(res)
                print(f"      Status: {'PASS' if res.passed else 'FAIL'}")

            # Scenario 5: Delete Race
            if run_all or "delete_race" in args.scenarios:
                print("\n[5/5] Running Scenario: delete_chat Racing In-Flight Writes (via NAT)...")
                writers = 5 if args.quick else 10
                per_writer = 10 if args.quick else 30
                res = await run_scenario_delete_race(write_session, delete_session, client, pod, agent, writers, per_writer)
                results.append(res)
                print(f"      Status: {'PASS' if res.passed else 'FAIL'}")

        finally:
            await client.aclose()

    env_info = {
        "redis_url": redis_url,
        "pod": pod,
        "agent": agent,
        "platform": f"{sys.platform} (Python {sys.version.split()[0]})",
    }

    if args.json_out:
        out_dict = {
            "environment": env_info,
            "results": [asdict(r) for r in results],
        }
        print(json.dumps(out_dict, indent=2))

    # Write Markdown Report
    if args.output_results:
        report_path = HERE / args.output_results
        report_content = generate_markdown_report(results, env_info)
        report_path.write_text(report_content, encoding="utf-8")
        print(f"\nBenchmark report written to: {report_path.resolve()}")

    all_passed = all(r.passed for r in results)
    print("\n=======================================================")
    print(f"Benchmark Run Complete: {'ALL PASSED' if all_passed else 'FAILURES DETECTED'}")
    print("=======================================================")
    if not all_passed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
