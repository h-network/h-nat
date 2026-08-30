#!/usr/bin/env bash
# Lint gate. On a PR, lints only files changed vs the PR base (cheap); on a
# push (e.g. to main), lints the whole tree.
#
# Ruleset (ruff.toml at repo root) is deliberately minimal for now -- see the
# comment there. This job is currently non-blocking (doesn't gate the test
# matrix) because ~110 pre-existing findings are spread across every module's
# code; flip `needs: lint` on in .github/workflows/ci.yml once those land as
# their own per-module cleanup tickets.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

RUFF_CONFIG="${REPO_ROOT}/ruff.toml"

if [[ "${GITHUB_EVENT_NAME:-}" == "pull_request" && -n "${BASE_SHA:-}" ]]; then
    echo "PR event: linting only .py files changed since ${BASE_SHA}"
    mapfile -t FILES < <(git diff --name-only --diff-filter=ACMR "${BASE_SHA}...HEAD" -- '*.py')
    if [[ ${#FILES[@]} -eq 0 ]]; then
        echo "No changed .py files, nothing to lint."
        exit 0
    fi
    printf 'Linting %d file(s):\n' "${#FILES[@]}"
    printf '  %s\n' "${FILES[@]}"
    ruff check --config "${RUFF_CONFIG}" "${FILES[@]}"
else
    echo "Non-PR event: linting the full tree"
    ruff check --config "${RUFF_CONFIG}" .
fi
