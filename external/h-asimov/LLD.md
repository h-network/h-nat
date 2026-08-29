# Low-Level Design (LLD) — `h-asimov`

> **Canonical Document Status:**
> This document describes the **current code as it exists today**. As of this writing, **no `h-asimov` code exists in this repo (`h-nat`)** — `external/h-asimov/` contains only a `.gitkeep` placeholder. What follows documents (a) the real, tested gate logic that exists in a sibling repo and is the porting source for this module, and (b) the target package shape this module is expected to converge on. Section 5 marks explicitly what is ported vs. newly built. Where this document's plan turns out to differ from what actually gets built here, this file is the one to update — not a historical record to preserve unchanged.

---

## 1. Current State in This Repo (`h-nat`)

```
external/h-asimov/
├── .gitkeep     # placeholder only
├── HLD.md       # this module's high-level design
└── LLD.md       # this document
```

No `pyproject.toml`, no `src/`, no tests exist here yet. `h-asimov` is listed in the top-level `README.md` as one of five composable plugins, but is not yet installable or importable from this repo.

---

## 2. Porting Source: `h-network-asimov-firewall`

**Repo**: `git@github.com:h-network/h-network-nemo-agent-toolkit.git`
**Path**: `external/h-network-asimov-firewall/`
**Commit verified against**: `bcb4e3744df8c8e22d7cab0c1ea4addf40c7aeba` (`main`)

This is real, tested code — not a description of planned work. Verified directly at the commit above:

```
external/h-network-asimov-firewall/
├── pyproject.toml                                  # name=h-network-asimov-firewall, deps: nvidia-nat, httpx
├── requirements.txt / requirements-test.txt
├── README.md
├── defaults/
│   ├── denylist.default.txt
│   └── groundRules.default.md
├── source/                                         # carried-over reference logic (pre-port)
│   ├── firewall.py                                 # Decision pipeline: Firewall Protocol, AsimovFirewall
│   ├── denylist.py                                 # Layer 1: substring denylist
│   ├── asimov.py                                   # Layer 2: LLM judge (stateless HTTP call)
│   └── noop.py                                     # NoopFirewall: always-ALLOW variant
├── src/nat/plugins/h_network_asimov_firewall/
│   ├── __init__.py                                 # module docstring only
│   └── register.py                                 # NAT registration — raises NotImplementedError
└── tests/
    ├── conftest.py                                  # FakeAsimov, FakeDenylist stubs
    ├── test_firewall.py
    ├── test_denylist.py
    ├── test_asimov.py
    └── test_noop_firewall.py
```

### 2.1 `source/firewall.py` — the Decision pipeline
- `Verdict` — `ALLOW` / `DENY` enum.
- `Decision` (frozen dataclass) — `verdict`, `rule_id: Optional[str]`, `brief: Optional[str]`, `gate_error_message: Optional[str]`. `rule_id`/`brief` populate only for rule-based denials (L1 or L2); the fail-closed judge-error path sets `verdict=DENY, rule_id=None` so the caller can distinguish "judged unsafe" from "gate itself failed."
- `Firewall` — a `Protocol` (not a base class) defining `async def evaluate(*, command, task_id, execute, emit_event=None, model_name=None) -> tuple[Decision, Optional[T]]`. Two stock implementations satisfy it: `AsimovFirewall` and `NoopFirewall`.
- `AsimovFirewall.evaluate`:
  1. Calls `self._denylist.check(command)`. On a hit: emits `EV_DENYLIST_BLOCK`, returns `Decision(DENY, rule_id="layer1.denylist", brief=...)`, result `None`. Judge is never invoked.
  2. Otherwise calls `self._asimov.evaluate(command)`, timing it (`latency_ms`).
     - `ALLOW` → emits `EV_ASIMOV_ALLOW`, falls through.
     - `DENY` → emits `EV_ASIMOV_DENY`, returns `Decision(DENY, rule_id="layer2.asimov", brief=reason)`.
     - `ERROR` → if `self._fail_open`: emits `EV_ASIMOV_ERROR_CONTINUING`, falls through to execute (fail-open path). Else: emits `EV_ASIMOV_ERROR_FAILING`, returns `Decision(DENY, rule_id=None, gate_error_message=sanitized)` — **without** `brief`, since this isn't a rule-based denial.
  3. On fall-through (ALLOW or fail-open error): emits `EV_GATE_ALLOW_STARTING`, awaits `execute()`, returns `Decision(ALLOW)` plus the result.
  - Construction: `__init__(*, denylist: Denylist, asimov: Asimov, fail_open: bool)`, dependency-injected for testability. `from_env()` is the production factory.
  - Brief/message sanitization (`_sanitize_brief`, `_sanitize_message`): strips non-printable characters, truncates to 200 chars, so a verbose upstream exception can't bloat the event/result payload.

