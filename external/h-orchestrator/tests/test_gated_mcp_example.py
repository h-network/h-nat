from pathlib import Path

import yaml


EXAMPLE = Path(__file__).parents[1] / "examples" / "gated-junos-mcp" / "workflow.yaml"

READ_ONLY = {
    "gather_device_facts",
    "get_junos_config",
    "get_router_list",
    "junos_config_diff",
}
GATED = {
    "execute_junos_command",
    "execute_junos_pfe_command",
    "execute_junos_command_batch",
    "render_and_apply_j2_template",
    "load_and_commit_config",
}


def test_junos_mcp_raw_group_is_read_only_allowlist():
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    group = config["function_groups"]["junos_mcp"]

    assert group["_type"] == "mcp_client"
    assert group["server"]["transport"] == "streamable-http"
    assert set(group["include"]) == READ_ONLY
    assert not GATED.intersection(group["include"])


def test_each_mutating_member_has_one_gate_wrapper_and_no_raw_agent_reference():
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    functions = config["functions"]
    wrappers = {
        value["mcp_tool_name"]: (name, value)
        for name, value in functions.items()
        if value["_type"] == "h_gated_mcp_tool"
    }

    assert set(wrappers) == GATED
    assert all(value["mcp_group"] == "junos_mcp" for _, value in wrappers.values())
    assert all(value["gate_fn"] == "network_gate" for _, value in wrappers.values())

    agent_tools = set(config["workflow"]["tool_names"])
    assert "junos_mcp" in agent_tools
    assert {name for name, _ in wrappers.values()}.issubset(agent_tools)
    assert not any(f"junos_mcp__{name}" in agent_tools for name in GATED)


def test_mcp_token_is_environment_interpolated():
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    headers = config["function_groups"]["junos_mcp"]["server"]["custom_headers"]
    assert headers["Authorization"] == "Bearer ${JUNOS_MCP_TOKEN}"
