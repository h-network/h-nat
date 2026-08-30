#!/usr/bin/env python3
"""Run one module's test suite (and nat validate its examples) in an isolated venv.

Single module (what CI's matrix legs call, one per discovered module):
    ./ci/scripts/run_tests.py --project external/h-asimov

All discovered modules, sequentially (local convenience -- no CI matrix needed):
    ./ci/scripts/run_tests.py

Each module gets its own throwaway venv. Modules with intra-repo dependencies
(e.g. h-orchestrator depends on h-asimov/h-memory/h-openshell, and its
"example" extra pulls in h-recall, all by bare package name, none of which are
published) get their declared local siblings installed editable first, so pip
finds the unversioned requirement already satisfied locally instead of
reaching for PyPI.

nat validate runs against every top-level examples/<module-dir-name>/**/*.yaml
with a top-level `workflow:` key, using dummy values for the ${VAR}
placeholders those configs interpolate (H_NAT_LLM_MODEL etc.) -- validate only
checks schema/config shape and makes no network calls, so no real
Redis/vLLM/Junos endpoint is needed.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import venv
from pathlib import Path

from discover_modules import REPO, discover_modules

REPORTS_DIR_DEFAULT = REPO / ".artifacts" / "junit"

# Every module's own INSTALL.md/README documents installing this by hand before
# the module itself -- h-memory deliberately omits nvidia-nat from its own
# pyproject dependencies per its documented ADR-008 (plugins shouldn't pin the
# host NAT version), so it must be installed explicitly rather than assumed.
NVIDIA_NAT_SPEC = "nvidia-nat>=1.6,<2"

DUMMY_ENV = {
    "H_NAT_LLM_MODEL": "dummy-model",
    "H_NAT_LLM_BASE_URL": "http://127.0.0.1:0/v1",
    "OPENAI_API_KEY": "dummy-key",
    "H_NAT_REDIS_URL": "redis://127.0.0.1:0",
    "JUNOS_MCP_TOKEN": "dummy-token",
}

TOP_LEVEL_WORKFLOW_KEY = re.compile(r"^workflow:", re.MULTILINE)


def slug(project: Path) -> str:
    return project.relative_to(REPO).as_posix().replace("/", "__")


def declared_name(project: Path) -> str:
    with open(project / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["name"]


def local_dependencies(project: Path, name_to_path: dict[str, Path]) -> list[Path]:
    """Declared deps + optional-deps of `project` that resolve to another discovered module."""
    with open(project / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)

    raw_specs: list[str] = list(data.get("project", {}).get("dependencies", []))
    for extra_deps in data.get("project", {}).get("optional-dependencies", {}).values():
        raw_specs.extend(extra_deps)

    siblings = []
    for spec in raw_specs:
        dep_name = re.split(r"[<>=!~\[\s;]", spec, maxsplit=1)[0].strip()
        dep_path = name_to_path.get(dep_name)
        if dep_path is not None and dep_path != project and dep_path not in siblings:
            siblings.append(dep_path)
    return siblings


def find_example_configs(project: Path) -> list[Path]:
    # Examples live under a top-level examples/<module-dir-name>/ tree, not
    # inside external/<module>/ -- kept out of external/ so each module's own
    # tree stays a pure installable NAT package (see examples/README.md).
    examples_dir = REPO / "examples" / project.name
    if not examples_dir.is_dir():
        return []
    configs = []
    for yaml_file in sorted(examples_dir.rglob("*.yaml")):
        if TOP_LEVEL_WORKFLOW_KEY.search(yaml_file.read_text(encoding="utf-8")):
            configs.append(yaml_file)
    return configs


def sh(cmd: list[str], *, env: dict[str, str] | None = None) -> int:
    print(f"+ {' '.join(cmd)}", flush=True)
    try:
        return subprocess.run(cmd, cwd=REPO, env=env, check=False).returncode
    except FileNotFoundError as exc:
        print(f"  ! {exc}", flush=True)
        return 127


def run_one(project: Path, *, reports_dir: Path, name_to_path: dict[str, Path], keep_venv: bool = False) -> int:
    display = project.relative_to(REPO).as_posix()
    venv_dir = Path(tempfile.mkdtemp(prefix=f"h-nat-ci-{project.name}-"))
    venv_python = venv_dir / "bin" / "python"

    try:
        print(f"\n=== {display}: creating venv ===", flush=True)
        venv.create(venv_dir, with_pip=True)

        if sh([str(venv_python), "-m", "pip", "install", "--upgrade", "-q", "pip"]):
            return 1

        if sh([str(venv_python), "-m", "pip", "install", "-q", NVIDIA_NAT_SPEC]):
            print(f"=== {display}: FAILED installing {NVIDIA_NAT_SPEC} ===", flush=True)
            return 1

        siblings = local_dependencies(project, name_to_path)
        for sibling in siblings:
            if sh([str(venv_python), "-m", "pip", "install", "-q", "-e", str(sibling)]):
                print(f"=== {display}: FAILED installing sibling module {sibling} ===", flush=True)
                return 1

        if sh([str(venv_python), "-m", "pip", "install", "-q", "-e", f"{project}[test]"]):
            print(f"=== {display}: FAILED installing {display}[test] ===", flush=True)
            return 1

        reports_dir.mkdir(parents=True, exist_ok=True)
        junit_xml = reports_dir / f"{slug(project)}.xml"
        print(f"\n=== {display}: pytest ===", flush=True)
        pytest_rc = sh([
            str(venv_python),
            "-m",
            "pytest",
            str(project / "tests"),
            f"--junitxml={junit_xml}",
        ])

        configs = find_example_configs(project)
        validate_rc = 0
        if configs:
            print(f"\n=== {display}: nat validate ({len(configs)} example config(s)) ===", flush=True)
            validate_env = {**os.environ, **DUMMY_ENV}
            for config in configs:
                rc = sh(
                    [str(venv_dir / "bin" / "nat"), "validate", "--config_file", str(config)],
                    env=validate_env,
                )
                validate_rc = validate_rc or rc
        else:
            print(f"\n=== {display}: no examples/ configs to validate ===", flush=True)

        if pytest_rc:
            print(f"=== {display}: FAILED (pytest) ===", flush=True)
        if validate_rc:
            print(f"=== {display}: FAILED (nat validate) ===", flush=True)
        if not pytest_rc and not validate_rc:
            print(f"=== {display}: PASSED ===", flush=True)

        return 1 if (pytest_rc or validate_rc) else 0
    finally:
        if not keep_venv:
            shutil.rmtree(venv_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", help="run a single module, e.g. external/h-asimov")
    parser.add_argument("--reports-dir", default=str(REPORTS_DIR_DEFAULT), help="where to write JUnit XML")
    parser.add_argument("--keep-venv", action="store_true", help="don't delete the per-module venv (debugging)")
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    all_modules = discover_modules()
    name_to_path = {declared_name(m): m for m in all_modules}

    if args.project:
        project = (REPO / args.project).resolve()
        if not (project / "pyproject.toml").is_file():
            print(f"Not a module (no pyproject.toml): {project}", file=sys.stderr)
            return 2
        return run_one(project, reports_dir=reports_dir, name_to_path=name_to_path, keep_venv=args.keep_venv)

    if not all_modules:
        print("No modules discovered under external/", file=sys.stderr)
        return 2

    failures = 0
    for module in all_modules:
        if run_one(module, reports_dir=reports_dir, name_to_path=name_to_path, keep_venv=args.keep_venv):
            failures += 1

    print(f"\n{len(all_modules) - failures}/{len(all_modules)} modules passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
