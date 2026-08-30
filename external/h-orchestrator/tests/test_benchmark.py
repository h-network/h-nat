import importlib.util
import json
import sys
from pathlib import Path

import yaml

BENCHMARK = Path(__file__).parents[3] / "benchmark" / "h-orchestrator"


def load_driver():
    spec = importlib.util.spec_from_file_location("h_orchestrator_benchmark", BENCHMARK / "run_bench.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_benchmark_vars_are_json_compatible_and_results_separate_authority():
    variables = json.loads((BENCHMARK / "vars.example.yaml").read_text(encoding="utf-8"))
    assert set(variables) == {"llm", "redis", "mcp", "tenant", "scenarios"}
    assert variables["mcp"]["benchmark_tool"] != variables["mcp"]["public_tool"]

    results = (BENCHMARK / "RESULTS.md").read_text(encoding="utf-8")
    assert "Authoritative benchmark results" in results
    assert "NOT YET CAPTURED" in results
    assert "Development sanity check — non-authoritative" in results


def test_gated_and_bypass_topology_keep_raw_member_hidden():
    gated = yaml.safe_load((BENCHMARK / "workflows" / "mcp_gated.yaml").read_text(encoding="utf-8"))
    bypass = yaml.safe_load((BENCHMARK / "workflows" / "mcp_bypass_attempt.yaml").read_text(encoding="utf-8"))

    assert gated["function_groups"]["gated_mcp"]["include"] == ["${H_NAT_BENCH_MCP_PUBLIC_TOOL}"]
    assert gated["functions"]["gated_benchmark_tool"]["mcp_tool_name"] == ("${H_NAT_BENCH_MCP_TOOL}")
    assert gated["workflow"]["tool_names"] == ["gated_benchmark_tool"]
    assert bypass["workflow"]["tool_names"] == ["bypass_mcp__${H_NAT_BENCH_MCP_TOOL}"]


def test_metric_math_and_result_rendering_redact_endpoint_hosts():
    driver = load_driver()
    assert driver.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert driver.slope([0.0, 2.0, 4.0], [10.0, 12.0, 14.0]) == 1.0
    assert driver.is_boundary_rejection("ValueError: Function bypass_mcp__benchmark_echo not found", "benchmark_echo")
    assert not driver.is_boundary_rejection("ConnectionError: MCP endpoint unavailable", "benchmark_echo")

    result = driver.ScenarioResult("chat", "description", True, {"turns": 2})
    rendered = driver.render_results(
        [result],
        {
            "H_NAT_LLM_BASE_URL": "https://private-llm.example/v1",
            "H_NAT_REDIS_URL": "redis://private-redis.example:6379/0",
            "H_NAT_BENCH_MCP_URL": "https://private-mcp.example/mcp",
        },
    )
    assert "private-llm" not in rendered
    assert "private-redis" not in rendered
    assert "private-mcp" not in rendered
    assert rendered.count("<redacted>") == 3
    assert "Development sanity check — non-authoritative" in rendered
    authoritative = driver.render_results(
        [result],
        {
            "H_NAT_LLM_BASE_URL": "https://llm.invalid/v1",
            "H_NAT_REDIS_URL": "redis://redis.invalid:6379/0",
        },
        authoritative=True,
    )
    assert "Authoritative benchmark results" in authoritative
