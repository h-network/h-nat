# h-orchestrator benchmark results

## Authoritative benchmark results

Status: **NOT YET CAPTURED**

Only the benchmark operator should replace this template using a production-like
test run. Do not copy development-machine measurements into this section.

### Environment

| Field | Value |
| --- | --- |
| Capture time (UTC) | pending |
| NAT / h-orchestrator versions | pending |
| LLM model | pending |
| Redis version / topology | pending |
| MCP server version / transport | pending |
| Host OS / CPU / memory | pending |
| Workload knobs | pending |

Endpoint hosts and credentials must remain redacted.

### Executive summary

| Scenario | Status | Primary metric | Key finding / breaking point |
| --- | --- | --- | --- |
| Chat-cycle growth | pending | turns/s, early/late p95, ms/prior-record slope | pending |
| Gated MCP overhead | pending | gated-minus-direct p50/p95 | pending |
| Slow MCP | pending | timeout/failure distribution | pending |
| Malformed MCP | pending | classified failure distribution | pending |
| Hidden-tool bypass | pending | live resolution rejected | pending |

### Detailed measurements

Paste the driver-generated metric blocks here, then document the first scale
or fault point at which latency, throughput, or correctness becomes
unacceptable.

### Operational recommendations

Pending lab evidence.

## Development sanity check — non-authoritative

Status: **HARNESS-ONLY; NOT AUTHORITATIVE BENCHMARK PROOF**

Record only enough information here to show that parsing, preflight, and result
rendering work during development. Do not use these numbers for capacity,
security, or release claims.

No development measurements are committed in this template.
