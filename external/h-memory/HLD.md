# High-Level Design (HLD) — `h-memory`

## 1. Executive Summary & Purpose

`h-memory` is a high-performance, lightweight, bounded conversation memory plugin for the NeMo Agent Toolkit (NAT) multi-agent runtime. It provides the **operational scratchpad** (hot tier) for conversational agents, capturing recent dialogue turns with deterministic resource bounds, sub-second ordering, and tenant isolation.

### 1.1 Core Objectives
- **Recency Buffer**: Maintains immediate dialogue context (what the user and agent just exchanged) for prompt assembly and conversational continuity.
- **Predictable Resource Bounds**: Prevents unbounded memory growth through dual time-based TTL expiration and count-based rank trimming.
- **Lightweight & Dependency-Free**: Operates strictly on vanilla Redis 7.x core primitives (`STRING` and `ZSET`). It intentionally requires no Redis Stack modules (such as RedisJSON or RediSearch) and no vector embeddings.
- **Multi-Tenant Isolation**: Implements the ADR-012 `<pod>:<agent>` tenant primitive to ensure strict separation across pods, agents, and dialogue threads.
- **Modularity & Decoupled Architecture**: Per ADR-010, long-term semantic memory, vector indexing, and hybrid search are separated into the sibling module `h-recall`. `h-memory` focuses purely on low-latency hot-tier storage and index management.

---

## 2. Architecture & Public Interface

`h-memory` exposes two primary integration surfaces: discoverable NAT workflow functions and a reusable Python asynchronous library store.

```
+-------------------------------------------------------------------------+
|                        NeMo Agent Toolkit (NAT)                         |
|                                                                         |
|  +-----------------------------+       +-----------------------------+  |
|  |     h_memory_write_turn     |       |     h_memory_delete_chat    |  |
|  |     (NAT Function Verb)     |       |     (NAT Function Verb)     |  |
|  +--------------+--------------+       +--------------+--------------+  |
|                 |                                     |                 |
|                 +------------------+------------------+                 |
|                                    |                                    |
|                                    v                                    |
|                       +--------------------------+                      |
|                       |    BoundedBufferStore    |                      |
|                       |      (Python Core)       |                      |
|                       +-------------+------------+                      |
+-------------------------------------|-----------------------------------+
                                      |
                                      v
+-------------------------------------------------------------------------+
|                            Redis 7.x Substrate                          |
|                                                                         |
|  +-------------------------------------------------------------------+  |
|  | Turn Data Key (STRING)                                            |  |
|  | `<pod>:<agent>:chat:<chat_id>:<ts_ns>`                            |  |
|  | Payload: {"role": "...", "content": "...", "ts": <unix_sec>}      |  |
|  | TTL: EX <ttl_seconds>                                             |  |
|  +-------------------------------------------------------------------+  |
|                                                                         |
|  +-------------------------------------------------------------------+  |
|  | Chat Index (ZSET)                                                 |  |
|  | `<pod>:<agent>:chat-index:<chat_id>`                               |  |
|  | Score: <ts_ns>  |  Member: `<pod>:<agent>:chat:<chat_id>:<ts_ns>`  |  |
|  | TTL: EXPIRE <ttl_seconds_max> (refreshed per write)               |  |
|  +-------------------------------------------------------------------+  |
+-------------------------------------------------------------------------+
```

### 2.1 NAT Function: `h_memory_write_turn`
Appends a single conversation turn to the hot tier within a single Redis pipeline execution.

- **Workflow Configuration (`HMemoryWriteTurnConfig`)**:
  - `redis_url` (*str*, default reads `H_NAT_REDIS_URL` -> `REDIS_URL` -> `"redis://localhost:6379"`): Redis connection endpoint.
  - `pod` (*str*, required): First segment of ADR-012 tenant primitive (matching `^[A-Za-z0-9_-]+$`).
  - `agent` (*str*, required): Second segment of ADR-012 tenant primitive (matching `^[A-Za-z0-9_-]+$`).
  - `ttl_seconds_max` (*int*, default `2592000` / 30 days): Operator-defined upper bound for turn TTL and index retention.
  - `hot_keep_count` (*Optional[int]*, default `None`): Optional count cap defining the maximum number of recent turns retained in the chat index.
- **Invocation Input (`WriteTurnInput`)**:
  - `chat_id` (*str*, required): Conversation or session identifier.
  - `role` (*str*, required): Message author role (`"user"`, `"assistant"`, etc.).
  - `content` (*str*, required): Text payload of the turn.
  - `ttl_seconds` (*int*, required): Per-call requested retention TTL (validated against `1 <= ttl_seconds <= ttl_seconds_max`).
  - `hot_keep_count` (*Optional[int]*, optional): Per-call override for the count cap.
