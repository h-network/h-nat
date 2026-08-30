"""Accuracy and stress benchmark suite for h-recall NAT plugin.

Exercises 4 comprehensive benchmark scenarios through NAT workflow YAMLs:
1. Semantic Discrimination / Confusion Matrix (Ambiguous & Similar-Sounding Facts)
2. Volume Scaling & Search Degradation (50 to 500+ documents in noisy haystack)
3. Concurrent Write / Sweep / Vectorize Interleaving (Race conditions & zero-loss audit)
4. Adversarial Query Sanitization & RediSearch Injection Resistance
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
import yaml
from nat.plugins.h_memory import BoundedBufferStore
from nat.plugins.h_network_semantic_memory._internal.sanitize import (
    escape_redisearch_query,
)
from nat.runtime.loader import load_workflow

HERE = Path(__file__).resolve().parent
DEFAULT_VARS_PATH = HERE / "vars.example.yaml"
WORKFLOW_SWEEP_PATH = HERE / "workflow_sweep.yaml"
WORKFLOW_VEC_PATH = HERE / "workflow_vectorize.yaml"
WORKFLOW_SEARCH_PATH = HERE / "workflow_search.yaml"


def percentile(values: list[float], pct: float) -> float:
    """Compute percentile from a list of floats without third-party dependencies."""
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct / 100.0
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return ordered[low]
    weight = pos - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def load_benchmark_config() -> dict[str, Any]:
    config: dict[str, Any] = {
        "redis_url": "redis://172.16.10.102:6379",
        "hot_redis_url": None,
        "pod": "bench_recall",
        "agent": "assistant",
        "scale_doc_count": 500,
        "concurrency_workers": 8,
        "vectorize_batch_size": 64,
        "rrf_k": 60,
        "candidate_pool_multiplier": 2,
    }
    user_vars = HERE / "vars.yaml"
    if user_vars.is_file():
        try:
            with open(user_vars, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
                config.update({k: v for k, v in loaded.items() if v is not None})
        except (OSError, yaml.YAMLError) as e:
            print(f"[WARN] Failed to load {user_vars}: {e}", file=sys.stderr)
    elif DEFAULT_VARS_PATH.is_file():
        try:
            with open(DEFAULT_VARS_PATH, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
                # Only take scale/concurrency defaults from example vars, keep lab default if placeholder
                for k, v in loaded.items():
                    if k == "redis_url" and v == "redis://127.0.0.1:6379":
                        continue
                    if v is not None:
                        config[k] = v
        except (OSError, yaml.YAMLError) as e:
            print(f"[WARN] Failed to load {DEFAULT_VARS_PATH}: {e}", file=sys.stderr)

    # CLI env overrides
    if "H_NAT_REDIS_URL" in os.environ:
        config["redis_url"] = os.environ["H_NAT_REDIS_URL"]
    return config


async def invoke_nat(manager: Any, payload: dict[str, Any] | str) -> Any:
    """Execute a NAT workflow via its session runner."""
    input_str = payload if isinstance(payload, str) else json.dumps(payload)
    async with manager.session() as session, session.run(input_str) as runner:
        raw_result = await runner.result(to_type=str)
        try:
            return json.loads(raw_result)
        except (json.JSONDecodeError, TypeError):
            return raw_result


# ===========================================================================
# Scenario 1: Semantic Discrimination & Near-Neighbor Confusion Matrix
# ===========================================================================

async def run_scenario_1(config: dict[str, Any]) -> dict[str, Any]:
    print("\n" + "=" * 78)
    print("  [Scenario 1] Semantic Discrimination & Near-Neighbor Confusion Matrix")
    print("=" * 78)

    nonce = secrets.token_hex(4)
    chat_id = f"bench-s1-{nonce}"
    redis_url = config["redis_url"]
    pod = config["pod"]
    agent = config["agent"]

    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    mem_store = BoundedBufferStore(client, pod=pod, agent=agent, ttl_seconds_max=86400)

    # 3 clusters of confusing/similar facts:
    # Cluster A: Cryptographic Algorithms
    # Cluster B: Datacenter Disaster Recovery Roles
    # Cluster C: Infrastructure Oncall & Escalation Roles
    confusion_sets = [
        # Cluster A
        {
            "cluster": "A (Crypto)",
            "facts": [
                ("A1", "Project Nova uses quantum-resistant lattice-based cryptography for internal key exchange.", "lattice-based cryptography"),
                ("A2", "Project Nova uses elliptic-curve Diffie-Hellman (ECDH P-384) for legacy edge endpoints.", "elliptic-curve Diffie-Hellman"),
                ("A3", "Project Nova uses RSA-4096 certificates strictly for root CA signing.", "RSA-4096 certificates"),
                ("A4", "Project Nova uses SPHINCS+ hash-based signatures for firmware integrity verification.", "SPHINCS+ hash-based signatures"),
            ],
            "queries": [
                ("Which cryptography is used for internal key exchange?", "A1", "lattice"),
                ("Which algorithm secures legacy edge endpoints?", "A2", "elliptic"),
                ("What key type is used for root CA signing?", "A3", "RSA-4096"),
                ("How is firmware integrity verified?", "A4", "SPHINCS"),
            ]
        },
        # Cluster B
        {
            "cluster": "B (Datacenters)",
            "facts": [
                ("B1", "The Reykjavik facility (ICE-7) serves as our cold disaster recovery backup site.", "Reykjavik facility ICE-7"),
                ("B2", "The Helsinki facility (HEL-2) operates as our high-throughput edge telemetry ingestion gateway.", "Helsinki facility HEL-2"),
                ("B3", "The Oslo facility (OSL-3) runs batch analytics and machine learning model fine-tuning.", "Oslo facility OSL-3"),
                ("B4", "The Stockholm facility (STO-4) hosts our primary active-active transactional database cluster.", "Stockholm facility STO-4"),
            ],
            "queries": [
                ("Where is our cold disaster recovery backup facility located?", "B1", "Reykjavik"),
                ("Which datacenter handles edge telemetry ingestion?", "B2", "Helsinki"),
                ("Which location runs batch analytics and model tuning?", "B3", "Oslo"),
                ("Where is the primary active-active transactional database hosted?", "B4", "Stockholm"),
            ]
        },
        # Cluster C
        {
            "cluster": "C (Oncall Roles)",
            "facts": [
                ("C1", "Alice is the primary oncall engineer responsible for immediate pager triage.", "Alice primary oncall"),
                ("C2", "Bob is the secondary oncall engineer providing escalation support for complex incidents.", "Bob secondary oncall"),
                ("C3", "Carol leads the specialized network core escalation and BGP routing team.", "Carol network escalation"),
                ("C4", "Dave acts as the incident commander for severe customer-facing P0 outages.", "Dave incident commander"),
            ],
            "queries": [
                ("Who is assigned as primary oncall for immediate pager triage?", "C1", "Alice"),
                ("Who provides secondary oncall escalation support?", "C2", "Bob"),
                ("Who handles network core escalation and BGP issues?", "C3", "Carol"),
                ("Who serves as incident commander for P0 outages?", "C4", "Dave"),
            ]
        }
    ]

    # Plant turns in hot memory
    planted_count = 0
    for cset in confusion_sets:
        for tag, text, _ in cset["facts"]:
            await mem_store.write_turn(chat_id, "user", text, ttl_seconds=86400)
            planted_count += 1
    print(f"  + Planted {planted_count} overlapping facts across 3 confusion clusters")

    # Let age pass migration threshold (1s in workflow_sweep.yaml)
    await asyncio.sleep(1.1)

    # Sweep via NAT workflow
    async with load_workflow(WORKFLOW_SWEEP_PATH) as sweep_mgr:
        sweep_res = await invoke_nat(sweep_mgr, {"chat_ids": [chat_id]})
    print(f"  + Migrated to audit tier via NAT workflow: {sweep_res.get('migrated')} turns")

    # Vectorize via NAT workflow
    async with load_workflow(WORKFLOW_VEC_PATH) as vec_mgr:
        vec_res = await invoke_nat(vec_mgr, {"batch_size": 32})
    print(f"  + Vectorized audit documents via NAT workflow: {vec_res.get('vectorized')} docs")

    # Evaluate queries via NAT search workflow
    results_table = []
    top1_correct = 0
    top3_correct = 0
    reciprocal_ranks = []

    print("\n  Evaluating Near-Neighbor Discrimination Queries:")
    async with load_workflow(WORKFLOW_SEARCH_PATH) as search_mgr:
        for cset in confusion_sets:
            print(f"\n  --- Cluster {cset['cluster']} ---")
            for qtext, expected_tag, target_keyword in cset["queries"]:
                search_payload = {
                    "chat_id": chat_id,
                    "query": qtext,
                    "top_k": 5,
                    "mode": "hybrid",
                }
                hits = await invoke_nat(search_mgr, search_payload)
                if not isinstance(hits, list):
                    hits = []
                ranks = [h.get("content", "") for h in hits]

                target_rank = None
                for idx, content in enumerate(ranks, start=1):
                    if target_keyword.casefold() in content.casefold():
                        target_rank = idx
                        break

                is_top1 = (target_rank == 1)
                is_top3 = (target_rank is not None and target_rank <= 3)
                rr = (1.0 / target_rank) if target_rank else 0.0

                if is_top1:
                    top1_correct += 1
                if is_top3:
                    top3_correct += 1
                reciprocal_ranks.append(rr)

                status = "PASS (Rank 1)" if is_top1 else f"RANK {target_rank}"
                print(f"    Q: \"{qtext[:50]}...\" -> {status} (MRR: {rr:.3f})")
                results_table.append({
                    "cluster": cset["cluster"],
                    "query": qtext,
                    "expected": target_keyword,
                    "rank": target_rank,
                    "rr": rr,
                })

    total_queries = len(reciprocal_ranks)
    mrr = sum(reciprocal_ranks) / total_queries if total_queries else 0.0
    acc_top1 = (top1_correct / total_queries) * 100.0 if total_queries else 0.0
    acc_top3 = (top3_correct / total_queries) * 100.0 if total_queries else 0.0

    print("\n  [Scenario 1 Results Summary]")
    print(f"    - Top-1 Accuracy: {top1_correct}/{total_queries} ({acc_top1:.1f}%)")
    print(f"    - Top-3 Recall:   {top3_correct}/{total_queries} ({acc_top3:.1f}%)")
    print(f"    - Mean Reciprocal Rank (MRR): {mrr:.4f}")

    await client.aclose()
    return {
        "top1_accuracy": acc_top1,
        "top3_recall": acc_top3,
        "mrr": mrr,
        "details": results_table,
    }


# ===========================================================================
# Scenario 2: Volume Scaling & Search Degradation
# ===========================================================================

async def run_scenario_2(config: dict[str, Any]) -> dict[str, Any]:
    scale_count = int(config.get("scale_doc_count", 500))
    print("\n" + "=" * 78)
    print(f"  [Scenario 2] Volume Scaling & Search Degradation ({scale_count} documents)")
    print("=" * 78)

    nonce = secrets.token_hex(4)
    chat_id = f"bench-s2-{nonce}"
    redis_url = config["redis_url"]
    pod = config["pod"]
    agent = config["agent"]

    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    mem_store = BoundedBufferStore(client, pod=pod, agent=agent, ttl_seconds_max=86400)

    # Haystack background templates
    log_templates = [
        "Border router BGP neighbor 192.0.2.{i} session state changed to Established, received {n} prefixes.",
        "Kafka consumer group billing-events lag increased to {n} ms on partition {i}.",
        "Redis cluster replica node-redis-{i}.internal synced replication offset {n} with master.",
        "Kubernetes daemonset node-exporter rolled out update v1.8.{i} successfully across worker pool.",
        "PostgreSQL autovacuum worker finished vacuuming table analytics_events_{i} (scanned {n} pages).",
        "OpenSSH server accepted publickey authentication from administrator on port 222{i}.",
        "Envoy proxy upstream cluster auth-service healthcheck succeeded (RTT {i}.{n} ms).",
    ]

    # Plant needles at 10%, 50%, 90% depth
    needles = {
        int(scale_count * 0.10): ("Needle-10%", "The secret vault encryption key passphrase is SolarWind-Alpha-99.", "SolarWind-Alpha-99"),
        int(scale_count * 0.50): ("Needle-50%", "The emergency override DNS server IP is 198.51.100.254 for offline recovery.", "198.51.100.254"),
        int(scale_count * 0.90): ("Needle-90%", "The decommissioned legacy mainframe hostname was VAX-Cluster-Obsidian.", "VAX-Cluster-Obsidian"),
    }

    print(f"  + Generating and planting {scale_count} background documents into hot memory...")
    t0_plant = time.perf_counter()
    for idx in range(scale_count):
        if idx in needles:
            _, text, _ = needles[idx]
        else:
            tmpl = log_templates[idx % len(log_templates)]
            text = tmpl.format(i=idx, n=(idx * 37) % 999)
        await mem_store.write_turn(chat_id, "user", text, ttl_seconds=86400)
    t_plant = time.perf_counter() - t0_plant
    print(f"  + Planted {scale_count} turns in {t_plant:.2f}s ({scale_count / t_plant:.0f} turns/s)")

    # Let age pass migration threshold
    await asyncio.sleep(1.1)

    # Measure Sweep Migration via NAT workflow
    t0_sweep = time.perf_counter()
    async with load_workflow(WORKFLOW_SWEEP_PATH) as sweep_mgr:
        sweep_res = await invoke_nat(sweep_mgr, {"chat_ids": [chat_id]})
    t_sweep = time.perf_counter() - t0_sweep
    migrated_count = sweep_res.get("migrated", 0)
    sweep_rate = migrated_count / t_sweep if t_sweep > 0 else 0
    print(f"  + Sweep Migration (NAT workflow): {migrated_count} docs migrated in {t_sweep:.3f}s ({sweep_rate:.0f} docs/s)")

    # Measure Vectorization via NAT workflow
    t0_vec = time.perf_counter()
    async with load_workflow(WORKFLOW_VEC_PATH) as vec_mgr:
        vec_res = await invoke_nat(vec_mgr, {"batch_size": config.get("vectorize_batch_size", 64)})
    t_vec = time.perf_counter() - t0_vec
    vec_count = vec_res.get("vectorized", 0)
    vec_rate = vec_count / t_vec if t_vec > 0 else 0
    print(f"  + Batch Vectorization (NAT workflow): {vec_count} docs embedded in {t_vec:.3f}s ({vec_rate:.1f} docs/s, {vec_res.get('batches')} batches)")

    # Measure Search Latency & Accuracy under scale via NAT search workflow
    search_queries = [
        ("What is the secret vault encryption key passphrase?", "SolarWind-Alpha-99"),
        ("What is the emergency override DNS server IP?", "198.51.100.254"),
        ("What was the legacy decommissioned mainframe hostname?", "VAX-Cluster-Obsidian"),
        ("Find BGP neighbor session state established prefixes", "BGP"),
        ("Check Kafka consumer group billing events lag", "Kafka"),
        ("Check PostgreSQL autovacuum worker analytics events", "PostgreSQL"),
    ]

    latencies_ms = []
    needle_recalls = []

    print("\n  Benchmarking Search Latency & Needle Retrieval via NAT search workflow:")
    async with load_workflow(WORKFLOW_SEARCH_PATH) as search_mgr:
        for qtext, expected_keyword in search_queries:
            # Run 5 iterations per query for latency distribution
            q_latencies = []
            target_found_top1 = False
            target_found_top5 = False

            for _ in range(5):
                t0 = time.perf_counter()
                hits = await invoke_nat(search_mgr, {"chat_id": chat_id, "query": qtext, "top_k": 5, "mode": "hybrid"})
                dt_ms = (time.perf_counter() - t0) * 1000.0
                q_latencies.append(dt_ms)
                latencies_ms.append(dt_ms)

            if not isinstance(hits, list):
                hits = []
            top_hit = hits[0].get("content", "") if hits else ""
            all_hits = " ".join([h.get("content", "") for h in hits])

            if expected_keyword.casefold() in top_hit.casefold():
                target_found_top1 = True
            if expected_keyword.casefold() in all_hits.casefold():
                target_found_top5 = True

            needle_recalls.append((target_found_top1, target_found_top5))
            p50_q = percentile(q_latencies, 50)
            print(f"    Q: \"{qtext[:45]}...\" -> p50: {p50_q:.2f}ms | Top-1: {'PASS' if target_found_top1 else 'MISS'}")

    p50 = percentile(latencies_ms, 50)
    p95 = percentile(latencies_ms, 95)
    p99 = percentile(latencies_ms, 99)
    mean_lat = sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0

    recall_top1_pct = (sum(1 for r1, _ in needle_recalls if r1) / len(needle_recalls)) * 100.0
    recall_top5_pct = (sum(1 for _, r5 in needle_recalls if r5) / len(needle_recalls)) * 100.0

    print("\n  [Scenario 2 Results Summary]")
    print(f"    - Search Latency Mean: {mean_lat:.2f} ms")
    print(f"    - Search Latency p50:  {p50:.2f} ms")
    print(f"    - Search Latency p95:  {p95:.2f} ms")
    print(f"    - Search Latency p99:  {p99:.2f} ms")
    print(f"    - Target Recall@1:    {recall_top1_pct:.1f}%")
    print(f"    - Target Recall@5:    {recall_top5_pct:.1f}%")

    await client.aclose()
    return {
        "doc_count": scale_count,
        "sweep_docs_per_sec": sweep_rate,
        "vectorize_docs_per_sec": vec_rate,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "latency_p99_ms": p99,
        "recall_at_1": recall_top1_pct,
        "recall_at_5": recall_top5_pct,
    }


# ===========================================================================
# Scenario 3: Concurrent Writes vs Sweep / Vectorize Interleaving
# ===========================================================================

async def run_scenario_3(config: dict[str, Any]) -> dict[str, Any]:
    concurrency_workers = int(config.get("concurrency_workers", 8))
    turns_per_worker = 25
    total_turns = concurrency_workers * turns_per_worker

    print("\n" + "=" * 78)
    print(f"  [Scenario 3] Concurrent Writes vs Sweep/Vectorize Stress ({concurrency_workers} workers, {total_turns} turns)")
    print("=" * 78)

    nonce = secrets.token_hex(4)
    chat_id = f"bench-s3-{nonce}"
    redis_url = config["redis_url"]
    pod = config["pod"]
    agent = config["agent"]

    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    mem_store = BoundedBufferStore(client, pod=pod, agent=agent, ttl_seconds_max=86400)

    stop_background = asyncio.Event()
    sweep_counts = {"migrated": 0, "skipped_existing": 0, "scanned": 0}
    vec_counts = {"vectorized": 0}

    # Background worker running sweeps via NAT workflow
    async def background_sweeper(sweep_mgr: Any):
        while not stop_background.is_set():
            res = await invoke_nat(sweep_mgr, {"chat_ids": [chat_id]})
            if isinstance(res, dict):
                sweep_counts["migrated"] += res.get("migrated", 0)
                sweep_counts["skipped_existing"] += res.get("skipped_existing", 0)
                sweep_counts["scanned"] += res.get("scanned", 0)
            await asyncio.sleep(0.1)

    # Background worker running vectorize via NAT workflow
    async def background_vectorizer(vec_mgr: Any):
        while not stop_background.is_set():
            res = await invoke_nat(vec_mgr, {"batch_size": 32})
            if isinstance(res, dict):
                vec_counts["vectorized"] += res.get("vectorized", 0)
            await asyncio.sleep(0.1)

    # Concurrent writer worker
    async def writer_worker(worker_id: int):
        for i in range(turns_per_worker):
            msg = f"Worker-{worker_id} event {i}: verification message payload nonce={secrets.token_hex(3)}"
            await mem_store.write_turn(chat_id, "user", msg, ttl_seconds=86400)
            await asyncio.sleep(0.02)

    print(f"  + Launching {concurrency_workers} concurrent writer workers + continuous NAT sweeper & vectorizer...")
    t0 = time.perf_counter()

    async with load_workflow(WORKFLOW_SWEEP_PATH) as sweep_mgr, load_workflow(WORKFLOW_VEC_PATH) as vec_mgr:
        sweeper_task = asyncio.create_task(background_sweeper(sweep_mgr))
        vectorizer_task = asyncio.create_task(background_vectorizer(vec_mgr))

        # Run writers concurrently
        await asyncio.gather(*[writer_worker(w) for w in range(concurrency_workers)])

        # Drain backlog: wait for turns to cross 1s threshold, then stop workers
        await asyncio.sleep(1.2)
        stop_background.set()
        await asyncio.gather(sweeper_task, vectorizer_task, return_exceptions=True)

        # Final sweep and vectorization pass to ensure total drain
        final_sweep = await invoke_nat(sweep_mgr, {"chat_ids": [chat_id]})
        if isinstance(final_sweep, dict):
            sweep_counts["migrated"] += final_sweep.get("migrated", 0)
            sweep_counts["skipped_existing"] += final_sweep.get("skipped_existing", 0)

        final_vec = await invoke_nat(vec_mgr, {"batch_size": 64})
        if isinstance(final_vec, dict):
            vec_counts["vectorized"] += final_vec.get("vectorized", 0)

    t_total = time.perf_counter() - t0

    # Verify invariants
    escaped_cid = escape_redisearch_query(chat_id, escape_whitespace=True)
    idx_name = f"{pod}:{agent}:chat-audit:idx"
    q = client.ft(idx_name)
    audit_docs_res = await q.search(f"@chat_id:{{{escaped_cid}}}")
    audit_doc_count = audit_docs_res.total

    pending_res = await q.search(f"@chat_id:{{{escaped_cid}}} @pending_vectorize:{{1}}")
    pending_count = pending_res.total

    hot_index_key = f"{pod}:{agent}:chat-index:{chat_id}"
    hot_remaining = await client.zcard(hot_index_key)

    turn_loss_rate = max(0, total_turns - audit_doc_count) / total_turns * 100.0
    duplicate_count = max(0, audit_doc_count - total_turns)

    print("\n  [Scenario 3 Results Summary]")
    print(f"    - Total Injected:     {total_turns} turns in {t_total:.2f}s")
    print(f"    - Audit Tier Count:   {audit_doc_count} docs")
    print(f"    - Turn Loss Rate:     {turn_loss_rate:.2f}% (0% expected)")
    print(f"    - Duplicate Records:  {duplicate_count} (0 expected)")
    print(f"    - Idempotency Hits:   {sweep_counts['skipped_existing']} hits")
    print(f"    - Pending Flags Left: {pending_count} docs (0 expected)")
    print(f"    - Hot ZSET Leftover:  {hot_remaining} members (0 expected)")

    assert audit_doc_count == total_turns, f"Data loss: expected {total_turns}, got {audit_doc_count}"
    assert pending_count == 0, f"Pending flags leftover: {pending_count}"

    await client.aclose()
    return {
        "total_injected": total_turns,
        "audit_count": audit_doc_count,
        "turn_loss_rate": turn_loss_rate,
        "duplicate_count": duplicate_count,
        "idempotency_hits": sweep_counts["skipped_existing"],
        "pending_leftover": pending_count,
    }


# ===========================================================================
# Scenario 4: Adversarial Query Sanitization & Injection Stress
# ===========================================================================

async def run_scenario_4(config: dict[str, Any]) -> dict[str, Any]:
    print("\n" + "=" * 78)
    print("  [Scenario 4] Adversarial Query Sanitization & RediSearch Injection Stress")
    print("=" * 78)

    nonce = secrets.token_hex(4)
    chat_id = f"bench-s4-{nonce}"
    redis_url = config["redis_url"]
    pod = config["pod"]
    agent = config["agent"]

    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    mem_store = BoundedBufferStore(client, pod=pod, agent=agent, ttl_seconds_max=86400)

    # Plant a baseline document
    await mem_store.write_turn(chat_id, "user", "Normal operational record for adversarial test target.", ttl_seconds=86400)
    await asyncio.sleep(1.1)
    async with load_workflow(WORKFLOW_SWEEP_PATH) as sweep_mgr:
        await invoke_nat(sweep_mgr, {"chat_ids": [chat_id]})
    async with load_workflow(WORKFLOW_VEC_PATH) as vec_mgr:
        await invoke_nat(vec_mgr, {})

    adversarial_payloads = [
        # Syntax / Punctuation attacks
        "(((([[[[****;;;:::\"\"\"&&&|||!!!~~~",
        "@chat_id:{admin} @role:{system} *",
        "*=>[KNN 100 @embedding $vec]",
        "@content:(secret) | @role:{*}",
        "-@chat_id:* -@content:* +foo",
        "foo && || ! ( ) { } [ ] ^ \" ~ * ? : \\",
        "SELECT * FROM users WHERE '1'='1';",
        "<script>alert('xss')</script>",
        "../../../../etc/passwd\0",
        "\x00\x01\x02\x03\x04\x05\x06\x07",
        "🔥 🚀 💥 Unicode Emoji Flood 💯 🛡️ 🎯",
        "\\x80\\xff\\xfe\\xfd",
        " ' OR 1=1 -- ",
        "\" OR \"a\"=\"a",
        "@role:{user|admin|system}",
        "foo*",
        "*foo*",
        "?foo?",
        "~foo~",
        "%foo%",
        "tag:{val1|val2}",
        "(unbalanced paren (nested (deeply",
        "[unbalanced bracket [nested",
        "{unbalanced brace {nested",
        "\"unbalanced quote \" nested \"",
    ]

    tested_count = 0
    passed_count = 0
    failures = []

    print(f"  Testing {len(adversarial_payloads)} adversarial RediSearch query patterns via NAT search workflow...")
    async with load_workflow(WORKFLOW_SEARCH_PATH) as search_mgr:
        for payload in adversarial_payloads:
            tested_count += 1
            try:
                hits = await invoke_nat(search_mgr, {"chat_id": chat_id, "query": payload, "top_k": 3, "mode": "hybrid"})
                if not isinstance(hits, list):
                    hits = []
                passed_count += 1
                print(f"    [{tested_count:02d}/{len(adversarial_payloads):02d}] PASS: {payload[:35]!r} (hits: {len(hits)})")
            except Exception as exc:  # noqa: BLE001
                failures.append((payload, str(exc)))
                print(f"    [{tested_count:02d}/{len(adversarial_payloads):02d}] FAIL: {payload[:35]!r} -> {exc}")

    print("\n  [Scenario 4 Results Summary]")
    print(f"    - Tested Payloads: {tested_count}")
    print(f"    - Safe Executions: {passed_count}/{tested_count} ({(passed_count/tested_count)*100:.1f}%)")
    print(f"    - Syntax / Injection Errors: {len(failures)}")

    assert len(failures) == 0, f"Adversarial query injection failures: {failures}"

    await client.aclose()
    return {
        "tested": tested_count,
        "passed": passed_count,
        "failures": failures,
    }


# ===========================================================================
# Main Benchmark Driver
# ===========================================================================

async def main_async() -> int:
    parser = argparse.ArgumentParser(description="h-recall Accuracy & Stress Benchmark Suite")
    parser.add_argument("--scenario", choices=["1", "2", "3", "4", "all"], default="all", help="Scenario to execute")
    parser.add_argument("--scale", type=int, default=None, help="Document scale for Scenario 2")
    parser.add_argument("--workers", type=int, default=None, help="Concurrency workers for Scenario 3")
    args = parser.parse_args()

    config = load_benchmark_config()
    if args.scale:
        config["scale_doc_count"] = args.scale
    if args.workers:
        config["concurrency_workers"] = args.workers

    os.environ["NAT_TELEMETRY_ENABLED"] = "false"
    os.environ["H_NAT_REDIS_URL"] = config["redis_url"]

    print("=" * 78)
    print("  h-recall NAT-Wired Performance, Stress & Accuracy Benchmark Suite")
    print("=" * 78)
    print(f"  Redis URL:      {config['redis_url']}")
    print(f"  Scope:          pod={config['pod']} | agent={config['agent']}")
    print(f"  Scale Target:   {config['scale_doc_count']} documents")
    print(f"  Concurrency:    {config['concurrency_workers']} workers")

    # Execute requested scenarios through NAT workflows
    if args.scenario in ("1", "all"):
        await run_scenario_1(config)
    if args.scenario in ("2", "all"):
        await run_scenario_2(config)
    if args.scenario in ("3", "all"):
        await run_scenario_3(config)
    if args.scenario in ("4", "all"):
        await run_scenario_4(config)

    print("\n" + "=" * 78)
    print("  ALL BENCHMARK SCENARIOS COMPLETED SUCCESSFULLY (VIA NAT WORKFLOWS)")
    print("=" * 78)
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nBenchmark interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\n[BENCHMARK FAILED]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
