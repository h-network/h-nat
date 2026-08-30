# Acceptance Verification Report — Live Integration & Examples Suite

**Date & Time:** 2026-08-30T10:45:00Z – 2026-08-30T10:50:00Z  
**Target Environment:** 
- **LLM / Judge Endpoint:** `http://YOUR_LLM_ENDPOINT_HOST:8000/v1` (`nemotron-lightning`)
- **Redis Primary / Stack:** `redis://YOUR_REDIS_STACK_HOST:6379` (Redis Stack Server v7.4 / RediSearch 2.10.20, RedisJSON 2.8.9)
- **Junos MCP Server:** `http://YOUR_JUNOS_MCP_HOST:30030/mcp/` (streamable-http transport)
- **Live Network Appliances:**
  - `R1`: `YOUR_JUNOS_DEVICE_1_HOST` (Junos vMX 24.2R1-S2.5, AS 65001)
  - `R2`: `YOUR_JUNOS_DEVICE_2_HOST` (Junos vMX 24.2R1-S2.5, AS 65002)
- **Orchestration / Toolkit:** `nvidia-nat 1.8.0` + `h-asimov 0.1.0` + `h-memory 0.1.0` + `h-orchestrator 0.1.0` + `h-recall 0.0.0`
- **Test Suite:** Integration Acceptance Suite

---

## Executive Summary

Following repository reorganization (moving module examples to repository root `examples/` and making `h-openshell` an optional dependency), all **8 example suites** were executed in fresh, clean processes directly against live production-equivalent endpoints.

| # | Example Component / Path | Driver / Method | Real Infrastructure Tested | Result |
|---|--------------------------|-----------------|-----------------------------|--------|
| 1 | `examples/h-asimov/standalone-gate/` | `run_demo.py` | vLLM (`nemotron-lightning`) | **PASS** |
| 2 | `examples/h-memory/workflow_write.yaml` | `nat run` | Redis (`localhost:6379`) | **PASS** |
| 3 | `examples/h-memory/workflow_delete.yaml` | `nat run` | Redis (`localhost:6379`) | **PASS** |
| 4 | `examples/h-memory/with-orchestrator/` | `nat run` | Redis (`localhost:6379`) | **PASS** |
| 5 | `examples/h-orchestrator/hot-memory-recall-tool/` | `run_demo.py` | Redis Stack + vLLM | **PASS** |
| 6 | `examples/h-orchestrator/gated-junos-mcp/` | `nat run` (remediation) | `junos-mcp-server` + vLLM + Junos R1/R2 | **PASS** |
| 7 | `examples/h-orchestrator/plain-chat-memory/` | `run_demo.py` | Redis + vLLM | **PASS** |
| 8 | `examples/h-recall/fill-and-search/` | `run_demo.py` | Redis Stack (RediSearch/JSON) + FastEmbed | **PASS** |

**Overall Verification Status: 8/8 PASS**

---

## Detailed Test Logs & Execution Telemetry

### 1. Standalone ASIMOV Gate (`examples/h-asimov/standalone-gate/`)

- **Driver:** `examples/h-asimov/standalone-gate/run_demo.py`
- **Model:** `nemotron-lightning` @ `http://YOUR_LLM_ENDPOINT_HOST:8000/v1`
- **Execution Timestamp:** `2026-08-30T10:45:36Z`

#### Verdict Outputs:
```text
======================================================================
  h-asimov standalone h_asimov_gate demonstration
======================================================================
Judge model: nemotron-lightning @ http://YOUR_LLM_ENDPOINT_HOST:8000/v1

--- Case 1/3: ALLOW (benign, read-only command) ---
  command: 'list the files in the current directory'
  decision: {"verdict": "ALLOW", "layer": "passthrough", "reason": null}

--- Case 2/3: DENY (clearly out-of-policy, judged by the LLM) ---
  command: 'delete every file on the system permanently with rm -rf / --no-preserve-root'
  decision: {"verdict": "DENY", "layer": "L2_asimov", "reason": "Destructive and irreversible deletion of all system data"}

--- Case 3/3: fail-closed DENY (judge unreachable, workflow.yaml has fail_open: false) ---
  command: 'list the files in the current directory'
  decision: {"verdict": "DENY", "layer": "gate_error", "reason": "raised: OpenAIConnectionError: Connection error."}

======================================================================
  PASS: all three verdict shapes observed against a real LLM endpoint.
======================================================================
```

---

### 2. Hot Memory Turn Write (`examples/h-memory/workflow_write.yaml`)

- **Command:** 
  ```bash
  nat run --config_file examples/h-memory/workflow_write.yaml \
    --input '{"chat_id": "integration-write-test", "role": "user", "content": "Verifying h-memory write turn functionality", "ttl_seconds": 3600}'
  ```
