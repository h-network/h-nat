# End-to-End Fill & Search Example — `h-recall`

This example demonstrates the complete **`h-recall`** lifecycle end-to-end against a live Redis Stack instance:

```text
1. Plant Turns (h-memory)  ──>  <pod>:<agent>:chat:<id>:<ts_ns> (Hot STRING)
                                <pod>:<agent>:chat-index:<id>   (Hot ZSET)
                                       │
2. Sweep Migration         ──>  h_semantic_sweep
                                (Moves hot turns -> RedisJSON with sentinel [0.0]*384)
                                       │
3. Batch Vectorization     ──>  h_semantic_vectorize
                                (FastEmbed MiniLM-L6-v2 384d -> replaces sentinel)
                                       │
4. Hybrid Recall Search    ──>  h_semantic_search
                                (Reciprocal Rank Fusion over BM25 text + KNN vector)
```

Every NAT operation (`sweep`, `vectorize`, `search`) runs as an independent NAT function invocation via `nat run`.

---

## Prerequisites

1. **Python 3.11+** with `nvidia-nat`, `h-recall`, and `h-memory` installed in your environment:
   ```bash
   pip install -e external/h-memory
   pip install -e external/h-recall
   ```

2. **Redis Stack** with `RediSearch` and `RedisJSON` modules loaded:
   ```bash
   export H_NAT_REDIS_URL=redis://YOUR_REDIS_STACK_HOST:6379   # or your Redis Stack URL
   ```

---

## Running the Demo

Run the automated driver script:

```bash
python examples/h-recall/fill-and-search/run_demo.py
```

### What the Driver Does

1. **Generates Session Nonce**: Creates a unique session identifier `recall-demo-<nonce>` so every run is completely isolated.
2. **Step 1 — Plant Turns**: Writes 4 distinct facts (8 conversational turns: 4 user + 4 assistant) into `h-memory` hot storage.
3. **Step 2 — Sweep Migration**: Invokes `h_semantic_sweep` via `nat run` with `migration_threshold_sec=1`. Verifies:
   - Hot turns are removed from the hot tier and `chat-index` ZSET.
   - Audit documents are created under `<pod>:<agent>:chat-audit:<chat_id>:<ts_ns>` with sentinel `[0.0]*384` embeddings and `pending_vectorize="1"`.
4. **Step 3 — Vectorization**: Invokes `h_semantic_vectorize` via `nat run`. Verifies:
   - FastEmbed MiniLM-L6-v2 produces real 384-dimensional dense vectors.
   - Real vectors replace the sentinel in `$.embedding`, and `pending_vectorize` is cleared.
5. **Step 4 — Hybrid Retrieval**: Invokes `h_semantic_search` via `nat run` for 4 distinct queries targeting each planted fact, asserting that the target document ranks at position 0 for all queries.

---

## Workflow Configurations

- [`workflow.yaml`](workflow.yaml): Composite workflow declaring all three functions (`sweep`, `vectorize`, `search`).
- [`sweep.yaml`](sweep.yaml): Dedicated workflow config for `_type: h_semantic_sweep`.
- [`vectorize.yaml`](vectorize.yaml): Dedicated workflow config for `_type: h_semantic_vectorize`.
- [`search.yaml`](search.yaml): Dedicated workflow config for `_type: h_semantic_search`.

To validate the configurations with the NAT CLI:

```bash
nat validate --config_file examples/h-recall/fill-and-search/workflow.yaml
```

---

## Example Transcript

```text
======================================================================
  h-recall End-to-End Fill & Search Demonstration
======================================================================
Target Redis URL: redis://YOUR_REDIS_STACK_HOST:6379
Pod: recall_demo | Agent: assistant

[Generated Test Session] chat_id = recall-demo-1a7480a1

--- [Step 1/4] Planting 4 conversation turns (8 turns total) into hot memory ---
  + Planted hot key: recall_demo:assistant:chat:recall-demo-1a7480a1:1788052335952708155
  + Planted hot key: recall_demo:assistant:chat:recall-demo-1a7480a1:1788052335954578300
  + Planted hot key: recall_demo:assistant:chat:recall-demo-1a7480a1:1788052335955068316
  + Planted hot key: recall_demo:assistant:chat:recall-demo-1a7480a1:1788052335955437085
  + Planted hot key: recall_demo:assistant:chat:recall-demo-1a7480a1:1788052335955848654
  + Planted hot key: recall_demo:assistant:chat:recall-demo-1a7480a1:1788052335956190623
  + Planted hot key: recall_demo:assistant:chat:recall-demo-1a7480a1:1788052335956543182
  + Planted hot key: recall_demo:assistant:chat:recall-demo-1a7480a1:1788052335956923122
  -> Hot ZSET index count: 8 records

  (Waiting 1.2s for turns to satisfy migration threshold)...

--- [Step 2/4] Executing h_semantic_sweep migration ---
  -> Sweep result: {'migrated': 8, 'skipped_existing': 0, 'skipped_fresh': 0, 'scanned': 8}
  -> Remaining hot ZSET count after sweep: 0 (expected 0)
  -> Verified audit doc: sentinel embedding [0.0]*384 and pending_vectorize='1' present

--- [Step 3/4] Executing h_semantic_vectorize batch embedding ---
  -> Vectorize result: {'vectorized': 8, 'scanned': 8, 'batches': 1}
  -> Verified audit doc: real 384d vector embedded, pending flag cleared

--- [Step 4/4] Executing hybrid retrieval queries via h_semantic_search ---

  [Query 1/4]: What kind of cryptography is used in Project Nova-1a7480a1?
    Rank 0 [assistant]: "Acknowledged that Project Nova-1a7480a1 uses lattice cryptography."

  [Query 2/4]: When is the database failover cluster maintenance scheduled?
    Rank 0 [user]: "The production database failover cluster is scheduled for maintenance every Tuesday at 04:00 UTC."

  [Query 3/4]: Where is our secondary disaster recovery site?
    Rank 0 [user]: "Our secondary disaster recovery datacenter is located in Reykjavik, Iceland (Facility ICE-1a7480a1)."

  [Query 4/4]: Who is the lead engineer for distributed storage?
    Rank 0 [user]: "The lead engineer for the distributed storage engine is Dr. Elena Rostova."

======================================================================
  PASS: End-to-end h-recall pipeline verified successfully!
  Planted: 8 turns | Migrated: 8 | Vectorized: 8 | 4/4 Queries Accurate
  Session ID: recall-demo-1a7480a1
======================================================================
```
