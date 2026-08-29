# Low-Level Design (LLD) — `h-asimov`

> **Canonical Document Status:**
> This document describes the **current code as it exists today**. `h-asimov` is now implemented in this repo (`h-nat`) — the earlier revision of this LLD, written before implementation started, documented a `.gitkeep`-only placeholder and a target shape; §1 and §3 below now describe what was actually built, and §5 records where the build diverged from that earlier plan (and from the porting source), with evidence. Where this document's description turns out to differ from what's actually in the tree, this file is the one to update — not a historical record to preserve unchanged.

---

## 1. Current State in This Repo (`h-nat`)

```
external/h-asimov/
├── pyproject.toml
├── requirements.txt
├── requirements-test.txt
├── README.md
├── HLD.md
├── LLD.md
├── src/nat/plugins/h_asimov/
│   ├── __init__.py
│   ├── register.py                 # AsimovGateConfig, GateDecision, h_asimov_gate
│   ├── defaults/
│   │   ├── denylist.default.txt
│   │   └── groundRules.default.md
│   └── _internal/
│       ├── __init__.py
│       ├── firewall.py             # ported: Decision pipeline
│       ├── denylist.py             # ported: Layer 1
│       ├── asimov.py               # ported + reworked: Layer 2, NAT LLM transport
│       └── noop.py                 # ported: always-ALLOW variant
└── tests/
    ├── conftest.py
    ├── test_firewall.py
    ├── test_denylist.py
    ├── test_asimov.py
    ├── test_noop_firewall.py
    └── test_register.py            # new: AsimovGateConfig + h_asimov_gate builder
```

Installable and importable: `pip install -e external/h-asimov` (or a built wheel) registers `h_asimov_gate` under the `nat.components` entry point. Verified directly (this branch): `pip install -e .` succeeds, `python -m pytest tests/` passes 50/50, and a real (non-editable) wheel build (`python -m build --wheel`) installs cleanly into a fresh venv with `importlib.resources` correctly resolving the packaged `defaults/` files — see §5.3 and §5.5 for why that packaging detail needed a deliberate decision rather than a straight port.

---

## 2. Porting Source: `h-network-asimov-firewall`

> The subsections below describe the predecessor as it exists in its own repo — this is what was ported *from*, not what's in `h-nat` today. See §3 for the actual `h-asimov` code in this repo, and §4 for exactly what changed in the port.

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
- **Current implementation detail — uses `urllib.request` (stdlib), not `httpx`**, despite `httpx>=0.27,<1` being declared as a runtime dependency in this package's `pyproject.toml`. See discrepancy log (§5.1).
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

## 3. Implemented Package Shape (`h-nat`, PEP 420 convention)

The actual layout (see §1) follows this repo's existing plugin convention (`h-memory`, `h-orchestrator`, etc.), with one deliberate deviation from the shape originally drafted in this LLD before implementation: `defaults/` lives *inside* the package (`src/nat/plugins/h_asimov/defaults/`), not as a sibling of `src/` at the module root. See §5.5 for why.

Package name differs deliberately from the predecessor: `nat.plugins.h_network_asimov_firewall` → `nat.plugins.h_asimov`, matching this repo's shorter naming convention (`h_memory`, `h_orchestrator`, etc.) rather than the `h_network_*` prefix used in the source toolkit repo.

### 3.1 `register.py` — what's actually there
- `AsimovGateConfig(FunctionBaseConfig, name="h_asimov_gate")`: `mode: Literal["asimov", "noop"] = "asimov"`, `llm_name: LLMRef | None`, `ground_rules: str | None`, `ground_rules_inline: str | None`, `denylist: str | None`, `fail_open: bool = False`, `timeout_sec: float = 30.0`. A `model_validator(mode="after")` enforces (only when `mode == "asimov"`): `llm_name` is required; exactly one of `ground_rules` / `ground_rules_inline` is required. See §5.6 for why `mode` exists at all — it isn't in the predecessor's spec docstring.
- `GateDecision(BaseModel)`: `verdict: Literal["ALLOW", "DENY"]`, `layer: Literal["L1_denylist", "L2_asimov", "passthrough", "gate_error"]`, `reason: str | None`. `gate_error` is a fourth layer value beyond the three the predecessor's spec docstring lists — see §5.7.
- `h_asimov_gate(config, builder)`: builds a `NoopFirewall` (mode=`noop`) or an `AsimovFirewall` wired to `builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)` (mode=`asimov`), then yields `FunctionInfo.from_fn(_gate, ...)` where `_gate(command: str) -> GateDecision` calls `firewall.evaluate(...)` with a no-op `execute` callback and maps the resulting `Decision` to a `GateDecision`. See §5.8 for why `execute` is a no-op here — `h_asimov_gate` is a pure judge, it does not run the gated action itself.
- Emits events via `logging.getLogger(__name__).info(...)`, not a Phoenix span — see §4's audit/telemetry row.