### 2.2 `source/denylist.py` — Layer 1
- `_normalize(command)`: lowercase, strip both `"` and `'`, collapse whitespace runs to single spaces.
- `Denylist.check(command)`: normalizes, then does a plain substring test against each configured pattern (also lowercased at load time); returns `DenylistHit(pattern_name=<the matched pattern>)` or `None`. First match wins; not a regex engine.
- `Denylist.from_env()`: always loads the packaged `defaults/denylist.default.txt`, then *appends* patterns from `NEMO_STACK_DENYLIST_PATH` if that env var is set. If the env var is set but the file doesn't exist, this raises — deliberately: a configured-but-missing override is treated as an operator error, not silently ignored.

### 2.3 `source/asimov.py` — Layer 2
- Stateless: one HTTP call per `evaluate(command)`, submitting the full ground-rules text plus the command, in a fixed prompt template (`ASIMOV_PROMPT_TEMPLATE`), no history.
- **Current implementation detail — uses `urllib.request` (stdlib), not `httpx`**, despite `httpx>=0.27,<1` being declared as a runtime dependency in this package's `pyproject.toml`. See discrepancy log (§6.1).
- Config via env vars: `NEMO_STACK_ASIMOV_BASE_URL`, `NEMO_STACK_ASIMOV_AUTH_TOKEN`, `NEMO_STACK_ASIMOV_MODEL`, `NEMO_STACK_ASIMOV_FAIL_OPEN` (default `false`), `NEMO_STACK_ASIMOV_TIMEOUT_SEC` (default 30), `NEMO_STACK_GROUND_RULES_PATH` (falls back to the packaged `groundRules.default.md`).
- `_parse_verdict` accepts exactly three response shapes on the first non-blank line: `ALLOW` (optionally with a trailing space-prefixed comment), `DENY:` with a reason, or bare `DENY` (reason defaults to `"(no reason given)"`). Anything else is `ERROR` — a parse failure is explicitly **not** treated as a DENY.
- `evaluate()` runs the sync HTTP call via `asyncio.to_thread`.
- Missing base URL, missing model, or empty ground rules are `ERROR` outcomes at call time, not raised exceptions.

### 2.4 `source/noop.py` — always-ALLOW variant
- `NoopFirewall.evaluate`: ignores `command`/`task_id`/`model_name`, emits `EV_GATE_SKIPPED` (`reason="firewall=noop"`) if `emit_event` is given, awaits `execute()`, returns `Decision(ALLOW)`.
- Selected by operators via `NEMO_STACK_FIREWALL=noop` in the predecessor's env-based factory convention.
- Intended for dev/test deployments and for operators running their own external safety layer.

### 2.5 `src/nat/plugins/h_network_asimov_firewall/register.py` — the stub
Confirmed: the module body is exactly

```python
raise NotImplementedError(
    "h_asimov_gate NAT registration is scaffolding — Phase 3 work, see "
    "this module's register.py docstring + repo ROADMAP.md."
)
```

The docstring above that line is a full spec (workflow YAML shape, `command: str` input, `Decision`-shaped output, audit event names, and an explicit TODO list) but **no `AsimovGateConfig`, no `@register_function`-decorated builder, and no NAT wiring exists** — the registration layer is scaffolding only, not partial code.

### 2.6 Tests
Real, passing coverage in the predecessor repo: `test_firewall.py` (pipeline/decision logic via `FakeAsimov`/`FakeDenylist`), `test_denylist.py`, `test_asimov.py`, `test_noop_firewall.py`. `conftest.py` supplies `FakeAsimov` (canned outcome queue) and `FakeDenylist` (fixed hit/no-hit) so `AsimovFirewall` is tested without any network dependency, plus an `envelope` fixture shaped like a NAT/dispatcher task payload.

---

## 3. Target Package Shape (`h-nat`, PEP 420 convention)

Following this repo's existing plugins (`h-memory`, `h-orchestrator`, etc.), the target layout is:

