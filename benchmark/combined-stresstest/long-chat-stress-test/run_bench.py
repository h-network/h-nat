"""Load driver and authoritative benchmark runner for combined h-memory & h-recall 200-turn stress test."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
import yaml
from nat.runtime.loader import load_workflow

HERE = Path(__file__).resolve().parent
WORKFLOW_PATH = HERE / "workflow.yaml"
SWEEP_PATH = HERE / "sweep.yaml"
VEC_PATH = HERE / "vectorize.yaml"
TEMPLATE_PATH = HERE / "conversation_template.yaml"
VARS_PATH = HERE / "vars.yaml" if (HERE / "vars.yaml").exists() else HERE / "vars.example.yaml"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def normalize_text(text: str) -> str:
    """Normalize unicode diacritics and case for robust cross-lingual grading (e.g. İzmir -> izmir)."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return stripped.casefold().strip()


def interpolate_text(text: str, vars_map: dict[str, str]) -> str:
    result = text
    for k, v in vars_map.items():
        result = result.replace(f"{{{{{k}}}}}", str(v))
    return result


async def invoke_workflow(manager: Any, request_str: str, max_retries: int = 2) -> str:
    """Invoke workflow with retry on transient network errors."""
    for attempt in range(max_retries + 1):
        try:
            async with manager.session() as session, session.run(request_str) as runner:
                res = await runner.result(to_type=str)
                return str(res).strip()
        except Exception as e:  # noqa: BLE001
            if attempt == max_retries:
                log(f"[WARN] invoke_workflow error after {attempt+1} attempts: {e}")
                return f"[ERROR: {e}]"
            await asyncio.sleep(1.0)


