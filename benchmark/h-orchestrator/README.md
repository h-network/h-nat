# h-orchestrator benchmark

This suite measures the two stateful/composite h-orchestrator surfaces under
real load: bounded Redis chat cycles and safety-gated MCP tools. It is an
operator benchmark, not a unit-test substitute.

## Scenarios

| Name | What it measures | Required service |
| --- | --- | --- |
| `chat` | Wall, dispatcher, and orchestration-overhead latency as prior turns grow; throughput and bounded-record invariants | Redis and an OpenAI-compatible LLM |
| `mcp` | Matched end-to-end direct-control versus `h_gated_mcp_tool` latency, throughput, error rate, and tool-output marker correctness | Healthy streamable-HTTP MCP, LLM judge/agent |
| `slow` | Timeout and failure behavior when MCP discovery or execution stalls | Deliberately slow MCP endpoint |
| `malformed` | Failure classification for invalid MCP protocol/tool responses | Deliberately malformed MCP endpoint |
| `bypass` | Schema validation succeeds, but live workflow construction must reject an agent reference to the hidden raw member | Healthy MCP endpoint |

The direct MCP workflow is a benchmark-only control. Configure
`mcp.benchmark_tool` as a harmless, read-only echo/health operation. Never use
a mutating production tool as the ungated control.

`nat validate` alone cannot prove the bypass boundary: NAT 1.8 validates the
reference shape without discovering group members. `run_bench.py` first
requires schema validation to pass, then requires real `load_workflow`
construction to reject `bypass_mcp__<hidden-tool>` after MCP discovery.

## Configure

Install the local modules plus the LangChain agent plugin, and ensure `nat` is
on `PATH`:

```bash
pip install -e external/h-asimov
pip install -e external/h-memory
pip install -e external/h-orchestrator
pip install 'nvidia-nat[langchain]>=1.8,<2'
cp benchmark/h-orchestrator/vars.example.yaml benchmark/h-orchestrator/vars.yaml
```

`vars.example.yaml` is JSON-compatible YAML so the driver can read it with the
standard library. Fill `vars.yaml`; never commit that file. Environment values
override the corresponding file fields:

- `H_NAT_LLM_MODEL`, `H_NAT_LLM_BASE_URL`, `OPENAI_API_KEY`
- `H_NAT_REDIS_URL`
- `H_NAT_BENCH_MCP_URL`, `H_NAT_BENCH_MCP_TOKEN`
- `H_NAT_BENCH_MCP_SLOW_URL`, `H_NAT_BENCH_MCP_MALFORMED_URL`
- `H_NAT_BENCH_MCP_TOOL`, `H_NAT_BENCH_MCP_PUBLIC_TOOL`
- `H_NAT_BENCH_MCP_EXPECTED_MARKER`

The healthy server must advertise two distinct tools. `benchmark_tool` is the
harmless operation measured directly and behind the gate. `public_tool` exists
only to satisfy the gated source group's non-empty public allowlist. The agent
is exposed only to the wrapper. `expected_marker` must be text emitted only by
the benchmark tool, proving the agent did not answer without calling it.

The slow and malformed endpoints must advertise the same schemas. The slow
endpoint should exceed `scenarios.timeout_seconds`; the malformed endpoint
should violate MCP framing or return an invalid tool response predictably.

## Run

```bash
# Small wiring check (still uses real services)
python benchmark/h-orchestrator/run_bench.py --quick

# Full suite
python benchmark/h-orchestrator/run_bench.py --scenarios all

# Authoritative benchmark capture (the only mode authorized to replace RESULTS.md)
python benchmark/h-orchestrator/run_bench.py --scenarios all \
  --authoritative \
  --output-results benchmark/h-orchestrator/RESULTS.md

# Selected scenarios and machine-readable stdout
python benchmark/h-orchestrator/run_bench.py \
  --scenarios chat mcp bypass --json \
  --raw-json .artifacts/orchestrator-bench.json
```

The driver keeps each healthy workflow loaded across its samples, avoiding
per-turn NAT startup distortion. Chat uses a unique ID and checks exact bounded
Redis counts. MCP records matched p50/p95 distributions; the reported gate
overhead is `gated - direct` for the same endpoint, prompt, model, and tool.
Fault samples are bounded with `asyncio.wait_for` and classified rather than
silently folded into pass/fail.

By default the driver writes both raw JSON and a prominently labeled
non-authoritative development report under ignored `.artifacts/`; it does not
touch `RESULTS.md`. Only an explicit `--authoritative` run should replace that
template. Output metadata redacts endpoint hosts and never writes the API key
or MCP bearer token.

## Interpreting breaking points

- A positive `wall_ms_per_prior_record_slope` or a materially larger late
  p95 identifies prompt/read growth before the configured hot bound.
- Rising orchestration overhead with flat dispatcher latency points to Redis
  reads, prompt assembly, or turn persistence rather than the model.
- `gate_added_p50_ms` and `gate_added_p95_ms` quantify the safety decision's
  end-to-end cost; compare error rates as well as latency.
- Any missing expected marker invalidates the MCP latency sample because the
  agent may have skipped the tool.
- A bypass build success is a security failure. A schema-validation success is
  expected and is not evidence of reachability.
- Slow/malformed results should be deterministic, bounded, and non-successful;
  record whether failure occurred during discovery, gate execution, or tool
  execution.

## Result authority

The authoritative section in `RESULTS.md` is intentionally blank until a
benchmark operator runs this suite in a production-like test environment.
Development sanity checks must remain in their separately labeled
non-authoritative section and must never be presented as authoritative proof.
