from pathlib import Path

import yaml


EXAMPLE = Path(__file__).parents[1] / "examples" / "gated-ssh" / "workflow.yaml"


def test_gated_ssh_example_composes_internal_gate():
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    gate = config["functions"]["ssh_gate"]
    workflow = config["workflow"]

    assert gate["_type"] == "h_asimov_gate"
    assert gate["fail_open"] is False
    assert workflow["_type"] == "h_ssh_exec"
    assert workflow["gate_fn"] == "ssh_gate"
    assert workflow["verify_host_key"] is True


def test_gated_ssh_credentials_are_deployment_configuration():
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    workflow = config["workflow"]

    assert workflow["username"] == "${SSH_USERNAME}"
    assert workflow["password"] == "${SSH_PASSWORD}"
    assert "credentials" not in workflow