---

## 4. Ported vs. Newly Built

| Component | Status | Notes |
| :--- | :--- | :--- |
| `firewall.py` (Decision pipeline, `AsimovFirewall`, `Firewall` protocol) | **Ported** | Logic carries over unchanged; only the `from_env()` classmethod is dropped (construction is NAT-config-driven now, done in `register.py`). |
| `denylist.py` (Layer 1) | **Ported, one fix** | `_normalize`, `_parse_patterns`(`_read_patterns`), `Denylist.check` unchanged. `from_env()` → `Denylist.from_texts(default_text, override_path)`: same "loud on missing override" semantics, parameterized instead of env-driven, and *not* reusing the predecessor's default-path computation — see §5.3, it's broken. |
| `noop.py` (always-ALLOW variant) | **Ported** | Unchanged behavior; `from_env()` dropped (selection is the `mode` config field, not an env var — §5.6). |
| `asimov.py` (Layer 2 judge) | **Ported + reworked** | `ASIMOV_PROMPT_TEMPLATE` and the three-way ALLOW/DENY/ERROR parsing rules carry over (adapted to parse already-extracted text instead of a raw HTTP JSON body). The transport is new: `Asimov.__init__` now takes an injected NAT LLM client (`llm: Any`, a langchain `BaseChatModel` obtained via `builder.get_llm(..., wrapper_type=LLMFrameworkEnum.LANGCHAIN)`) instead of building its own `urllib.request` call from env vars. `evaluate()` wraps the call in `asyncio.wait_for(timeout_sec)`; a timeout is an `ERROR` outcome, same as any other judge failure. |
| `AsimovGateConfig` (`FunctionBaseConfig`) | **Built** | Fields per the predecessor's `register.py` docstring (`llm_name`, `ground_rules`/`ground_rules_inline`, `denylist`, `fail_open`), plus `mode` and `timeout_sec` (§5.6). |
| `GateDecision` (output schema) | **Built** | Not in the predecessor's spec docstring in this exact shape — see §5.7 for the `gate_error` layer value. |
| `h_asimov_gate` builder (`register.py` body) | **Built** | The predecessor's `register.py` was a stub (`raise NotImplementedError`); the implementation here is entirely new, following its spec docstring's YAML shape plus the `mode` extension (§5.6). |
| Audit/telemetry wiring | **Built, minimally** | Predecessor's `emit_event` callback had no concrete sink either. Here it logs structured events via `logging.getLogger(__name__).info(...)` (same shape as `h-memory`'s "structured logging" pattern). Wiring to a real telemetry sink (e.g. a Phoenix span, if NAT's own observability surface calls for one) is still open — not attempted here, since it wasn't ticket scope and NAT's own tool/function code in the installed `nvidia-nat` package uses plain `logging` too, not a bespoke span API. |
| Tests | **Ported + extended** | `test_firewall.py`, `test_denylist.py`, `test_noop_firewall.py` port with import-path updates; the predecessor's env-based/`from_env` test cases in each are replaced with the config-driven equivalents. `test_asimov.py`'s parser tests port with a text-instead-of-JSON-bytes adaptation; its `from_env`/raw-HTTP tests are replaced with `evaluate()`-level tests against a fake LLM client. `test_register.py` is new — no predecessor coverage exists for the builder. 50/50 tests pass (verified this branch). |

---

## 5. Discrepancy & Drift Log

This section records places where the predecessor's own documentation/config disagrees with its own code, or where this document's plan may drift from what's actually built here. Entries should be dated and kept even after resolution, with a note on how they were resolved.

### 5.1 `register.py` docstring claims `asimov.py` "uses httpx directly" — it doesn't
- **Claim** (predecessor `register.py` docstring, "Implementation pointer" section): *"The current asimov.py uses httpx directly against an OpenAI-compatible endpoint; the port should replace that with NAT's LLM resource lookup..."*
- **Actual code** (`source/asimov.py`, verified at commit `bcb4e374`): uses `urllib.request` / `urllib.error` (stdlib), not `httpx`. There is no `import httpx` anywhere in `source/`.
- **Also inconsistent**: the package's own `pyproject.toml` declares `httpx>=0.27,<1` as a runtime dependency, which is unused by any code in the package as it stands.
- **Impact on this port**: none of the substance — the port target (NAT's LLM abstraction via `llm_name`) is unaffected either way, since neither `urllib` nor `httpx` survives the port. Flagging this so the eventual `h-asimov` `pyproject.toml` doesn't cargo-cult an `httpx` dependency that was never actually load-bearing.
- **Status**: open — no action needed until the port's dependency list is written; resolve by simply not carrying `httpx` forward unless the port introduces a real use for it.

### 5.2 No code existed in `h-nat` yet for a module the top-level `README.md` already advertised
- **Observation**: at the time the HLD/LLD pair was first written, `h-nat/README.md` described `h-asimov` as one of five available plugins in the same breath as the other four, without qualifying that it was unimplemented.
- **Status**: resolved by this branch — `h-asimov` is now actually implemented, so the README's framing is accurate again. Left in this log as a record that the gap existed and why (a reader of the top-level README alone would not have known to check this LLD's §1 before assuming installability).

### 5.3 The predecessor's packaged-default path resolution is broken — 7 of its own tests fail
- **Observation**: `source/denylist.py`'s `_PACKAGE_DEFAULT = Path(__file__).parent.parent / "denylist.default.txt"` and `source/asimov.py`'s equivalent `_PACKAGE_GROUND_RULES` both resolve to the repo root (`external/h-network-asimov-firewall/`), but the actual files live one level deeper, at `external/h-network-asimov-firewall/defaults/denylist.default.txt` and `.../defaults/groundRules.default.md`. The `defaults/` path segment is missing from the computation.
- **Evidence**: re-cloned the predecessor at the verified commit and ran its own suite: `python -m pytest tests/ -q` → **7 failed, 46 passed, 2 skipped**, every failure a `FileNotFoundError` on the wrong path (`.../h-network-asimov-firewall/denylist.default.txt`, not `.../defaults/denylist.default.txt`). Failing tests: `test_denylist.py::test_from_env_loads_package_default`, `::test_from_env_appends_operator_overrides`, `::test_from_env_loud_on_missing_file`; `test_asimov.py::test_from_env_uses_package_default_when_unset`; `test_noop_firewall.py::test_firewall_from_env_default_is_asimov`, `::test_firewall_from_env_asimov_explicit`, `::test_firewall_from_env_empty_string_treated_as_default`.
- **Correction to the office's earlier verification claim**: this repo's own earlier message (and this LLD's first revision) characterized the predecessor as having "real, tested" gate logic without actually running the suite — true in that the tests are real and most pass, but incomplete: a meaningful slice of that coverage (anything touching the packaged-default path) currently fails on `main` at the verified commit. Worth being precise about next time a predecessor's test status is asserted rather than checked.
- **Impact on this port**: not carried over. `Denylist.from_texts` (this repo) takes the default text as an explicit parameter, loaded in `register.py` via `importlib.resources.files("nat.plugins.h_asimov.defaults")` — see §5.5 — rather than reconstructing a `Path(__file__)`-relative guess. Verified working via both an editable install and a real wheel build/install in a clean venv (see §1).
- **Status**: resolved in this port; not something to fix upstream from this branch, but worth flagging to whoever owns `h-network-asimov-firewall` next.

### 5.4 `register.py` needs eager (non-deferred) type annotations, same as `h-memory`
- **Observation**: an early draft of this module's `register.py` included `from __future__ import annotations` (matching the predecessor's own style). This broke `FunctionInfo.from_fn`'s introspection: NAT 1.6+ resolves `_gate`'s type hints via `typing.get_type_hints()` at registration time, and deferred (string) annotations raised `NameError: name 'GateDecision' is not defined` even though `GateDecision` is defined earlier in the same module.
- **Evidence**: caught by this branch's own test suite (`test_register.py`) before ever reaching the lead — all 7 builder-integration tests failed with that `NameError` until the import was removed.
- **Impact**: none once caught — `h-memory`'s LLD (`external/h-memory/LLD.md` §5.2) already documents this exact NAT constraint; this is a second confirmation of it, not a new discovery. Recorded here so a future contributor touching `register.py` doesn't reintroduce it.
- **Status**: resolved — `from __future__ import annotations` is not used in `register.py`, with a comment at the top of the file explaining why.

### 5.5 `defaults/` moved inside the package, not at the module root as first drafted
- **Observation**: this LLD's pre-implementation draft (and the predecessor's own layout) placed `defaults/` as a sibling of `src/` at the package root. That doesn't survive a real `pip install`: `[tool.setuptools.packages.find]` with `where=["src"]` only packages what's under `src/`, so a root-level `defaults/` would silently not ship in the wheel — only present for local/editable installs run from a source checkout.
- **Impact**: moved to `src/nat/plugins/h_asimov/defaults/`, declared via `[tool.setuptools.package-data]`, and read at runtime with `importlib.resources.files("nat.plugins.h_asimov.defaults")` instead of a `Path(__file__)`-relative guess (the approach that was broken in the predecessor — §5.3). Verified by building an actual wheel (`python -m build --wheel`) and installing it into a clean venv: the packaged files are present and readable via `importlib.resources`.
- **Status**: resolved — this LLD's §1 and §3 reflect the actual (in-package) location.