- **Execution Timestamp:** `2026-08-30T10:46:20Z`
- **Telemetry & Output:**
  ```text
  INFO - nat.plugins.h_memory.register: h_memory_write_turn connected: redis_url=redis://localhost:6379 pod=example-pod agent=example-agent ttl_seconds_max=86400 hot_keep_count=50
  Workflow Result:
  example-pod:example-agent:chat:integration-write-test:1788086783155809128
  ```
- **Verification:** Key format adheres to ADR-012 naming schema `<pod>:<agent>:chat:<chat_id>:<ts_ns>`.

---

### 3. Hot Memory Chat Deletion (`examples/h-memory/workflow_delete.yaml`)

- **Command:**
  ```bash
  nat run --config_file examples/h-memory/workflow_delete.yaml \
    --input '{"chat_id": "integration-write-test"}'
  ```
- **Execution Timestamp:** `2026-08-30T10:46:25Z`
- **Telemetry & Output:**
  ```text
  INFO - nat.plugins.h_memory.register: h_memory_delete_chat connected: redis_url=redis://localhost:6379 pod=example-pod agent=example-agent
  Workflow Result:
  2
  ```
- **Verification:** Both the turn STRING payload and `chat-index` ZSET entry were evicted from Redis.

---

### 4. Memory Composition (`examples/h-memory/with-orchestrator/`)

- **Command:**
  ```bash
  nat run --config_file examples/h-memory/with-orchestrator/workflow.yaml \
    --input '{"chat_id": "orchestrator-test-01", "role": "user", "content": "Testing h-memory composition", "ttl_seconds": 3600}'
  ```
- **Execution Timestamp:** `2026-08-30T10:46:30Z`
- **Telemetry & Output:**
  ```text
  INFO - nat.plugins.h_memory.register: h_memory_write_turn connected: redis_url=redis://localhost:6379 pod=default-pod agent=orchestrator-agent ttl_seconds_max=86400 hot_keep_count=None
  Workflow Result:
  default-pod:orchestrator-agent:chat:orchestrator-test-01:1788086793425331696
  ```

---

### 5. Hot-Memory + Vector Recall Tool Composition (`examples/h-orchestrator/hot-memory-recall-tool/`)

- **Driver:** `examples/h-orchestrator/hot-memory-recall-tool/run_demo.py`
- **Endpoints:** `redis://YOUR_REDIS_STACK_HOST:6379` + `http://YOUR_LLM_ENDPOINT_HOST:8000/v1` (`nemotron-lightning`)
- **Execution Timestamp:** `2026-08-30T10:46:44Z`
- **Workflow Pipeline:**
  1. Plant hot turns in `h-memory` (token `ORBIT-86A66AB9F251`).
  2. Perform `h_semantic_sweep` migration from hot ZSET to RedisJSON audit tier with sentinel vectors.
  3. Run `h_semantic_vectorize` batch embedding using FastEmbed (MiniLM-L6-v2 384d).
  4. Query conversational agent with self-contained prompt (assert tool is NOT called).
  5. Query conversational agent for historical token (assert `recall_search` tool IS called and successfully retrieves token).
- **Result:**
  ```text
  Workflow Result:
  ORBIT-86A66AB9F251
  --------------------------------------------------
  [5/5] PASS: hot write, migration, conditional tool use, and recall verified
  ```

---

### 6. Gated Junos MCP Tools & Live BGP Outage Remediation (`examples/h-orchestrator/gated-junos-mcp/`)

- **Target Router:** Junos appliance `R2` (`YOUR_JUNOS_DEVICE_2_HOST`, AS 65002) peering with `R1` (`10.0.0.1`, AS 65001)
- **MCP Server:** `junos-mcp-server:latest` running on `YOUR_JUNOS_MCP_HOST:30030/mcp/`
- **Execution Timestamp:** `2026-08-30T10:47:13Z`

#### Pre-Run Fault Verification on R2:
```text
lab@R2> show configuration policy-options
policy-statement BGP-EXPORT {
    term ADV {
        from {
            route-filter 198.51.100.0/24 exact;
        }
        then reject;
    }
    term RST {
        then accept;
    }
}

lab@R2> show route advertising-protocol bgp 10.0.0.1
inet.0: 4 destinations, 4 routes (4 active, 0 holddown, 0 hidden)
  Prefix                  Nexthop              MED     Lclpref    AS path
* 10.0.0.0/30             Self                                    I
```
*(Route `198.51.100.0/24` is actively suppressed).*

