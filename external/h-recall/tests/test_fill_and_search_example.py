"""Unit tests verifying fill-and-search example structure and YAML configs."""

from pathlib import Path
import pytest
from nat.builder.workflow_builder import WorkflowBuilder
from nat.runtime.loader import load_config

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "fill-and-search"


def test_example_files_exist():
    assert (EXAMPLE_DIR / "README.md").is_file()
    assert (EXAMPLE_DIR / "run_demo.py").is_file()
    assert (EXAMPLE_DIR / "workflow.yaml").is_file()
    assert (EXAMPLE_DIR / "sweep.yaml").is_file()
    assert (EXAMPLE_DIR / "vectorize.yaml").is_file()
    assert (EXAMPLE_DIR / "search.yaml").is_file()


@pytest.mark.asyncio
async def test_example_yamls_buildable(monkeypatch):
    monkeypatch.setenv("H_NAT_REDIS_URL", "redis://127.0.0.1:1")
    # Verify each YAML can be loaded and built by WorkflowBuilder without eager I/O
    yaml_files = [
        EXAMPLE_DIR / "workflow.yaml",
        EXAMPLE_DIR / "sweep.yaml",
        EXAMPLE_DIR / "vectorize.yaml",
        EXAMPLE_DIR / "search.yaml",
    ]
    for yf in yaml_files:
        config = load_config(str(yf))
        async with WorkflowBuilder.from_config(config) as builder:
            assert builder is not None
