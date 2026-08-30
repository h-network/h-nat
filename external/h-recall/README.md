# h-recall

NAT plugin: **operator-controlled long-term semantic memory and audit-tier recall**.

Three workflow-callable functions over a Redis Stack store:

- `_type: h_semantic_search` — hybrid retrieval (lexical BM25 text + dense vector KNN via RRF)
- `_type: h_semantic_sweep` — one bounded hot→audit migration pass, operator-scheduled
- `_type: h_semantic_vectorize` — embed pending docs, operator-scheduled

The operator controls the cadence. No daemons, no background loops — every operation is one bounded, idempotent NAT function call from the workflow YAML.

## Architecture

`h-recall` implements the audit/long-term tier of the h-network two-tier memory hierarchy:

- **Hot Tier (`h-memory`)**: Short-term per-chat turn buffering in core Redis (`STRING` data keys + `ZSET` chat index).
- **Audit Tier (`h-recall`)**: Long-term persistent storage in Redis Stack (`RedisJSON` docs + `RediSearch` HNSW index).

Per ADR-012, both modules use disjoint scope tags:
- `h-memory` writes to `<pod>:<agent>:chat:`
- `h-recall` writes to `<pod>:<agent>:chat-audit:`

## Workflow YAML Shape

```yaml
general:
  use_uvloop: true

functions:
  recall:
    _type: h_semantic_search
    redis_url: redis://localhost:6379
    pod: my-pod
    agent: my-agent
    rrf_k: 60
    candidate_pool_multiplier: 2

  sweep:
    _type: h_semantic_sweep
    redis_url: redis://localhost:6379       # Audit-tier Redis
    hot_redis_url: redis://localhost:6379   # Hot-tier Redis (optional; for split topology)
    pod: my-pod
    agent: my-agent
    migration_threshold_sec: 18000          # 5h — caller-config
    max_per_cycle: 0                         # 0 = unbounded

  vectorize:
    _type: h_semantic_vectorize
    redis_url: redis://localhost:6379
    pod: my-pod
    agent: my-agent
    batch_size: 64
    max_per_cycle: 0
```

### Function Invocations

- `recall`: `{"chat_id": "<id>", "query": "<text>", "top_k": 10, "mode": "hybrid"}` (`mode` $\in$ `text` | `semantic` | `hybrid`).
- `sweep`: `{"chat_ids": ["c1", "c2"]}` — operator schedules cadence; empty list `chat_ids=[]` is a deliberate no-op.
- `vectorize`: `{}` or `{"batch_size": 32, "max_per_cycle": 100}` to override config defaults.

## Runtime Dependencies

- **Redis Stack** (Redis $\ge$ 7.1 with `RediSearch` and `RedisJSON` modules loaded).
- **fastembed** + **numpy** for embeddings (default model: `sentence-transformers/all-MiniLM-L6-v2`, 384d).

## Examples

- [Fill and Search Example](../../examples/h-recall/fill-and-search/README.md) — Self-contained end-to-end demonstration planting turns with `h-memory`, sweeping with `h_semantic_sweep`, vectorizing with `h_semantic_vectorize`, and retrieving with `h_semantic_search`.

## Documentation

- [HLD.md](HLD.md) — High-Level Design, system architecture, topologies, and RRF mathematics.
- [LLD.md](LLD.md) — Low-Level Design, the 11 core invariants, class contracts, and failure recovery.
- [INSTALL.md](INSTALL.md) — Prerequisites, installation steps, and verification.
