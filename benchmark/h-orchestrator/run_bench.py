#!/usr/bin/env python3
"""Quantitative h-orchestrator chat-cycle and gated-MCP benchmark."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
WORKFLOWS = HERE / "workflows"
ALL_SCENARIOS = ("chat", "mcp", "slow", "malformed", "bypass")


@dataclass
class ScenarioResult:
    name: str
    description: str
    passed: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def latency_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {key: 0.0 for key in ("min", "p50", "p90", "p95", "p99", "max", "mean", "stddev")}
    return {
        "min": round(min(values), 3),
        "p50": round(percentile(values, 50), 3),
        "p90": round(percentile(values, 90), 3),
        "p95": round(percentile(values, 95), 3),
        "p99": round(percentile(values, 99), 3),
        "max": round(max(values), 3),
        "mean": round(statistics.fmean(values), 3),
        "stddev": round(statistics.pstdev(values), 3),
    }


def slope(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)
    denominator = sum((value - x_mean) ** 2 for value in xs)
    if denominator == 0:
        return 0.0
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True)) / denominator


def load_vars(path: Path | None) -> dict[str, Any]:
    selected = path or (HERE / "vars.yaml" if (HERE / "vars.yaml").exists() else HERE / "vars.example.yaml")
    try:
        value = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot load JSON-compatible YAML from {selected}: {error}") from error
    if not isinstance(value, dict):
        raise TypeError(f"{selected} must contain a mapping")
    return value


def nested(config: dict[str, Any], *path: str) -> Any:
    value: Any = config
    for part in path:
        if not isinstance(value, dict) or part not in value:
            raise RuntimeError(f"missing benchmark variable: {'.'.join(path)}")
        value = value[part]
    return value


def choose(config: dict[str, Any], env_name: str, *path: str) -> str:
    value = os.environ.get(env_name) or str(nested(config, *path))
    if value.startswith("<") and value.endswith(">"):
        raise RuntimeError(f"set {env_name} or replace {'.'.join(path)} in vars.yaml")
    return value


def benchmark_env(
    config: dict[str, Any], quick: bool, selected: tuple[str, ...]
) -> tuple[dict[str, str], dict[str, int]]:
    settings = {
        "chat_turns": int(nested(config, "scenarios", "chat_turns")),
        "chat_payload_chars": int(nested(config, "scenarios", "chat_payload_chars")),
        "chat_keep_records": int(nested(config, "scenarios", "chat_keep_records")),
        "mcp_samples": int(nested(config, "scenarios", "mcp_samples")),
        "fault_samples": int(nested(config, "scenarios", "fault_samples")),
        "timeout_seconds": int(nested(config, "scenarios", "timeout_seconds")),
    }
    if quick:
        settings.update(chat_turns=6, chat_payload_chars=32, mcp_samples=2, fault_samples=1)
    env = {
        **os.environ,
        "H_NAT_LLM_MODEL": choose(config, "H_NAT_LLM_MODEL", "llm", "model"),
        "H_NAT_LLM_BASE_URL": choose(config, "H_NAT_LLM_BASE_URL", "llm", "base_url"),
        "OPENAI_API_KEY": choose(config, "OPENAI_API_KEY", "llm", "api_key"),
        "H_NAT_REDIS_URL": choose(config, "H_NAT_REDIS_URL", "redis", "url"),
        "H_NAT_BENCH_POD": str(nested(config, "tenant", "pod")),
        "H_NAT_BENCH_AGENT": str(nested(config, "tenant", "agent")),
        "H_NAT_BENCH_CHAT_KEEP": str(settings["chat_keep_records"]),
        "NAT_TELEMETRY_ENABLED": "false",
    }
    if set(selected) & {"mcp", "slow", "malformed", "bypass"}:
        env.update(
            H_NAT_BENCH_MCP_URL=choose(config, "H_NAT_BENCH_MCP_URL", "mcp", "url"),
            H_NAT_BENCH_MCP_TOKEN=choose(config, "H_NAT_BENCH_MCP_TOKEN", "mcp", "token"),
            H_NAT_BENCH_MCP_TOOL=choose(config, "H_NAT_BENCH_MCP_TOOL", "mcp", "benchmark_tool"),
            H_NAT_BENCH_MCP_PUBLIC_TOOL=choose(config, "H_NAT_BENCH_MCP_PUBLIC_TOOL", "mcp", "public_tool"),
        )
        env["H_NAT_BENCH_ACTIVE_MCP_URL"] = env["H_NAT_BENCH_MCP_URL"]
    return env, settings


async def invoke(manager: Any, prompt: str, to_type: type = str) -> Any:
    async with manager.session() as session, session.run(prompt) as runner:
        return await runner.result(to_type=to_type)


async def chat_scenario(env: dict[str, str], settings: dict[str, int]) -> ScenarioResult:
    import redis.asyncio as aioredis
    from nat.plugins.h_orchestrator.chat_cycle import HChatCycleOutput
    from nat.runtime.loader import load_workflow

    chat_id = f"bench-{uuid.uuid4().hex}"
    index_key = f"{env['H_NAT_BENCH_POD']}:{env['H_NAT_BENCH_AGENT']}:chat-index:{chat_id}"
    wall: list[float] = []
    dispatch: list[float] = []
    overhead: list[float] = []
    priors: list[float] = []
    anomalies: list[str] = []
    client = aioredis.Redis.from_url(env["H_NAT_REDIS_URL"], decode_responses=True)
    started_all = time.perf_counter()
    try:
        async with load_workflow(WORKFLOWS / "chat_cycle.yaml") as manager:
            for number in range(settings["chat_turns"]):
                payload = f"turn-{number:05d}-" + "x" * settings["chat_payload_chars"]
                request = json.dumps({"message": payload, "chat_id": chat_id})
                started = time.perf_counter()
                output = await invoke(manager, request, HChatCycleOutput)
                elapsed = (time.perf_counter() - started) * 1000
                expected_prior = min(2 * number, settings["chat_keep_records"])
                if output.prior_turn_count != expected_prior:
                    anomalies.append(
                        f"turn {number}: prior_turn_count={output.prior_turn_count}, expected={expected_prior}"
                    )
                wall.append(elapsed)
                dispatch.append(float(output.duration_ms))
                overhead.append(max(0.0, elapsed - output.duration_ms))
                priors.append(float(output.prior_turn_count))
        final_records = int(await client.zcard(index_key))
    finally:
        await client.aclose()
    total_seconds = time.perf_counter() - started_all
    expected_final = min(2 * settings["chat_turns"], settings["chat_keep_records"])
    if final_records != expected_final:
        anomalies.append(f"final Redis records={final_records}, expected={expected_final}")
    window = max(1, min(10, len(wall) // 4))
    metrics = {
        "turns": len(wall),
        "payload_chars": settings["chat_payload_chars"],
        "final_records": final_records,
        "turns_per_second": round(len(wall) / total_seconds, 3),
        "wall_ms": latency_summary(wall),
        "dispatcher_ms": latency_summary(dispatch),
        "orchestration_overhead_ms": latency_summary(overhead),
        "early_wall_p95_ms": round(percentile(wall[:window], 95), 3),
        "late_wall_p95_ms": round(percentile(wall[-window:], 95), 3),
        "wall_ms_per_prior_record_slope": round(slope(priors, wall), 6),
    }
    return ScenarioResult(
        "chat",
        "Memory growth and latency across a bounded multi-turn chat",
        not anomalies,
        metrics,
        anomalies,
    )


async def run_mcp_workflow(path: Path, prompt: str, samples: int, timeout: int) -> dict[str, Any]:
    from nat.runtime.loader import load_workflow

    latencies: list[float] = []
    outputs: list[str] = []
    errors: list[str] = []
    started_all = time.perf_counter()
    try:
        async with load_workflow(path) as manager:
            await asyncio.wait_for(invoke(manager, prompt), timeout=timeout)
            for _ in range(samples):
                started = time.perf_counter()
                try:
                    output = await asyncio.wait_for(invoke(manager, prompt), timeout=timeout)
                    outputs.append(str(output))
                except Exception as error:  # noqa: BLE001 - benchmark records arbitrary provider failures
                    errors.append(f"{type(error).__name__}: {error}"[:300])
                latencies.append((time.perf_counter() - started) * 1000)
    except Exception as error:  # noqa: BLE001 - build/discovery failures are benchmark output
        errors.extend([f"build:{type(error).__name__}: {error}"[:300]] * max(1, samples))
    elapsed = time.perf_counter() - started_all
    return {
        "latencies": latencies,
        "outputs": outputs,
        "errors": errors,
        "ops_per_second": round(samples / elapsed, 3) if elapsed else 0.0,
    }


async def mcp_scenario(env: dict[str, str], config: dict[str, Any], settings: dict[str, int]) -> ScenarioResult:
    prompt = str(nested(config, "mcp", "prompt"))
    direct = await run_mcp_workflow(
        WORKFLOWS / "mcp_direct.yaml", prompt, settings["mcp_samples"], settings["timeout_seconds"]
    )
    gated = await run_mcp_workflow(
        WORKFLOWS / "mcp_gated.yaml", prompt, settings["mcp_samples"], settings["timeout_seconds"]
    )
    direct_stats = latency_summary(direct["latencies"])
    gated_stats = latency_summary(gated["latencies"])
    marker = choose(config, "H_NAT_BENCH_MCP_EXPECTED_MARKER", "mcp", "expected_marker")
    anomalies = [f"direct: {error}" for error in direct["errors"]]
    anomalies.extend(f"gated: {error}" for error in gated["errors"])
    direct_missing = sum(marker not in output for output in direct["outputs"])
    gated_missing = sum(marker not in output for output in gated["outputs"])
    if direct_missing:
        anomalies.append(f"direct: {direct_missing} outputs lacked the expected tool marker")
    if gated_missing:
        anomalies.append(f"gated: {gated_missing} outputs lacked the expected tool marker")
    metrics = {
        "samples_each": settings["mcp_samples"],
        "direct_ms": direct_stats,
        "gated_ms": gated_stats,
        "gate_added_p50_ms": round(gated_stats["p50"] - direct_stats["p50"], 3),
        "gate_added_p95_ms": round(gated_stats["p95"] - direct_stats["p95"], 3),
        "direct_ops_per_second": direct["ops_per_second"],
        "gated_ops_per_second": gated["ops_per_second"],
        "direct_error_rate": round(len(direct["errors"]) / settings["mcp_samples"], 4),
        "gated_error_rate": round(len(gated["errors"]) / settings["mcp_samples"], 4),
        "direct_marker_misses": direct_missing,
        "gated_marker_misses": gated_missing,
    }
    return ScenarioResult(
        "mcp",
        "Matched direct-control and h_asimov-gated MCP latency",
        not anomalies,
        metrics,
        anomalies,
    )


def classify_fault(error: str) -> str:
    lowered = error.casefold()
    if "timeout" in lowered:
        return "timeout"
    if "json" in lowered or "parse" in lowered or "protocol" in lowered:
        return "malformed_or_protocol"
    if error.startswith("build:"):
        return "build_or_discovery"
    return "execution_error"


def is_boundary_rejection(error: str, tool_name: str) -> bool:
    lowered = error.casefold()
    resolution_markers = ("not found", "unknown function", "not registered", "cannot find")
    return tool_name.casefold() in lowered and any(marker in lowered for marker in resolution_markers)


async def fault_scenario(name: str, endpoint: str, config: dict[str, Any], settings: dict[str, int]) -> ScenarioResult:
    previous = os.environ.get("H_NAT_BENCH_ACTIVE_MCP_URL")
    os.environ["H_NAT_BENCH_ACTIVE_MCP_URL"] = endpoint
    try:
        data = await run_mcp_workflow(
            WORKFLOWS / "mcp_gated.yaml",
            str(nested(config, "mcp", "prompt")),
            settings["fault_samples"],
            settings["timeout_seconds"],
        )
    finally:
        if previous is None:
            os.environ.pop("H_NAT_BENCH_ACTIVE_MCP_URL", None)
        else:
            os.environ["H_NAT_BENCH_ACTIVE_MCP_URL"] = previous
    categories: dict[str, int] = {}
    for error in data["errors"]:
        category = classify_fault(error)
        categories[category] = categories.get(category, 0) + 1
    unexpected = len(data["outputs"])
    anomalies = [f"{unexpected} calls unexpectedly succeeded"] if unexpected else []
    metrics = {
        "samples": settings["fault_samples"],
        "latency_ms": latency_summary(data["latencies"]),
        "failure_categories": categories,
        "failure_rate": round(len(data["errors"]) / settings["fault_samples"], 4),
        "output_excerpts": [value[:160] for value in data["outputs"][:3]],
        "error_excerpts": data["errors"][:3],
    }
    return ScenarioResult(name, f"Behavior against the {name} MCP endpoint", not anomalies, metrics, anomalies)


async def bypass_scenario(config: dict[str, Any]) -> ScenarioResult:
    from nat.runtime.loader import load_workflow

    started = time.perf_counter()
    error_text = ""
    try:
        async with load_workflow(WORKFLOWS / "mcp_bypass_attempt.yaml"):
            pass
    except Exception as error:  # noqa: BLE001 - any successful build is the security failure
        error_text = f"{type(error).__name__}: {error}"[:500]
    elapsed = (time.perf_counter() - started) * 1000
    tool_name = choose(config, "H_NAT_BENCH_MCP_TOOL", "mcp", "benchmark_tool")
    passed = is_boundary_rejection(error_text, tool_name)
    if passed:
        anomalies: list[str] = []
    elif error_text:
        anomalies = ["workflow failed for an unrelated reason; hidden-tool rejection was not proven"]
    else:
        anomalies = ["hidden raw MCP member unexpectedly resolved"]
    return ScenarioResult(
        "bypass",
        "Hidden raw MCP member must not resolve as an agent tool",
        passed,
        {
            "build_failed": bool(error_text),
            "boundary_rejection_proven": passed,
            "rejection_latency_ms": round(elapsed, 3),
            "error_excerpt": error_text,
        },
        anomalies,
    )


def validate_configs(env: dict[str, str], selected: tuple[str, ...]) -> None:
    names: set[str] = set()
    if "chat" in selected:
        names.add("chat_cycle.yaml")
    if set(selected) & {"mcp", "slow", "malformed"}:
        names.add("mcp_gated.yaml")
    if "mcp" in selected:
        names.add("mcp_direct.yaml")
    if "bypass" in selected:
        names.add("mcp_bypass_attempt.yaml")
    for name in sorted(names):
        completed = subprocess.run(
            ["nat", "validate", "--config_file", str(WORKFLOWS / name)],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(f"nat validate failed for {name}: {completed.stdout[-800:]}")


def redacted_endpoint(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme or 'unknown'}://<redacted>"


def render_results(results: list[ScenarioResult], env: dict[str, str], *, authoritative: bool = False) -> str:
    authority_heading = (
        "Authoritative benchmark results" if authoritative else "Development sanity check — non-authoritative"
    )
    lines = [
        "# h-orchestrator benchmark results",
        "",
        f"## {authority_heading}",
        "",
        (
            "These measurements are authoritative benchmark results from a production-like test environment."
            if authoritative
            else "HARNESS-ONLY: these measurements are not authoritative benchmark proof."
        ),
        "",
        f"- Captured (UTC): {datetime.now(UTC).isoformat()}",
        f"- Platform: {platform.platform()}",
        f"- Python: {platform.python_version()}",
        f"- LLM endpoint: {redacted_endpoint(env['H_NAT_LLM_BASE_URL'])}",
        f"- Redis endpoint: {redacted_endpoint(env['H_NAT_REDIS_URL'])}",
        (
            f"- MCP endpoint: {redacted_endpoint(env['H_NAT_BENCH_MCP_URL'])}"
            if "H_NAT_BENCH_MCP_URL" in env
            else "- MCP endpoint: not configured for selected scenarios"
        ),
        "",
        "## Executive summary",
        "",
        "| Scenario | Status | Primary metric | Key finding |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        primary = next(iter(result.metrics.items()), ("metrics", "none"))
        finding = result.anomalies[0] if result.anomalies else "No invariant violation observed"
        lines.append(
            f"| {result.name} | {'PASS' if result.passed else 'FAIL'} | {primary[0]}={primary[1]} | {finding} |"
        )
    for result in results:
        lines.extend(
            [
                "",
                f"## {result.name}",
                "",
                result.description,
                "",
                "```json",
                json.dumps(result.metrics, indent=2, sort_keys=True),
                "```",
                "",
                "Anomalies: " + ("; ".join(result.anomalies) if result.anomalies else "none"),
            ]
        )
    lines.extend(
        [
            "",
            "## Analysis and operational recommendations",
            "",
            (
                "Benchmark operator: record the first scale/concurrency point where latency, throughput, "
                "or correctness becomes unacceptable, and document the deployment-specific tuning here."
            ),
            "",
        ]
    )
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    config = load_vars(args.vars)
    selected = ALL_SCENARIOS if "all" in args.scenarios else tuple(args.scenarios)
    env, settings = benchmark_env(config, args.quick, selected)
    os.environ.update(env)
    validate_configs(env, selected)
    results: list[ScenarioResult] = []
    if "chat" in selected:
        results.append(await chat_scenario(env, settings))
    if "mcp" in selected:
        env["H_NAT_BENCH_ACTIVE_MCP_URL"] = env["H_NAT_BENCH_MCP_URL"]
        os.environ["H_NAT_BENCH_ACTIVE_MCP_URL"] = env["H_NAT_BENCH_MCP_URL"]
        results.append(await mcp_scenario(env, config, settings))
    if "slow" in selected:
        endpoint = choose(config, "H_NAT_BENCH_MCP_SLOW_URL", "mcp", "slow_url")
        results.append(await fault_scenario("slow", endpoint, config, settings))
    if "malformed" in selected:
        endpoint = choose(config, "H_NAT_BENCH_MCP_MALFORMED_URL", "mcp", "malformed_url")
        results.append(await fault_scenario("malformed", endpoint, config, settings))
    if "bypass" in selected:
        results.append(await bypass_scenario(config))

    payload = {
        "captured_utc": datetime.now(UTC).isoformat(),
        "results": [asdict(result) for result in results],
    }
    output_results = args.output_results or (
        HERE / "RESULTS.md" if args.authoritative else HERE.parents[1] / ".artifacts" / "h-orchestrator-dev-results.md"
    )
    args.raw_json.parent.mkdir(parents=True, exist_ok=True)
    output_results.parent.mkdir(parents=True, exist_ok=True)
    args.raw_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_results.write_text(render_results(results, env, authoritative=args.authoritative), encoding="utf-8")
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        for result in results:
            print(f"{result.name:10} {'PASS' if result.passed else 'FAIL'} {json.dumps(result.metrics)}")
        print(f"wrote {output_results} and {args.raw_json}")
    return 0 if all(result.passed for result in results) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vars", type=Path, help="JSON-compatible vars YAML; defaults to vars.yaml")
    parser.add_argument("--scenarios", nargs="+", choices=("all", *ALL_SCENARIOS), default=["all"])
    parser.add_argument("--quick", action="store_true", help="run a small harness smoke")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results to stdout")
    parser.add_argument("--output-results", type=Path)
    parser.add_argument(
        "--authoritative",
        action="store_true",
        help="label output as authoritative benchmark evidence; defaults output to RESULTS.md",
    )
    parser.add_argument(
        "--raw-json", type=Path, default=HERE.parents[1] / ".artifacts" / "h-orchestrator-benchmark.json"
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main_async(parse_args())))
    except Exception as error:
        print(f"benchmark setup failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
