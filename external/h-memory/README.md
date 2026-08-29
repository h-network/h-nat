# h-memory

**Team:** `memory`

NeMo Agent Toolkit (NAT) plugin for bounded per-chat conversation memory in Redis. Fully asynchronous, zero extra Redis modules, and designed for multi-agent workflows.

Long-term and semantic memory live in the sibling module [`h-recall`](../h-recall/) (per ADR-010). `h-memory` is the lightweight recency primitive: vanilla Redis 7.x, no extra modules, no embeddings.

## What's in this package

| Surface | NAT-discoverable | Purpose |
| :--- | :--- | :--- |
| `_type: h_memory_write_turn` | ✅ `@register_function` | Write one turn to the hot tier (per-call TTL, optional count cap). |
| `_type: h_memory_delete_chat` | ✅ `@register_function` | Wipe one chat's hot-tier state. |
| `BoundedBufferStore` | Python library class | Core engine managing turn writes, ZSET index, and eviction. |

The two NAT functions are the operator-facing surface: workflow YAMLs reference them as `_type: h_memory_write_turn` and `_type: h_memory_delete_chat`. `BoundedBufferStore` is exported for harnesses and composites that drive memory directly from Python.

## Layout

```
external/h-memory/
├── pyproject.toml                     # Package metadata & NAT entry points
├── requirements.txt                   # Runtime dependencies (redis>=5,<7, nvidia-nat>=1.6,<2)
├── requirements-test.txt              # Test dependencies (pytest, pytest-asyncio)
├── HLD.md                             # High-level architecture & system design
├── LLD.md                             # Canonical low-level technical specification
├── README.md                          # Quickstart and overview
├── INSTALL.md                         # Detailed installation and verification guide
├── src/nat/plugins/h_memory/          # Plugin source tree
│   ├── __init__.py
│   ├── memory.py                      # BoundedBufferStore (write_turn + delete_chat)
│   └── register.py                    # NAT function registration & Pydantic models
├── src/nat/plugins/h_network_memory/  # Backwards-compatible import bridge
├── tests/                             # Unit and integration test suite
└── examples/                          # Example workflow YAMLs
    ├── workflow_write.yaml
    ├── workflow_delete.yaml
    └── with_orchestrator/workflow.yaml
```

## Bounded Buffer Schema

Keyspace strictly conforms to [ADR-012 Redis naming contract](../../docs/adrs/ADR-012-redis-naming-contract.md):

```
<pod>:<agent>:chat:{chat_id}:{ts_ns}                    STRING (JSON)
                                                          {"role": "user"|"assistant", "content": "...", "ts": <unix_sec>}
                                                          SET ... EX <per-call ttl_seconds>

<pod>:<agent>:chat-index:{chat_id}                       ZSET
                                                          score = ts_ns
                                                          member = full turn key
                                                          EXPIRE ttl_seconds_max (refreshed per write)
```

- `<pod>:<agent>` is ADR-012's multi-tenancy primitive.
- `chat` is the scope tag for turn payloads; `chat-index` is the scope tag for the per-chat ZSET index.

## Eviction & Bounding Knobs

1. **Time-Based Eviction**:
   - `ttl_seconds_max` (config): Operator ceiling (default 30 days / 2592000s). Refreshed onto the ZSET index TTL on every write.
   - `ttl_seconds` (input): Per-turn requested lifetime, applied via `SET ... EX`.
   - On every write, `ZREMRANGEBYSCORE` automatically cleans up index entries older than the maximum retention window.
2. **Count-Based Bounding (`hot_keep_count`)**:
   - `hot_keep_count` (optional config/input override): Trims the ZSET index to the most recent $N$ entries via `ZREMRANGEBYRANK` after each write.

## Workflow YAML Examples

### Write a Turn
```yaml
workflow:
  _type: h_memory_write_turn
  redis_url: redis://localhost:6379
  pod: example-pod
  agent: example-agent
  ttl_seconds_max: 86400
  hot_keep_count: 50
```

Execute via NAT CLI:
```bash
nat run --config_file workflow.yaml \
    --input '{"chat_id": "chat-1", "role": "user", "content": "Hello", "ttl_seconds": 300}'
```

### Delete a Chat
```yaml
workflow:
  _type: h_memory_delete_chat
  redis_url: redis://localhost:6379
  pod: example-pod
  agent: example-agent
```

Execute via NAT CLI:
```bash
nat run --config_file workflow.yaml \
    --input '{"chat_id": "chat-1"}'
```

## Running Tests

```bash
pytest external/h-memory/tests -v
```