### 5.6 `mode: Literal["asimov", "noop"]` config field — not in the predecessor's spec docstring
- **Observation**: the predecessor's `register.py` docstring's YAML example only shows `llm_name`/`ground_rules`/`denylist`/`fail_open` — no field for selecting `NoopFirewall`. In the predecessor itself, that selection was a separate env var (`NEMO_STACK_FIREWALL=asimov|noop`) read by a `firewall_from_env()` factory, entirely outside the NAT registration surface (which didn't exist).
- **Decision**: added `mode` to `AsimovGateConfig` so the audited opt-out (`NoopFirewall`, still emitting `gate_skipped`) is reachable declaratively in NAT workflow YAML, without reintroducing an env-var side-channel NAT config doesn't otherwise use. `llm_name`/`ground_rules`/`ground_rules_inline` are only required (via a `model_validator`) when `mode == "asimov"`.
- **Status**: resolved — confirmed by the architect: keep as-is. Reasonable, additive, doesn't conflict with the predecessor's spec.

### 5.7 `GateDecision.layer` has a fourth value, `gate_error`, beyond the docstring's three
- **Observation**: the predecessor's `register.py` docstring lists exactly three layer values: `L1_denylist`, `L2_asimov`, `passthrough`. But `_internal/firewall.py`'s `Decision` dataclass encodes a fourth, distinct case on purpose: a fail-closed judge error sets `rule_id=None` specifically so callers don't confuse "the judge looked at this and denied it" (`L2_asimov`) with "the judge itself couldn't be reached/parsed" (frozen-dataclass docstring: caller should map this to `gate_internal_error`, *not* `firewall_denied`). Collapsing that into `L2_asimov` would erase a distinction the predecessor's own pipeline was explicitly designed to preserve.
- **Decision**: `GateDecision.layer` includes `"gate_error"` as a fourth literal value, carrying `Decision.gate_error_message` as `reason`.
- **Status**: resolved — confirmed by the architect: keep as-is. Preserves a real safety-relevant distinction (judged-unsafe vs. gate-itself-broke) worth keeping explicit in the output shape.

### 5.8 `h_asimov_gate` is a pure judge — it does not execute the gated action
- **Observation**: the `Firewall.evaluate` protocol (ported as-is, §4) takes an `execute` callback and only calls it on ALLOW — in the predecessor, a dispatcher held `gateway.client.submit` in closure as that callback, so gating and execution were one call. The NAT registration docstring instead describes `h_asimov_gate` as taking `command: str` and returning a Decision — no mention of an execute callback, since a NAT function only takes one input.
- **Decision**: `register.py`'s `_gate` passes a no-op `execute` (returns `None`, result discarded) to satisfy the `Firewall.evaluate` contract, and returns only the mapped `GateDecision`. The calling workflow is responsible for deciding what to do with an ALLOW/DENY verdict (e.g. branching to an actual execution function). This matches the docstring's literal description of the function's I/O, but is worth being explicit about since it's a structural difference from how the predecessor's own dispatcher used the same `Firewall` protocol.
- **Status**: resolved — confirmed by the architect: pure judge, separate from execute, is the correct integration shape. No bundling.
