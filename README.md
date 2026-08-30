# h-nat

[![CI](https://github.com/h-network/h-nat/actions/workflows/ci.yml/badge.svg)](https://github.com/h-network/h-nat/actions/workflows/ci.yml)

NeMo Agent Toolkit plugins for agent orchestration, memory, and Asimov-gated safety — with optional NVIDIA OpenShell sandboxing.

Five composable plugins, install what you need:

- **h-openshell** — gRPC client for NVIDIA OpenShell sandboxes.
- **h-orchestrator** — invoke or stream any sandboxed CLI, compose Redis-backed
  chat cycles, and gate hidden MCP tools as NAT functions.
- **h-memory** — bounded per-chat conversation memory in Redis.
- **h-recall** — long-term semantic memory, hybrid search over vectorized history.
- **h-asimov** — pre-flight safety gate: denylist + LLM judge, before anything executes.

## CI

`.github/workflows/ci.yml` discovers every module under `external/` (anything
with a `pyproject.toml`), then runs each one's pytest suite and `nat validate`
against its `examples/**/*.yaml` in its own isolated venv and its own native
GitHub Actions job/check — one module's failure doesn't block the others. A
lint job (`ruff`, config in `ruff.toml`) runs first and blocks the test
matrix — the whole repo is clean under ruff's default ruleset. `nat
validate` is schema/config validation only, no live vLLM/Junos endpoint
required. The test job runs a `redis/redis-stack-server` service container on
`localhost:6379`: h-memory's integration tests connect to a real Redis (its
config's `redis_url` field defaults to that address regardless of env vars),
no other module's pytest suite currently touches Redis for real.

This is a private repo and Actions minutes cost money, so the workflow only
runs on manual trigger (`workflow_dispatch`), not on every push/PR. The badge
above reflects whatever the last manually-triggered run showed, not
continuous/live status on `main`. To run it:

- **GitHub UI**: repo → Actions tab → "CI" workflow → "Run workflow" → pick
  the branch → Run workflow.
- **`gh` CLI** (needs `gh auth login` or a `GH_TOKEN` with Actions access):
  `gh workflow run ci.yml --ref <branch>`

Run the same checks locally (a Redis reachable at `localhost:6379` is only
needed for h-memory's suite):

```bash
./ci/scripts/discover_modules.py          # list modules
./ci/scripts/run_tests.py                 # all modules
./ci/scripts/run_tests.py --project external/h-asimov   # one module
./ci/scripts/lint.sh                      # full-tree lint
```
