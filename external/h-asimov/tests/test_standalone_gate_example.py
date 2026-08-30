"""Structural checks for the standalone-gate example's YAML configs.

Cheap, no LLM required — complements (doesn't replace) `run_demo.py`,
which actually verifies these configs against a real LLM endpoint (see
examples/h-asimov/standalone-gate/README.md).
"""
from __future__ import annotations

from pathlib import Path

import yaml

EXAMPLE_DIR = (
    Path(__file__).parents[3]
    / "examples"
    / "h-asimov"
    / "standalone-gate"
)


def test_workflow_yaml_is_a_bare_asimov_gate() -> None:
    config = yaml.safe_load((EXAMPLE_DIR / "workflow.yaml").read_text(encoding="utf-8"))
    workflow = config["workflow"]

    assert workflow["_type"] == "h_asimov_gate"
    assert workflow["fail_open"] is False
    assert workflow["llm_name"] == "judge_llm"
    assert "ground_rules_inline" in workflow
    assert "functions" not in config  # standalone: the gate itself is the entry point


def test_workflow_yaml_reads_endpoint_from_env_not_hardcoded() -> None:
    config = yaml.safe_load((EXAMPLE_DIR / "workflow.yaml").read_text(encoding="utf-8"))
    llm = config["llms"]["judge_llm"]

    assert llm["model_name"] == "${H_NAT_LLM_MODEL}"
    assert llm["base_url"] == "${H_NAT_LLM_BASE_URL}"
    assert llm["api_key"] == "${OPENAI_API_KEY}"


def test_noop_yaml_requires_no_llm() -> None:
    config = yaml.safe_load((EXAMPLE_DIR / "noop.yaml").read_text(encoding="utf-8"))
    workflow = config["workflow"]

    assert workflow["_type"] == "h_asimov_gate"
    assert workflow["mode"] == "noop"
    assert "llms" not in config
    assert "llm_name" not in workflow
