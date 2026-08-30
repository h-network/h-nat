#!/usr/bin/env python3
"""Discover installable modules in this repo (any directory with a pyproject.toml under external/).

Standalone: prints one module path per line.
    ./ci/scripts/discover_modules.py

CI matrix input: prints a single-line JSON array of module paths.
    ./ci/scripts/discover_modules.py --json
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SEARCH_ROOTS = ("external", )
MAX_DEPTH = 2
SKIP_DIRS = {"__pycache__", ".venv", "node_modules"}


def discover_modules(repo: Path = REPO) -> list[Path]:
    modules: list[Path] = []
    for root_name in SEARCH_ROOTS:
        root = repo / root_name
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            rel_depth = len(Path(dirpath).relative_to(root).parts)
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            if rel_depth >= MAX_DEPTH:
                dirnames[:] = []
            if "pyproject.toml" in filenames:
                modules.append(Path(dirpath))
    return sorted(modules)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print a single-line JSON array of relative paths")
    args = parser.parse_args()

    modules = [m.relative_to(REPO).as_posix() for m in discover_modules()]

    if args.json:
        print(json.dumps(modules))
    else:
        for module in modules:
            print(module)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