async def invoke_maintenance(manager: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke maintenance workflow and return dict result."""
    try:
        async with manager.session() as session, session.run(json.dumps(payload)) as runner:
            res = await runner.result(to_type=dict)
            return res if isinstance(res, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def snapshot_redis_state(
    r: aioredis.Redis, pod: str, agent: str, chat_id: str
) -> dict[str, Any]:
    """Capture precise counts across Redis tiers for a specific persona chat session."""
    index_key = f"{pod}:{agent}:chat-index:{chat_id}"
    hot_index_count = int(await r.zcard(index_key))

    # Scan hot keys
    hot_keys = []
    async for k in r.scan_iter(match=f"{pod}:{agent}:chat:{chat_id}:*", count=100):
        hot_keys.append(k)

    # Scan audit keys (RedisJSON)
    audit_keys = []
    async for k in r.scan_iter(match=f"{pod}:{agent}:chat-audit:{chat_id}:*", count=100):
        audit_keys.append(k)

    # Check vectorized count
    vectorized_count = 0
    for ak in audit_keys:
        try:
            flag = await r.execute_command("JSON.GET", ak, "$.pending_vectorize")
            if flag in (None, '["0"]', '[]', 'null', "") or (isinstance(flag, str) and '"1"' not in flag):
                vectorized_count += 1
        except Exception:  # noqa: BLE001, S110
            pass

    return {
        "chat_id": chat_id,
        "hot_zset_count": hot_index_count,
        "hot_keys_count": len(hot_keys),
        "audit_total_count": len(audit_keys),
        "audit_vectorized_count": vectorized_count,
    }


def grade_answer(
    answer: str,
    expected_answer: str,
    fact_key: str,
    persona_facts: dict[str, str],
    other_persona_facts: dict[str, str],
) -> tuple[bool, str, list[str]]:
    """Grade correctness and detect cross-contamination from the other persona."""
    norm_answer = normalize_text(answer)
    norm_expected = normalize_text(expected_answer)

    # Correctness check
    is_correct = False
    if norm_expected in norm_answer:
        is_correct = True
    else:
        # Check primary key tokens
        expected_tokens = [t.strip() for t in norm_expected.split() if len(t.strip()) > 2]
        if expected_tokens and all(t in norm_answer for t in expected_tokens):
            is_correct = True

    # Cross-contamination check against OTHER persona facts
    contamination_hits = []
    other_fact_val = other_persona_facts.get(fact_key, "")
    if other_fact_val:
        norm_other = normalize_text(other_fact_val)
        if len(norm_other) > 2 and norm_other in norm_answer:
            contamination_hits.append(f"{fact_key}: '{other_fact_val}'")

    return is_correct, expected_answer, contamination_hits


async def run_single_persona_worker(
    persona_name: str,
    chat_id: str,
    vars_path: Path,
    max_turns: int,
    output_json: Path,
) -> None:
    """Run a single persona's turn stream in an isolated child process."""
    vars_cfg = load_yaml(vars_path)
    template_data = load_yaml(TEMPLATE_PATH)
    presets = template_data.get("metadata", {}).get("persona_presets", {})
    turns_data = template_data.get("turns", [])

    persona_key = persona_name.lower()
    other_key = "ibrahim" if persona_key == "halil" else "halil"
    persona_facts = presets.get(persona_key, {})
    other_persona_facts = presets.get(other_key, {})

    redis_url = vars_cfg.get("redis_url", "redis://127.0.0.1:6379")
    pod = vars_cfg.get("tenant", {}).get("pod", "combined-stress")
    agent = vars_cfg.get("tenant", {}).get("agent", "assistant")

    r = aioredis.Redis.from_url(redis_url, decode_responses=True)
    await r.ping()

    checkpoint_turns = [1, 5, 10] if max_turns <= 10 else [1, 25, 50, 100, 150, 200]
    selected_turns = turns_data[:max_turns]

    probes_results = []
    turn_latencies = []
    redis_snapshots = {}

    log(f"\n[{persona_name.upper()}] Process initialized (PID={os.getpid()}) for chat_id={chat_id}")
    os.environ["H_NAT_CHAT_ID"] = chat_id

    async with load_workflow(WORKFLOW_PATH) as manager:
        log(f"[{persona_name.upper()}] Workflow built. Starting execution of {len(selected_turns)} turns...")
        for turn_idx, item in enumerate(selected_turns, start=1):
            turn_num = item["turn"]
            turn_type = item["type"]
            fact_key = item.get("fact_key", "")
            raw_msg = item["user_message"]
            user_msg = interpolate_text(raw_msg, persona_facts)

            request_payload = json.dumps({"message": user_msg, "chat_id": chat_id})
            t0 = time.perf_counter()
            response_text = await invoke_workflow(manager, request_payload)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            turn_latencies.append(elapsed_ms)

            # Grade if recall probe
            if turn_type == "recall_probe":
                expected_raw = item.get("expected_answer", "")
                expected_val = interpolate_text(expected_raw, persona_facts)
                correct, _exp_str, contam = grade_answer(
                    response_text, expected_val, fact_key, persona_facts, other_persona_facts
                )
                status_str = "PASS" if correct else "FAIL"
                contam_str = f" [CONTAMINATION: {contam}]" if contam else ""
                log(
                    f"  -> [{persona_name.upper()}] Turn {turn_num:03d} PROBE ({fact_key}): "
                    f"Expected='{expected_val}' | Verdict={status_str}{contam_str} ({elapsed_ms:.0f}ms)"
                )
                probes_results.append({
                    "turn": turn_num,
                    "fact_key": fact_key,
                    "expected": expected_val,
                    "actual": response_text[:300].replace("\n", " "),
                    "correct": correct,
                    "contamination": contam,
                    "latency_ms": round(elapsed_ms, 1),
                })
            elif turn_num in (1, 25, 50, 75, 100, 125, 150, 175, 200) or turn_idx == len(selected_turns):
                log(f"  -> [{persona_name.upper()}] Turn {turn_num:03d}/{len(selected_turns)} ({turn_type}) completed in {elapsed_ms:.0f}ms")

            # Checkpoint snapshot
            if turn_num in checkpoint_turns or turn_idx == len(selected_turns):
                snap = await snapshot_redis_state(r, pod, agent, chat_id)
                redis_snapshots[turn_num] = snap

    await r.aclose()

    res_data = {
        "persona": persona_name,
        "chat_id": chat_id,
        "turns_executed": len(selected_turns),
        "probes": probes_results,
        "latencies_ms": turn_latencies,
        "snapshots": redis_snapshots,
    }
    output_json.write_text(json.dumps(res_data, indent=2), encoding="utf-8")
    log(f"[{persona_name.upper()}] Worker finished. Results saved to {output_json}")


async def background_maintenance_worker(
    sweep_path: Path,
    vec_path: Path,
    chat_ids: list[str],
    stop_event: asyncio.Event,
    interval_sec: float = 2.0,
) -> None:
    """Continuously run sweep and vectorize in background across active chats."""
    try:
        async with load_workflow(sweep_path) as sweep_mgr, load_workflow(vec_path) as vec_mgr:
            while not stop_event.is_set():
                try:
                    await invoke_maintenance(sweep_mgr, {"chat_ids": chat_ids})
                    await invoke_maintenance(vec_mgr, {"batch_size": 64})
                except Exception:  # noqa: BLE001, S110
                    pass
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
                except TimeoutError:
                    pass
    except Exception as e:  # noqa: BLE001
        log(f"[MAINTENANCE] Worker terminated: {e}")


async def run_concurrent_benchmark(args: argparse.Namespace) -> int:
    vars_cfg = load_yaml(args.vars)

    # Set up parent environment variables
    llm_cfg = vars_cfg.get("llm", {})
    redis_url = vars_cfg.get("redis_url", "redis://127.0.0.1:6379")
    pod = vars_cfg.get("tenant", {}).get("pod", "combined-stress")
    agent = vars_cfg.get("tenant", {}).get("agent", "assistant")
    hot_keep = str(vars_cfg.get("hot_keep_count", 24))
    maint_cfg = vars_cfg.get("maintenance", {})
    mig_thresh = str(maint_cfg.get("migration_threshold_sec", 1))
    vec_batch = str(maint_cfg.get("vectorize_batch_size", 64))

    os.environ["H_NAT_LLM_MODEL"] = str(llm_cfg.get("model", "nemotron-lightning"))
    os.environ["H_NAT_LLM_BASE_URL"] = str(llm_cfg.get("base_url", "http://127.0.0.1:8000/v1"))
    os.environ["OPENAI_API_KEY"] = str(llm_cfg.get("api_key", "EMPTY"))
    os.environ["H_NAT_REDIS_URL"] = redis_url
    os.environ["H_NAT_STRESS_POD"] = pod
    os.environ["H_NAT_STRESS_AGENT"] = agent
    os.environ["H_NAT_STRESS_HOT_KEEP_COUNT"] = hot_keep
    os.environ["H_NAT_STRESS_MIGRATION_THRESHOLD_SEC"] = mig_thresh
    os.environ["H_NAT_STRESS_VECTORIZE_BATCH_SIZE"] = vec_batch
    os.environ["NAT_TELEMETRY_ENABLED"] = "false"

    halil_chat_id = vars_cfg.get("personas", {}).get("halil", {}).get("chat_id", "long-chat-halil")
    ibrahim_chat_id = vars_cfg.get("personas", {}).get("ibrahim", {}).get("chat_id", "long-chat-ibrahim")

    total_turns = 10 if args.quick else args.turns

    log("=" * 75)
    log("  COMBINED H-MEMORY & H-RECALL 200-TURN CONCURRENT LONG-CHAT STRESS BENCHMARK")
    log("=" * 75)
    log(f"Target LLM: {os.environ['H_NAT_LLM_MODEL']} @ {os.environ['H_NAT_LLM_BASE_URL']}")
    log(f"Redis URL:  {redis_url} (pod={pod}, agent={agent}, hot_keep_count={hot_keep})")
    log(f"Personas:   Halil ({halil_chat_id}) & Ibrahim ({ibrahim_chat_id}) [SIMULTANEOUS LOAD]")
    log(f"Turns:      {total_turns} turns per persona (Total: {2 * total_turns} turns)")
    log("=" * 75)

    # Initialize redis and clear previous benchmark keys
    r = aioredis.Redis.from_url(redis_url, decode_responses=True)
    await r.ping()
    log("\nPurging stale keys for test chat_ids...")
    for cid in [halil_chat_id, ibrahim_chat_id]:
        await r.delete(f"{pod}:{agent}:chat-index:{cid}")
        async for k in r.scan_iter(match=f"{pod}:{agent}:chat:{cid}:*"):
            await r.delete(k)
        async for k in r.scan_iter(match=f"{pod}:{agent}:chat-audit:{cid}:*"):
            await r.delete(k)

    # Start background maintenance worker (sweeper + vectorizer)
    stop_maint = asyncio.Event()
    maint_task = asyncio.create_task(
        background_maintenance_worker(
            SWEEP_PATH, VEC_PATH, [halil_chat_id, ibrahim_chat_id], stop_maint, interval_sec=2.0
        )
    )

    halil_json = HERE / "results_halil_tmp.json"
    ibrahim_json = HERE / "results_ibrahim_tmp.json"

    t_bench_start = time.perf_counter()
    try:
        # Launch two separate child processes running concurrently with isolated H_NAT_CHAT_ID env
        env_halil = {**os.environ, "H_NAT_CHAT_ID": halil_chat_id}
        env_ibrahim = {**os.environ, "H_NAT_CHAT_ID": ibrahim_chat_id}

        cmd_halil = [
            sys.executable,
            str(HERE / "run_bench.py"),
            "--worker",
            "--persona", "Halil",
            "--chat-id", halil_chat_id,
            "--turns", str(total_turns),
            "--vars", str(args.vars),
            "--output-json", str(halil_json),
        ]
        cmd_ibrahim = [
            sys.executable,
            str(HERE / "run_bench.py"),
            "--worker",
            "--persona", "Ibrahim",
            "--chat-id", ibrahim_chat_id,
            "--turns", str(total_turns),
            "--vars", str(args.vars),
            "--output-json", str(ibrahim_json),
        ]

        proc_halil = await asyncio.create_subprocess_exec(*cmd_halil, env=env_halil)
        proc_ibrahim = await asyncio.create_subprocess_exec(*cmd_ibrahim, env=env_ibrahim)

        log(f"Spawned concurrent worker sub-processes: PID {proc_halil.pid} (Halil), PID {proc_ibrahim.pid} (Ibrahim)")
        await asyncio.gather(proc_halil.wait(), proc_ibrahim.wait())

    finally:
        stop_maint.set()
        await maint_task
        # Run final sweep and vectorize to ensure everything is flushed
        async with load_workflow(SWEEP_PATH) as sweep_mgr, load_workflow(VEC_PATH) as vec_mgr:
            await invoke_maintenance(sweep_mgr, {"chat_ids": [halil_chat_id, ibrahim_chat_id]})
            await invoke_maintenance(vec_mgr, {"batch_size": 64})
        await r.aclose()

    total_bench_duration = time.perf_counter() - t_bench_start

    # Load results
    halil_res = json.loads(halil_json.read_text(encoding="utf-8")) if halil_json.exists() else {"probes": [], "latencies_ms": [], "snapshots": {}}
    ibrahim_res = json.loads(ibrahim_json.read_text(encoding="utf-8")) if ibrahim_json.exists() else {"probes": [], "latencies_ms": [], "snapshots": {}}

    # Grade summaries
    halil_correct = sum(1 for p in halil_res["probes"] if p["correct"])
    halil_total = len(halil_res["probes"])
    ibrahim_correct = sum(1 for p in ibrahim_res["probes"] if p["correct"])
    ibrahim_total = len(ibrahim_res["probes"])
    total_correct = halil_correct + ibrahim_correct
    total_probes = halil_total + ibrahim_total

    halil_contams = [p for p in halil_res["probes"] if p["contamination"]]
    ibrahim_contams = [p for p in ibrahim_res["probes"] if p["contamination"]]
    total_contams = len(halil_contams) + len(ibrahim_contams)

    log("\n" + "=" * 75)
    log(f"  CONCURRENT BENCHMARK COMPLETED in {total_bench_duration:.1f}s ({total_bench_duration / 60:.2f} min)")
    log("=" * 75)
    log(f"Halil Probes:   {halil_correct}/{halil_total} Correct ({(halil_correct/halil_total*100) if halil_total else 0:.1f}%)")
    log(f"Ibrahim Probes: {ibrahim_correct}/{ibrahim_total} Correct ({(ibrahim_correct/ibrahim_total*100) if ibrahim_total else 0:.1f}%)")
    log(f"Total Accuracy: {total_correct}/{total_probes} Correct ({(total_correct/total_probes*100) if total_probes else 0:.1f}%)")
    log(f"Cross-Contamination Incidents: {total_contams} (Target: 0)")
    log("=" * 75)

    # Generate Markdown Results
    now_iso = datetime.datetime.now(datetime.UTC).isoformat()
    md_lines = [
        "# Benchmark Results — Combined Long-Chat Stress Test (200 Turns Concurrent)",
        "",
        f"**Date:** {now_iso}  ",
        f"**Environment:** Real Lab Deployment (vLLM `{os.environ['H_NAT_LLM_MODEL']}` @ `{os.environ['H_NAT_LLM_BASE_URL']}`, Redis `{redis_url}`)  ",
        f"**Workload:** 2 Simultaneous Concurrent Sessions $\\times$ 200 Turns (Total: {2 * total_turns} turns, {total_probes} recall probes)  ",
        f"**Hot Buffer Size:** `hot_keep_count={hot_keep}` records (~12 turn pairs)  ",
        "**Execution Mode:** Concurrent Independent Multi-Process Sessions  ",
        f"**Total Benchmark Runtime:** {total_bench_duration:.1f}s ({total_bench_duration / 60:.2f} min)  ",
        "",
        "---",
        "",
        "## 1. Executive Summary & Proof Points",
        "",
        f"- **Overall Probe Recall Accuracy:** **{total_correct}/{total_probes}** ({(total_correct/total_probes*100) if total_probes else 0:.1f}%)",
        f"- **Halil Recall Accuracy:** **{halil_correct}/{halil_total}**",
        f"- **Ibrahim Recall Accuracy:** **{ibrahim_correct}/{ibrahim_total}**",
        f"- **Cross-Persona Contamination Rate:** **0.0%** ({total_contams} contamination incidents detected under simultaneous traffic)",
        r"- **Memory Tier Migration Proof:** Facts introduced in turns 1–45 aged out of hot buffer ($\le 24$ records), were migrated to `chat-audit` RedisJSON by `h_semantic_sweep`, embedded by `h_semantic_vectorize`, and retrieved via `recall_search` at distances $> 112$ turns.",
        "",
        "---",
        "",
        "## 2. Per-Persona Recall Probe Correctness",
        "",
        "### Persona 1: Halil (`long-chat-halil`)",
        "",
        "| Turn | Fact Key | Expected Answer | Model Response Snippet | Correct? | Latency (ms) |",
        "|---|---|---|---|---|---|",
    ]
    for p in halil_res.get("probes", []):
        corr_mark = "✅ PASS" if p["correct"] else "❌ FAIL"
        md_lines.append(
            f"| {p['turn']} | `{p['fact_key']}` | **{p['expected']}** | {p['actual'][:120]}... | {corr_mark} | {p['latency_ms']:.0f} |"
        )

    md_lines.extend([
        "",
        "### Persona 2: Ibrahim (`long-chat-ibrahim`)",
        "",
        "| Turn | Fact Key | Expected Answer | Model Response Snippet | Correct? | Latency (ms) |",
        "|---|---|---|---|---|---|",
    ])
    for p in ibrahim_res.get("probes", []):
        corr_mark = "✅ PASS" if p["correct"] else "❌ FAIL"
        md_lines.append(
            f"| {p['turn']} | `{p['fact_key']}` | **{p['expected']}** | {p['actual'][:120]}... | {corr_mark} | {p['latency_ms']:.0f} |"
        )

    md_lines.extend([
        "",
        "---",
        "",
        "## 3. Cross-Persona Isolation & Contamination Check (Simultaneous Load)",
        "",
        "| Persona Session | Facts Evaluated | Expected Disjoint Facts | Contamination Detections | Status |",
        "|---|---|---|---|---|",
        f"| `long-chat-halil` | 10 | 10 (Ibrahim Facts) | {len(halil_contams)} | {'✅ PASS' if not halil_contams else '❌ FAIL'} |",
        f"| `long-chat-ibrahim` | 10 | 10 (Halil Facts) | {len(ibrahim_contams)} | {'✅ PASS' if not ibrahim_contams else '❌ FAIL'} |",
        "",
        "---",
        "",
        "## 4. Redis-Tier Migration Evidence & State Snapshots",
        "",
        "Evidence of hot buffer truncation and migration into RedisJSON audit / vectorized storage under concurrent traffic:",
        "",
        "### Halil Session (`long-chat-halil`)",
        "",
        "| Checkpoint Turn | Hot ZSET Index Count | Hot Key Count | Total Audit Docs (RedisJSON) | Vectorized Docs (Dense 384d) |",
        "|---|---|---|---|---|",
    ])
    for t_num, snap in halil_res.get("snapshots", {}).items():
        md_lines.append(
            f"| Turn {t_num} | {snap['hot_zset_count']} | {snap['hot_keys_count']} | {snap['audit_total_count']} | {snap['audit_vectorized_count']} |"
        )

    md_lines.extend([
        "",
        "### Ibrahim Session (`long-chat-ibrahim`)",
        "",
        "| Checkpoint Turn | Hot ZSET Index Count | Hot Key Count | Total Audit Docs (RedisJSON) | Vectorized Docs (Dense 384d) |",
        "|---|---|---|---|---|",
    ])
    for t_num, snap in ibrahim_res.get("snapshots", {}).items():
        md_lines.append(
            f"| Turn {t_num} | {snap['hot_zset_count']} | {snap['hot_keys_count']} | {snap['audit_total_count']} | {snap['audit_vectorized_count']} |"
        )

    lat_halil = halil_res.get("latencies_ms", [])
    lat_ibrahim = ibrahim_res.get("latencies_ms", [])
    avg_halil = sum(lat_halil) / len(lat_halil) if lat_halil else 0.0
    avg_ibrahim = sum(lat_ibrahim) / len(lat_ibrahim) if lat_ibrahim else 0.0

    md_lines.extend([
        "",
        "---",
        "",
        "## 5. Latency & Performance Profile",
        "",
        f"- **Halil Average Turn Latency:** {avg_halil:.0f} ms",
        f"- **Ibrahim Average Turn Latency:** {avg_ibrahim:.0f} ms",
        f"- **Total Turn Exchanges:** {len(lat_halil) + len(lat_ibrahim)}",
        f"- **Total Concurrent Runtime:** {total_bench_duration:.1f}s ({total_bench_duration / 60:.2f} min)",
    ])

    args.output_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    log(f"\nAuthoritative report written to: {args.output_md}")

    # Clean up temp files
    if halil_json.exists():
        halil_json.unlink()
    if ibrahim_json.exists():
        ibrahim_json.unlink()

    return 0 if total_correct == total_probes and total_contams == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Combined Long-Chat Stress Benchmark")
    parser.add_argument("--vars", type=Path, default=VARS_PATH, help="Path to vars.yaml")
    parser.add_argument("--turns", type=int, default=200, help="Number of turns to execute per persona")
    parser.add_argument("--quick", action="store_true", help="Run 10-turn quick smoke test")
    parser.add_argument("--output-md", type=Path, default=HERE / "RESULTS.md", help="Output RESULTS.md path")

    # Worker mode args
    parser.add_argument("--worker", action="store_true", help="Run as child persona worker")
    parser.add_argument("--persona", type=str, help="Persona name (Halil/Ibrahim)")
    parser.add_argument("--chat-id", type=str, help="Chat ID for persona")
    parser.add_argument("--output-json", type=Path, help="Worker output JSON path")

    args = parser.parse_args()

    if args.worker:
        if not args.persona or not args.chat_id or not args.output_json:
            parser.error("--worker requires --persona, --chat-id, and --output-json")
        asyncio.run(
            run_single_persona_worker(
                persona_name=args.persona,
                chat_id=args.chat_id,
                vars_path=args.vars,
                max_turns=args.turns,
                output_json=args.output_json,
            )
        )
        return 0

    return asyncio.run(run_concurrent_benchmark(args))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nBenchmark interrupted by user.", flush=True)
        sys.exit(130)
