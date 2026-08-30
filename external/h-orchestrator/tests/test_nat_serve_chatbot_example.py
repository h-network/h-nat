from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).parents[3]
    / "examples"
    / "h-orchestrator"
    / "nat-serve-chatbot"
    / "workflow.yaml"
)


def load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_chatbot_uses_one_chat_workflow_without_gate_or_router() -> None:
    config = load_workflow()

    assert config["workflow"]["_type"] == "h_chat_cycle"
    assert config["workflow"]["dispatcher"] == "chatbot_agent"
    assert config["functions"]["chatbot_agent"]["_type"] == "tool_calling_agent"
    assert config["functions"]["chatbot_agent"]["tool_names"] == ["recall_search"]
    assert "h_asimov_gate" not in WORKFLOW.read_text(encoding="utf-8")
    assert "router_agent" not in WORKFLOW.read_text(encoding="utf-8")


def test_memory_functions_share_one_tenant_and_chat() -> None:
    config = load_workflow()
    workflow = config["workflow"]

    assert workflow["chat_id"] == "${H_NAT_CHATBOT_CHAT_ID}"
    for name in ("recall_search", "semantic_sweep", "semantic_vectorize"):
        function = config["functions"][name]
        assert function["redis_url"] == workflow["redis_url"]
        assert function["pod"] == workflow["pod"]
        assert function["agent"] == workflow["agent"]


def test_fastapi_exposes_typed_chat_websocket_and_maintenance() -> None:
    front_end = load_workflow()["general"]["front_end"]
    workflow_endpoint = front_end["workflow"]
    endpoints = {
        endpoint["path"]: endpoint["function_name"]
        for endpoint in front_end["endpoints"]
    }

    assert workflow_endpoint["path"] == "/v1/workflow"
    assert workflow_endpoint["websocket_path"] == "/websocket"
    assert "openai_api_path" not in workflow_endpoint
    assert "openai_api_v1_path" not in workflow_endpoint
    assert endpoints == {
        "/maintenance/sweep": "semantic_sweep",
        "/maintenance/vectorize": "semantic_vectorize",
    }
