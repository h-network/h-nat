# High-Level Design (HLD) — `h-asimov`

## 1. Executive Summary & Purpose

`h-asimov` is the pre-flight safety gate for the NeMo Agent Toolkit (NAT) runtime. It evaluates a single command/prompt against a configurable rules document *before* anything executes, and returns a typed allow/deny decision. It is the safety moat that sits in front of execution-capable NAT functions (most notably `h-openshell`'s sandbox dispatch) — nothing downstream of the gate runs unless the gate says so.

### 1.1 Core Objectives
- **Independent-context judgment**: the LLM judge evaluates each command statelessly, against a single ground-rules document, with no conversation history — a deliberate design property so prior turns in a session can't be used to talk the judge into an unsafe verdict.
- **Fast, cheap first pass**: a Layer 1 substring denylist rejects obviously unsafe commands without paying for an LLM round trip.
- **Fail-closed by default**: if the judge can't be reached or its response can't be parsed, the default posture is DENY, not ALLOW. Operators who want fail-open behavior (or no gate at all) opt into it explicitly.
- **Auditable, not silent**: every phase boundary — denylist hit, judge allow/deny, judge error, or the gate being skipped entirely — emits a structured event. An operator reading the audit trail should never have to infer that a task ran ungated; that's itself an observable fact.
- **Composable as a NAT function**: `h_asimov_gate` is workflow-callable (`_type: h_asimov_gate`) from any NAT YAML flow, not just from one specific caller.

---

## 2. Architecture & Public Interface

`h-asimov` exposes one NAT function, backed by a two-layer evaluator.

```
+---------------------------------------------------------------------+
|                     NeMo Agent Toolkit (NAT) Workflow                |
|                                                                      |
|   functions:                                                        |
|     bgp_gate:                                                       |
|       _type: h_asimov_gate                                          |
|       llm_name: judge_llm                                           |
|       ground_rules: defaults/bgp.md                                 |
|       denylist: defaults/denylist.default.txt                       |
|       fail_open: false                                              |
|                                                                      |
+------------------------------|---------------------------------------+
                                v
                    +-------------------------+
                    |     h_asimov_gate       |   command: str  -->
                    |    (NAT Function)       |   GateDecision (verdict/layer/reason)
                    +------------+------------+
                                 |
                                 v
                    +-------------------------+
                    |   Firewall (Protocol)   |
                    +------------+------------+
                        /                 \
                       v                   v
          +-------------------+   +-------------------+
          |   AsimovFirewall   |   |    NoopFirewall    |
          |  (default gate)    |   |  (always-ALLOW,    |
          |                    |   |   dev/test/opt-out)|
          +---------+----------+   +---------------------+
                    |
          +---------+----------+
          |                    |
          v                    v
  +---------------+   +-------------------------+
  |   Denylist    |   |         Asimov           |
  |  (Layer 1,    |   |  (Layer 2, LLM judge —   |
  |  substring    |   |  stateless, single-turn, |
  |  match)       |   |  ground-rules-only)      |
  +---------------+   +-------------------------+
```

### 2.1 NAT Function: `h_asimov_gate`
- **Input**: `command: str` — the action being gated (e.g. a shell command destined for an execution backend).
- **Output**: a `GateDecision` — verdict (`ALLOW`/`DENY`), which layer produced it (`L1_denylist` / `L2_asimov` / `passthrough` / `gate_error`), and a short human-readable reason. `h_asimov_gate` is a pure judge: it does not run the gated action itself — the calling workflow decides what to do with the verdict (see LLD.md §5.8).
- **Workflow configuration**:
  - `mode` — `asimov` (default, two-layer gate) or `noop` (always ALLOW, audited opt-out; see §2.2.3). See LLD.md §5.6.
  - `llm_name` — the NAT-registered LLM resource to use as the judge (so any NAT LLM backend works: NIM, OpenAI-compatible, Azure, etc.). Required when `mode=asimov`.
  - `ground_rules` (path) or `ground_rules_inline` (string) — the rules document the judge evaluates against. Exactly one required when `mode=asimov`.
  - `denylist` — path to substring patterns checked before the judge is invoked.
  - `fail_open` — default `false`. Governs behavior only when the judge itself errors (unreachable, malformed response); it has no effect on a judge-produced DENY.

### 2.2 Decision Semantics
1. **Layer 1 — denylist short-circuit.** The command is normalized (lowercased, quotes stripped, whitespace collapsed) and checked against the configured patterns. Any match is an immediate DENY with `layer=L1_denylist` — the judge is never called.
2. **Layer 2 — Asimov LLM judge.** If Layer 1 clears, the command is submitted to the judge alone, alongside the ground-rules document, with no other context. The judge returns one of three outcomes:
   - `ALLOW` → gate clears (`layer=passthrough`).
   - `DENY: <reason>` → immediate DENY with `layer=L2_asimov` and the judge's reason.
   - Judge error (unreachable, timeout, unparseable response) → **fail-closed**: DENY with `layer=gate_error`, distinguished from a rule-based denial so the caller can map it to an internal-error condition rather than "this request was judged unsafe." If `fail_open=true`, this case ALLOWs instead (`layer=passthrough`), with a distinct audit event marking the fallback.
3. **No-op variant (`mode=noop`).** Always ALLOWs (`layer=passthrough`) and never calls the judge — for dev/test deployments or operators running their own external safety layer. It still emits an explicit "gate skipped" event, so its use is visible in the audit trail rather than indistinguishable from a real ALLOW.

### 2.3 Contract (all Firewall implementations)
- Never raise on gate failure — a judge error is a DENY (or ALLOW, if fail-open), not an exception.
- Exceptions raised by the gated call itself propagate to the caller; the gate does not swallow them.
- Emit an event at every phase boundary, including the "no real decision was made" case.

---

## 3. Non-Goals

- **Not a network firewall or process sandbox.** `h-asimov` decides *whether* a command runs; it has no opinion on *how* it runs or what it can reach once allowed — that's `h-openshell`'s job.
- **Not a source of conversational memory.** The judge is intentionally stateless and single-turn; it does not consult `h-memory` or `h-recall`, and multi-turn manipulation of the judge is out of scope for this layer by design, not oversight.
- **Not a replacement for operator-owned safety layers.** The no-op variant exists precisely so operators who already gate elsewhere aren't forced to pay for a second, redundant judge call.
- **Not responsible for what happens after ALLOW.** Once the gate clears, `h-asimov` awaits and returns the gated call's result; it does not retry, rate-limit, or otherwise supervise execution.

---

## 4. System Integration & Ecosystem Fit

```
                                  +-------------------+
                                  |    h-asimov       |
                                  |  (this module)    |
                                  +---------+---------+
                                            |
                                gates entry to execution
                                            |
                                            v
+------------------+             +--------------------+             +------------------+
|   h-openshell    | <---------> |  h-orchestrator    | <---------> |    h-memory      |
| (Sandbox Client) |             |  (Chat Composites) |             |  (Hot Memory)    |
+------------------+             +---------+----------+             +--------+---------+
                                           |                                 |
                                           v                                 v
                                 +--------------------+             +------------------+
                                 |    h-recall        |             |   Redis Hot ZSET |
                                 | (Semantic Memory)  |             +------------------+
                                 +--------------------+
```

- **`h-orchestrator`**: the expected primary caller — chat-cycle composites route the command a coding-agent CLI is about to run through `h_asimov_gate` before dispatching it to `h-openshell`.
- **`h-openshell`**: the typical thing being gated; `h-asimov` has no direct dependency on it, only on the `execute` callback passed in by whoever calls the gate.
- **`h-memory` / `h-recall`**: no integration — deliberately, since the judge's statelessness is a safety property, not a gap.

---

## 5. Architectural Quality Attributes

- **Fail-closed default**: judge errors DENY unless an operator explicitly opts into fail-open. Safety over availability is the default trade-off.
- **Auditability**: every phase boundary (denylist block, judge allow, judge deny, judge error, gate skipped) emits a structured event — including the "nothing happened" cases.
- **Low latency on the common unsafe case**: the denylist short-circuits before any LLM round trip is attempted.
- **Backend portability**: the judge is invoked through NAT's LLM abstraction (`llm_name`), not a hardcoded HTTP client, so any NAT-registered LLM backend works.
- **Testability**: the two-layer evaluator and both `Firewall` implementations are constructed via dependency injection, so the judge and denylist can be swapped for fakes in tests without a network dependency.
- **Non-raising contract**: gate failures are always a typed `Decision`, never an exception — callers get a uniform shape to branch on regardless of failure mode.
