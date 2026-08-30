# Standalone `h_asimov_gate` example

This example runs `h_asimov_gate` on its own — as the workflow's entry point,
not wrapped inside another tool — so you can see exactly what a caller gets
back for each verdict shape, against a real LLM judge.

## Prerequisites

Install `h-asimov`:

```bash
pip install -e external/h-asimov
```

Point at an OpenAI-compatible endpoint (this example was verified against a
self-hosted vLLM instance; any OpenAI-compatible endpoint works):

```bash
export H_NAT_LLM_MODEL=nemotron-lightning
export H_NAT_LLM_BASE_URL=http://your-openai-compatible-endpoint/v1
export OPENAI_API_KEY=EMPTY   # or a real key if your endpoint requires one
```

Never hardcode the endpoint in the workflow YAML — it's deployment
configuration, read from the environment (`${H_NAT_LLM_MODEL}` /
`${H_NAT_LLM_BASE_URL}` / `${OPENAI_API_KEY}` in `workflow.yaml`).

## Workflow YAML shape

`workflow.yaml` makes `h_asimov_gate` itself the top-level `workflow:` entry
point — no `functions:` section, since there's nothing else to compose here
(compare to `external/h-orchestrator/examples/gated-ssh/workflow.yaml`, where
a different tool references a `h_asimov_gate` instance by name via a
`gate_fn: FunctionRef` field):

```yaml
llms:
  judge_llm:
    _type: openai
    model_name: ${H_NAT_LLM_MODEL}
    base_url: ${H_NAT_LLM_BASE_URL}
    api_key: ${OPENAI_API_KEY}
    temperature: 0.0

workflow:
  _type: h_asimov_gate
  llm_name: judge_llm
  fail_open: false
  ground_rules_inline: |
    Allow read-only, informational requests that do not change system state...
    Deny anything destructive or irreversible...
```

`ground_rules_inline` is a plain multi-line YAML string — the entire ground
rules document the judge sees for every call, with no conversation history.
Use `ground_rules: path/to/file.md` instead for a longer or reusable policy
document (see `src/nat/plugins/h_asimov/defaults/groundRules.default.md` for
the packaged default, written for OpenShell sandbox actions — write your own
for a different domain rather than reusing it verbatim; see LLD.md for why).

## `mode: asimov` vs `mode: noop`

`workflow.yaml` above uses the default `mode: asimov` (denylist + LLM judge).
`noop.yaml` in this directory shows the audited opt-out:

```yaml
workflow:
  _type: h_asimov_gate
  mode: noop
```

In `mode: noop`, no LLM is called and no `llm_name`/`ground_rules` are
required — every call ALLOWs immediately. It still emits a `gate_skipped`
event (visible in the logs below) so the absence of real gating is an
observable fact in the audit trail, not silent. Use this for dev/test
deployments or when an operator runs their own external safety layer.

## `fail_open` behavior

`fail_open: false` (the default, used in `workflow.yaml`) is fail-**closed**:
if the judge call itself fails (unreachable endpoint, timeout, unparseable
response), the verdict is `DENY` with `layer: gate_error` — distinct from a
judge-produced `DENY` (`layer: L2_asimov`), so a caller can tell "judged
unsafe" apart from "the gate itself broke," but either way nothing executes.
Case 3 below demonstrates this directly by pointing the judge at an
unreachable address. Set `fail_open: true` only if you want a broken judge to
ALLOW through instead — not recommended for anything touching real
infrastructure.

## Running the demo

```bash
python external/h-asimov/examples/standalone-gate/run_demo.py
```

The driver runs three cases through `nat run` and asserts each verdict:

1. **ALLOW** — a benign, read-only command.
2. **DENY** — a clearly destructive command, judged unsafe by the LLM.
3. **fail-closed DENY** — the same benign command from case 1, but with the
   judge endpoint temporarily pointed at an unreachable address, showing
   `layer: gate_error` and `fail_open: false` denying rather than silently
   allowing through.

You can also invoke `nat run` directly:

```bash
nat validate --config_file external/h-asimov/examples/standalone-gate/workflow.yaml

nat run --config_file external/h-asimov/examples/standalone-gate/workflow.yaml \
  --input '{"command": "list the files in the current directory"}'

nat run --config_file external/h-asimov/examples/standalone-gate/noop.yaml \
  --input '{"command": "delete every file on the system permanently with rm -rf / --no-preserve-root"}'
```

## Example transcript

Verified against a live self-hosted vLLM endpoint
(`nemotron-lightning`, OpenAI-compatible, `temperature: 0.0`):

```text
======================================================================
  h-asimov standalone h_asimov_gate demonstration
======================================================================
Judge model: nemotron-lightning @ http://172.16.0.11:8000/v1

--- Case 1/3: ALLOW (benign, read-only command) ---
  command: 'list the files in the current directory'
  decision: {"verdict": "ALLOW", "layer": "passthrough", "reason": null}

--- Case 2/3: DENY (clearly out-of-policy, judged by the LLM) ---
  command: 'delete every file on the system permanently with rm -rf / --no-preserve-root'
  decision: {"verdict": "DENY", "layer": "L2_asimov", "reason": "Permanently deletes all system files, violating the prohibition on destructive and irreversible actions."}

--- Case 3/3: fail-closed DENY (judge unreachable, workflow.yaml has fail_open: false) ---
  command: 'list the files in the current directory'
  decision: {"verdict": "DENY", "layer": "gate_error", "reason": "raised: OpenAIConnectionError: Connection error."}

======================================================================
  PASS: all three verdict shapes observed against a real LLM endpoint.
======================================================================
```

And `noop.yaml`, run directly via `nat run` (no LLM call at all — note the
`gate_skipped` audit event even though the command is destructive):

```text
$ nat run --config_file external/h-asimov/examples/standalone-gate/noop.yaml \
    --input '{"command": "delete every file on the system permanently with rm -rf / --no-preserve-root"}'
...
INFO - nat.plugins.h_asimov.register - h_asimov_gate event=gate_skipped data={'reason': 'firewall=noop'}
...
Workflow Result:
{"verdict":"ALLOW","layer":"passthrough","reason":null}
```