- **Output**: Returns the full Redis turn key written (e.g., `example-pod:example-agent:chat:session-42:1778572465841249202`).

### 2.2 NAT Function: `h_memory_delete_chat`
Purges all active hot-tier state for a given conversation.

- **Workflow Configuration (`HMemoryDeleteChatConfig`)**:
  - `redis_url` (*str*, default reads `H_NAT_REDIS_URL` -> `REDIS_URL` -> `"redis://localhost:6379"`): Redis connection endpoint.
  - `pod` (*str*, required): First segment of ADR-012 tenant primitive.
  - `agent` (*str*, required): Second segment of ADR-012 tenant primitive.
- **Invocation Input (`DeleteChatInput`)**:
  - `chat_id` (*str*, required): Identifier of the chat to delete.
- **Output**: Returns an integer count of Redis keys deleted (live turn data keys plus the index key).

### 2.3 Python Library API: `BoundedBufferStore`
The underlying Python class that encapsulates all Redis interactions and key derivations. Directly importable by harnesses, scripts, and composite verbs without running full NAT CLI pipelines.

---

## 3. Storage Architecture & Keyspace Contract

The keyspace strictly follows the [ADR-012 Redis naming contract](../../docs/adrs/ADR-012-redis-naming-contract.md).

### 3.1 Key Schema
| Key Pattern | Redis Type | Structure / Value | Lifecycle & TTL |
| :--- | :--- | :--- | :--- |
| `<pod>:<agent>:chat:<chat_id>:<ts_ns>` | `STRING` | JSON payload: `{"role": str, "content": str, "ts": int}` | `EX <ttl_seconds>` per-turn expiration |
| `<pod>:<agent>:chat-index:<chat_id>` | `ZSET` | `score = ts_ns`, `member = turn_key` | `EXPIRE <ttl_seconds_max>` refreshed on every write |

### 3.2 Dual Eviction Model
1. **Time-Based Eviction**:
   - Turn keys expire automatically via Redis key-level `EX`.
   - On every write, `ZREMRANGEBYSCORE index_key 0 (now_ns - ttl_seconds_max * 1e9)` prunes index entries that have exceeded the maximum possible retention window.
   - Quiescent chats (no writes for `ttl_seconds_max`) automatically evict completely when the index key expires.
2. **Count-Based Eviction (`hot_keep_count`)**:
   - When configured, `ZREMRANGEBYRANK index_key 0 -(hot_keep_count + 1)` trims the index to retain only the top $N$ most recent entries.
   - Orphaned data keys removed from the index remain in Redis until their individual time TTLs expire, avoiding expensive bulk deletes during writes.

---

## 4. System Integration & Ecosystem Fit

`h-memory` is one of five specialized modules within the `h-nat` suite:

```
                                  +-------------------+
                                  |    h-asimov       |
                                  |  (Safety Gate)    |
                                  +---------+---------+
                                            |
                                            v
+------------------+             +--------------------+             +------------------+
|   h-openshell    | <---------> |  h-orchestrator    | <---------> |    h-memory      |
| (Sandbox Client) |             |  (Chat Composites) |             |  (Hot Memory)    |
+------------------+             +---------+----------+             +--------+---------+
                                           |                                 |
                                           | (Recency Reads & Search)        | (Shared Index)
                                           v                                 v
                                 +--------------------+             +------------------+
                                 |    h-recall        | <-----------+   Redis Hot ZSET |
                                 | (Semantic Memory)  |             +------------------+
                                 +--------------------+
```

### 4.1 Integration Points
- **`h-orchestrator`**: Chat cycle composites (such as `h_chat_cycle` and `h_claude_cycle`) read dialogue history from the hot tier, assemble context prompts, invoke backend LLMs, and record new user/assistant turns via `BoundedBufferStore`.
- **`h-recall` (Semantic Memory)**: Per ADR-010, `h-memory` owns write-side indexing, while `h-recall` consumes the shared `<pod>:<agent>:chat-index:<chat_id>` ZSET index to execute recency reads (`ZREVRANGE` + `MGET`), perform hybrid search, and migrate older history into vector storage.
- **`h-asimov` & `h-openshell`**: Complementary runtime modules for safety filtering and containerized tool execution.

---

## 5. Architectural Quality Attributes

- **Performance**: Single-roundtrip async Redis pipeline for all write operations.
- **Concurrency & Ordering**: Monotonic timestamp guard (`_next_ts_ns`) ensures unique, strictly increasing nanosecond identifiers even during high-frequency same-process writes.
- **Fail-Fast Reliability**: Eager connection verification (`ping()`) during NAT workflow build prevents runtime misconfigurations.
- **Security & Multi-Tenancy**: Strict separation using validated token segments (`<pod>` and `<agent>`) prevents cross-tenant data leakage.
