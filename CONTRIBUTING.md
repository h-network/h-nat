# Contributing

## Repository layout

- `external/<module>/` — the installable module itself (matches the actual
  `pip install` layout, no example or benchmark content).
- `examples/<module>/<example-name>/` — runnable demos, each with its own
  README and `workflow.yaml`.
- `benchmark/<module>/` — adversarial and stress benchmark suites, each with
  a `vars.example.yaml` you can point at your own infrastructure.

## CI

`.github/workflows/ci.yml` discovers every module under `external/` (anything
with a `pyproject.toml`), then runs each one's pytest suite and `nat validate`
against its `examples/**/*.yaml` in its own isolated venv and its own native
GitHub Actions job/check — one module's failure doesn't block the others. A
lint job (`ruff`, config in `ruff.toml`) runs first and blocks the test
matrix — the whole repo is clean under ruff's default ruleset. `nat validate`
is schema/config validation only, no live vLLM/Junos endpoint required. The
test job runs a `redis/redis-stack-server` service container so h-memory's
integration tests connect to a real Redis.

The workflow runs on manual trigger (`workflow_dispatch`):

- **GitHub UI**: repo → Actions tab → "CI" workflow → "Run workflow" → pick
  the branch → Run workflow.
- **`gh` CLI**: `gh workflow run ci.yml --ref <branch>`

Run the same checks locally (a Redis reachable at `localhost:6379` is only
needed for h-memory's suite):

```bash
./ci/scripts/discover_modules.py          # list modules
./ci/scripts/run_tests.py                 # all modules
./ci/scripts/run_tests.py --project external/h-asimov   # one module
./ci/scripts/lint.sh                      # full-tree lint
```
