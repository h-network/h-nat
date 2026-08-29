"""Round-38/54/56/67 build check — exercises NAT's WorkflowBuilder
against ``build-check.yaml`` (and, from round 67 onward,
``build-check-split.yaml``) without invoking any function.

Pass criterion: all three function ``_type:``s build successfully (the
NAT 1.6.0 surface — Pydantic config validation, type-hint resolution,
``str → PydanticModel`` converter registration, ``ConfigDict(extra="forbid")``
enforcement, builder body up to but not past the first Redis command —
all green) without any external Redis being reachable.

Round 67 adds the split-topology variant: ``build-check-split.yaml``
declares ``hot_redis_url`` distinct from ``redis_url`` (both deliberately
unreachable). A positive PASS demonstrates that BOTH the colocated and
split-topology sweep configs build correctly under the round-67
dual-client refactor.

Run:

    python3 external/h-network-semantic-memory/_verify/check.py
"""
import asyncio
import sys
from pathlib import Path

from nat.builder.workflow_builder import WorkflowBuilder
from nat.runtime.loader import load_config


async def _build_one(yaml_path: Path, label: str) -> None:
    print(f"--- {label} ({yaml_path.name}) ---")
    config = load_config(str(yaml_path))
    async with WorkflowBuilder.from_config(config) as builder:
        for fn_name in ("search", "sweep", "vectorize"):
            fn = await builder.get_function(fn_name)
            print(f"  {fn_name}: built OK ({type(fn).__name__})")


async def main() -> int:
    here = Path(__file__).parent
    await _build_one(here / "build-check.yaml", "colocated topology")
    await _build_one(here / "build-check-split.yaml", "split topology (round-67)")
    print("build check: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
