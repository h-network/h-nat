#!/usr/bin/env bash
# Lint gate. On a PR, lints only files changed vs the PR base (cheap); on a
# push (e.g. to main), lints the whole tree.
#
# Deliberately does NOT pass --config: an explicit --config forces that one
# file for everything and silently defeats any module's own nested
# ruff.toml/.ruff.toml (e.g. h-openshell excludes its generated protobuf
# sources this way). Running from the repo root lets ruff's normal discovery
# pick the nearest config per file -- the root ruff.toml for everything
# without a closer one.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

if [[ "${GITHUB_EVENT_NAME:-}" == "pull_request" && -n "${BASE_SHA:-}" ]]; then
    echo "PR event: linting only .py files changed since ${BASE_SHA}"
    mapfile -t FILES < <(git diff --name-only --diff-filter=ACMR "${BASE_SHA}...HEAD" -- '*.py')
    if [[ ${#FILES[@]} -eq 0 ]]; then
        echo "No changed .py files, nothing to lint."
        exit 0
    fi
    printf 'Linting %d file(s):\n' "${#FILES[@]}"
    printf '  %s\n' "${FILES[@]}"
    ruff check "${FILES[@]}"
else
    echo "Non-PR event: linting the full tree"
    ruff check .
fi
