import importlib.util
from pathlib import Path

import yaml

EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "hot-memory-recall-tool"
)


def load_yaml(name):
    return yaml.safe_load((EXAMPLE / name).read_text(encoding="utf-8"))


def test_workflow_composes_hot_memory_agent_and_recall_tool():
    config = load_yaml("workflow.yaml")
    assert config["llms"]["chat_llm"] == {
        "_type": "openai",
        "model_name": "${H_NAT_LLM_MODEL}",
        "base_url": "${H_NAT_LLM_BASE_URL}",
        "api_key": "${OPENAI_API_KEY}",
        "temperature": 0.0,
        "request_timeout": 120,
    }
    recall = config["functions"]["recall_search"]
    agent = config["functions"]["recall_agent"]
    workflow = config["workflow"]
    assert recall["_type"] == "h_semantic_search"
    assert agent["_type"] == "tool_calling_agent"
    assert agent["tool_names"] == ["recall_search"]
    assert workflow["_type"] == "h_chat_cycle"
    assert workflow["dispatcher"] == "recall_agent"
    assert (workflow["pod"], workflow["agent"]) == (
        recall["pod"],
        recall["agent"],
    )
    assert 'chat_id "recall-demo-chat"' in agent["system_prompt"]
    assert workflow["chat_id"] == "recall-demo-chat"


def test_maintenance_configs_share_tenant_and_substrate():
    workflow = load_yaml("workflow.yaml")["workflow"]
    sweep = load_yaml("sweep.yaml")["workflow"]
    vectorize = load_yaml("vectorize.yaml")["workflow"]
    assert sweep["_type"] == "h_semantic_sweep"
    assert vectorize["_type"] == "h_semantic_vectorize"
    for config in (sweep, vectorize):
        assert config["redis_url"] == workflow["redis_url"]
        assert config["pod"] == workflow["pod"]
        assert config["agent"] == workflow["agent"]
    assert sweep["migration_threshold_sec"] == 1


def test_driver_detects_only_the_actual_nat_tool_call_trace():
    spec = importlib.util.spec_from_file_location("recall_demo", EXAMPLE / "run_demo.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.TOOL_CALL_PATTERN.search("Calling tools: ['recall_search']")
    assert not module.TOOL_CALL_PATTERN.search("configured tool recall_search")

