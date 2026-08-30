# h-nat

NeMo Agent Toolkit plugins for agent orchestration, memory, and Asimov-gated safety — with optional NVIDIA OpenShell sandboxing.

Five composable plugins, install what you need:

- **h-openshell** — gRPC client for NVIDIA OpenShell sandboxes.
- **h-orchestrator** — invoke/stream a coding-agent CLI as a NAT function.
- **h-memory** — bounded per-chat conversation memory in Redis.
- **h-recall** — long-term semantic memory, hybrid search over vectorized history.
- **h-asimov** — pre-flight safety gate: denylist + LLM judge, before anything executes.

## CI

`.github/workflows/ci.yml` discovers every module under `external/` (anything
with a `pyproject.toml`), then runs each one's pytest suite and `nat validate`
against its `examples/**/*.yaml` in its own isolated venv and its own native
GitHub Actions job/check — one module's failure doesn't block the others. A
lint job (`ruff`, config in `ruff.toml`) runs first; it's currently
non-blocking pending per-module cleanup of pre-existing findings. `nat
validate` is schema/config validation only, no live Redis/vLLM/Junos endpoint
required.

Run the same checks locally:

```bash
./ci/scripts/discover_modules.py          # list modules
./ci/scripts/run_tests.py                 # all modules
./ci/scripts/run_tests.py --project external/h-asimov   # one module
./ci/scripts/lint.sh                      # full-tree lint
```