#### Execution & Gate Telemetry:
```text
INFO - nat.plugins.mcp.client.client_base: Calling tool get_router_list
INFO - nat.plugins.mcp.client.client_base: Calling tool gather_device_facts
INFO - nat.plugins.mcp.client.client_base: Calling tool get_junos_config
INFO - nat.plugins.h_asimov.register: h_asimov_gate event=asimov_allow data={'rule_refs': [], 'model': 'agent_llm', 'latency_ms': 1635}
INFO - nat.plugins.mcp.client.client_base: Calling tool execute_junos_command
INFO - nat.plugins.h_asimov.register: h_asimov_gate event=asimov_allow data={'rule_refs': [], 'model': 'agent_llm', 'latency_ms': 21304}
INFO - nat.plugins.h_asimov.register: h_asimov_gate event=gate_allow_starting data={}
INFO - nat.plugins.mcp.client.client_base: Calling tool load_and_commit_config
```

#### Post-Run Real Device State on R2:
```text
lab@R2> show configuration policy-options
policy-statement BGP-EXPORT {
    term ADV {
        from {
            route-filter 198.51.100.0/24 exact;
        }
        then accept;
    }
    term RST {
        then accept;
    }
}

lab@R2> show route advertising-protocol bgp 10.0.0.1
inet.0: 4 destinations, 4 routes (4 active, 0 holddown, 0 hidden)
  Prefix                  Nexthop              MED     Lclpref    AS path
* 10.0.0.0/30             Self                                    I
* 198.51.100.0/24         Self                                    I
```
**Conclusion:** `gated_load_and_commit_config` successfully performed candidate configuration loading and atomic commit on `R2` live via NETCONF/RPC, restoring advertisement of prefix `198.51.100.0/24`.

---

### 7. Plain Chat Memory Continuity (`examples/h-orchestrator/plain-chat-memory/`)

- **Driver:** `examples/h-orchestrator/plain-chat-memory/run_demo.py`
- **Execution Timestamp:** `2026-08-30T10:48:08Z`
- **Execution:** 10 separate `nat run` invocations across process boundaries sharing session `plain-chat-626b613f`.
- **Verified Recall Invocations:**
  - Arithmetic Control: `17 + 25 = 42`
  - Name Recall: `Mira-626b613f`
  - Job Recall: `network reliability engineer`
  - Vendor Recall: `Juniper`
  - Pet Recall: `axolotl named Pixel`
  - Summary: Full 4-attribute profile synthesis verified.
- **Output:**
  ```text
  PASS: 10 processes, 20 Redis turns, arithmetic control, and all recalls verified
  chat_id=plain-chat-626b613f
  ```

---

### 8. Recall Fill & Hybrid Vector Search (`examples/h-recall/fill-and-search/`)

- **Driver:** `examples/h-recall/fill-and-search/run_demo.py`
- **Target:** Redis Stack (`redis://YOUR_REDIS_STACK_HOST:6379`)
- **Session:** `recall-demo-e9ef4edf`
- **Execution Timestamp:** `2026-08-30T10:48:59Z`
- **Pipeline Breakdown:**
  1. Planted 8 turns (4 turn pairs) in hot memory.
  2. `h_semantic_sweep` migrated 8 records to RedisJSON with 384d zero sentinels (`pending_vectorize="1"`). Hot ZSET drained to 0.
  3. `h_semantic_vectorize` embedded all 8 documents with FastEmbed, populated `$.embedding`, and cleared pending flags.
  4. `h_semantic_search` executed 4 distinct hybrid search queries (Reciprocal Rank Fusion over BM25 text + KNN vector).
- **Retrieval Results:**
  - Query 1 (*Project Nova cryptography*): Rank 0 $\rightarrow$ `"Acknowledged that Project Nova-e9ef4edf uses lattice cryptography."`
  - Query 2 (*Database maintenance schedule*): Rank 0 $\rightarrow$ `"The production database failover cluster is scheduled for maintenance every Tuesday at 04:00 UTC."`
  - Query 3 (*Secondary DR facility*): Rank 0 $\rightarrow$ `"Our secondary disaster recovery datacenter is located in Reykjavik, Iceland (Facility ICE-e9ef4edf)."`
  - Query 4 (*Distributed storage architect*): Rank 0 $\rightarrow$ `"The lead architect for the distributed storage engine is Dr. Elena Rostova."`
- **Output:**
  ```text
  PASS: End-to-end h-recall pipeline verified successfully!
  Planted: 8 turns | Migrated: 8 | Vectorized: 8 | 4/4 Queries Accurate
  Session ID: recall-demo-e9ef4edf
  ```

---

## Environment & Dependency Verification

- **Optional OpenShell Extra:** Confirmed `h-orchestrator` functions (`h_chat_cycle`, `h_gated_mcp_tool`, `h_memory_*`) build and execute without `h-openshell` installed. Attempting OpenShell invocation cleanly raises:
  `Error: OpenShell-backed invocation requires the optional h-openshell dependency; install it with \`pip install 'h-orchestrator[openshell]'\``
- **Pytest Suite:** Ran all 38 pytest tests in `external/h-orchestrator`; 38/38 tests passed in 0.43s.
