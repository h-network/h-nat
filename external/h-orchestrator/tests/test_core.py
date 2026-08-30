import pytest
from nat.plugins.h_orchestrator.core import build_script, with_context


def test_with_context_uses_blank_line_and_ignores_whitespace_only_context():
    assert with_context("role", "prompt") == "role\n\nprompt"
    assert with_context("  ", "prompt") == "prompt"


def test_build_script_quotes_argument_prompt():
    script = build_script(
        command="agent cli", args=["--flag", "a b"], prompt="$(unsafe)", prompt_via="arg"
    )
    assert script == b"set -e\nexec 'agent cli' --flag 'a b' '$(unsafe)'\n"


def test_build_script_selects_non_colliding_heredoc_delimiter():
    script = build_script(
        command="agent", args=[], prompt="first\n__H_AGENT_PROMPT__\nlast", prompt_via="stdin"
    )
    assert b"<<'__H_AGENT_PROMPT___1'" in script
    assert script.endswith(b"__H_AGENT_PROMPT___1\n")


def test_build_script_validates_environment_name_defensively():
    with pytest.raises(ValueError, match="invalid environment variable"):
        build_script(command="agent", args=[], prompt="x", prompt_via="env:BAD-NAME")


def test_build_script_rejects_unknown_delivery_method():
    with pytest.raises(ValueError, match="unknown prompt_via"):
        build_script(command="agent", args=[], prompt="x", prompt_via="file")