```
external/h-asimov/
├── pyproject.toml                          # entry point: nat.plugins.h_asimov.register
├── requirements.txt
├── requirements-test.txt
├── HLD.md
├── LLD.md
├── src/nat/plugins/h_asimov/
│   ├── __init__.py
│   ├── register.py                         # AsimovGateConfig, @register_function, FunctionInfo.from_fn
│   └── _internal/                          # migrated firewall/denylist/asimov/noop modules
│       ├── firewall.py
│       ├── denylist.py
│       ├── asimov.py
│       └── noop.py
├── defaults/
│   ├── denylist.default.txt
│   └── groundRules.default.md
└── tests/
    ├── conftest.py
    ├── test_firewall.py
    ├── test_denylist.py
    ├── test_asimov.py
    └── test_noop_firewall.py
```

Note the package name differs deliberately from the predecessor: `nat.plugins.h_network_asimov_firewall` → `nat.plugins.h_asimov`, matching this repo's shorter naming convention (`h_memory`, `h_orchestrator`, etc.) rather than the `h_network_*` prefix used in the source toolkit repo.

---

## 4. Ported vs. Newly Built

| Component | Status | Notes |
| :--- | :--- | :--- |
| `firewall.py` (Decision pipeline, `AsimovFirewall`, `Firewall` protocol) | **Port** | Logic carries over as-is; only import paths change (`_internal/` package). |
| `denylist.py` (Layer 1) | **Port** | As-is. |
| `noop.py` (always-ALLOW variant) | **Port** | As-is. |
| `asimov.py` (Layer 2 judge) | **Port + rework** | Pipeline/parsing logic (`ASIMOV_PROMPT_TEMPLATE`, `_parse_verdict`, three-way outcome) carries over. The HTTP transport does **not** carry over as-is: the predecessor's env-var-driven `urllib.request` call must be replaced with NAT's LLM resource lookup via `llm_name`, per the target config shape already specified in the predecessor's own `register.py` docstring. |
| `AsimovGateConfig` (`FunctionBaseConfig`) | **New** | Does not exist anywhere yet; fields per the predecessor's `register.py` docstring (`llm_name`, `ground_rules`/`ground_rules_inline`, `denylist`, `fail_open`). |
| `@register_function`-decorated builder / `register.py` body | **New** | The predecessor's `register.py` is a stub (`raise NotImplementedError`) with a spec docstring — the implementation itself is entirely new work. |
| Audit/telemetry wiring (Phoenix span + structured event) | **New** | Predecessor emits events via a generic `emit_event` callback with no concrete sink; wiring to NAT's actual telemetry surface is new. |
| Tests | **Port, then extend** | Existing four test files port with import-path updates only; new tests are needed for the `register.py` builder itself, which has no predecessor coverage (it doesn't exist yet). |

---

## 5. Discrepancy & Drift Log

This section records places where the predecessor's own documentation/config disagrees with its own code, or where this document's plan may drift from what's actually built here. Entries should be dated and kept even after resolution, with a note on how they were resolved.

### 5.1 `register.py` docstring claims `asimov.py` "uses httpx directly" — it doesn't
- **Claim** (predecessor `register.py` docstring, "Implementation pointer" section): *"The current asimov.py uses httpx directly against an OpenAI-compatible endpoint; the port should replace that with NAT's LLM resource lookup..."*
- **Actual code** (`source/asimov.py`, verified at commit `bcb4e374`): uses `urllib.request` / `urllib.error` (stdlib), not `httpx`. There is no `import httpx` anywhere in `source/`.
- **Also inconsistent**: the package's own `pyproject.toml` declares `httpx>=0.27,<1` as a runtime dependency, which is unused by any code in the package as it stands.
- **Impact on this port**: none of the substance — the port target (NAT's LLM abstraction via `llm_name`) is unaffected either way, since neither `urllib` nor `httpx` survives the port. Flagging this so the eventual `h-asimov` `pyproject.toml` doesn't cargo-cult an `httpx` dependency that was never actually load-bearing.
- **Status**: open — no action needed until the port's dependency list is written; resolve by simply not carrying `httpx` forward unless the port introduces a real use for it.

### 5.2 No code exists in `h-nat` yet for a module the top-level `README.md` already advertises
- **Observation**: `h-nat/README.md` describes `h-asimov` as one of five available plugins ("pre-flight safety gate: denylist + LLM judge, before anything executes") in the same breath as the other four, without qualifying that it's unimplemented here.
- **Impact**: a reader of the top-level README alone would not know to check this LLD's §1 before assuming `h-asimov` is installable.
- **Status**: open — flagged for the lead; not something this HLD/LLD pair can fix unilaterally without altering the top-level README's framing.
