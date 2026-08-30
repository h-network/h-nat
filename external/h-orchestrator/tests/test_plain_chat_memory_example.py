import ast
from pathlib import Path

import yaml


EXAMPLE = Path(__file__).parents[1] / "examples" / "plain-chat-memory"


def test_plain_chat_workflow_has_no_tools_or_network_ops():
    config = yaml.safe_load((EXAMPLE / "workflow.yaml").read_text(encoding="utf-8"))

    assert set(config) == {"llms", "functions", "workflow"}
    assert config["functions"]["plain_chat"]["_type"] == "chat_completion"
    assert config["workflow"]["_type"] == "h_chat_cycle"
    assert config["workflow"]["dispatcher"] == "plain_chat"
    assert config["workflow"]["hot_keep_count"] >= 20
    assert "tool_names" not in config["workflow"]
    assert "function_groups" not in config


def test_plain_chat_driver_contains_ten_separate_nat_invocations():
    source = (EXAMPLE / "run_demo.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    subprocess_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]

    assert len(subprocess_calls) == 1
    assert "for number, (message, expected) in enumerate(turns" in source
    assert "[{number}/10]" in source
    assert "roles.count(\"user\") != 10" in source
    assert "roles.count(\"assistant\") != 10" in source


def test_plain_chat_identity_matches_driver():
    config = yaml.safe_load((EXAMPLE / "workflow.yaml").read_text(encoding="utf-8"))
    driver = (EXAMPLE / "run_demo.py").read_text(encoding="utf-8")

    assert f'POD = "{config["workflow"]["pod"]}"' in driver
    assert f'AGENT = "{config["workflow"]["agent"]}"' in driver
