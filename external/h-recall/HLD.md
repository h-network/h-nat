# High-Level Design (HLD) — `h-recall`

**Module:** `h-recall` (`h-network-semantic-memory`)  
**Status:** Production-Grade / Architecture Baseline  
**Maintainer:** `h-network`  
**Applicable ADRs:** [ADR-001](../../docs/adrs/ADR-001-bgp-community-keyspace.md), [ADR-002](../../docs/adrs/ADR-002-sweeper-workflow-callable.md), [ADR-004](../../docs/adrs/ADR-004-redis-version-pin.md), [ADR-007](../../docs/adrs/ADR-007-naming-conventions.md), [ADR-008](../../docs/adrs/ADR-008-cross-module-version-pin.md), [ADR-009](../../docs/adrs/ADR-009-authority-and-escalation.md), [ADR-010](../../docs/adrs/ADR-010-split-semantic-memory.md), [ADR-011](../../docs/adrs/ADR-011-bgp-community-logical-separation.md), [ADR-012](../../docs/adrs/ADR-012-redis-naming-contract.md)

---

## 1. Executive Summary & Purpose

`h-recall` is the long-term semantic memory and audit-tier retrieval module for the **h-network** LLM agent ecosystem. Built as a plugin for the NVIDIA NeMo Agent Toolkit (NAT), `h-recall` provides persistent, searchable episodic memory across agent sessions and chat histories.

### Core Objectives
1. **Long-Term Recall**: Provide hybrid semantic retrieval (Reciprocal Rank Fusion over lexical BM25/full-text and dense vector KNN) across historical chat sessions.
2. **Decoupled Tiering**: Separate lightweight, short-term recency buffering (`h-network-memory` hot tier) from heavyweight vectorization and indexing (`h-recall` audit tier), ensuring agents that only need short-term recency do not incur vector store overhead (per [ADR-010](../../docs/adrs/ADR-010-split-semantic-memory.md)).
3. **Operator-Controlled Cadence**: Eliminate background daemons and continuous loops. All maintenance actions (hot-to-audit migration and vectorization) execute as bounded, idempotent, workflow-callable NAT functions scheduled directly by the operator/harness (per [ADR-002](../../docs/adrs/ADR-002-sweeper-workflow-callable.md)).
4. **Strict Multi-Tenancy**: Enforce tenant and agent isolation at the keyspace and index layers adhering to the project-wide naming contract (per [ADR-012](../../docs/adrs/ADR-012-redis-naming-contract.md)).
5. **Topology Flexibility**: Support both single-instance (colocated) Redis deployments and distributed split topologies where the hot tier resides on core Redis and the audit/vector tier resides on per-agent Redis Stack instances.

---

## 2. System Architecture & Topology

### 2.1 Two-Tier Memory Hierarchy

The h-network storage architecture divides agent conversational memory into two distinct tiers:

```
+-----------------------------------------------------------------------------+
|                               Agent Workflow                                |
|   (Harness / Orchestrator / nat run / nat serve FastAPI endpoint)           |
+-----------------------------------------------------------------------------+
         │                                              │
         │ 1. Write turn / Read recency                 │ 2. Hybrid Search / Sweep / Vectorize
         ▼                                              ▼
+─────────────────────────────────+           +─────────────────────────────────+
|        h-network-memory         |           |            h-recall             |
|          (Hot Tier)             |           |          (Audit Tier)           |
+─────────────────────────────────+           +─────────────────────────────────+
| - Storage: Core Redis (STRING)  |           | - Storage: Redis Stack (JSON)   |
| - Index: ZSET (<scope>-index)   |           | - Index: RediSearch (HNSW+Text) |
| - Retention: Short TTL (e.g. 5h)|           | - Retention: Indefinite / Audit |
| - Scope Tag: `chat`             |           | - Scope Tag: `chat-audit`       |
+─────────────────────────────────+           +─────────────────────────────────+
                 ▲                                     │
                 │              Cross-Module           │
                 └────────────── Migration ────────────┘
                              (Sweep read/delete)
```

1. **Hot Tier (`h-network-memory`)**:
   - High-throughput, short-term per-chat turn storage.
   - Backed by standard Redis primitives (`STRING` with JSON payloads, indexed via per-chat `ZSET`s).
   - Enforces automatic expiry via Redis key-level `TTL`.
2. **Audit / Long-Term Tier (`h-recall`)**:
   - Long-term persistent storage of conversation history.
   - Backed by **Redis Stack** (`RedisJSON` documents + `RediSearch` full-text and vector HNSW indexes).
   - Populated asynchronously from the hot tier via bounded migration sweeps, then embedded with fast, CPU-efficient local dense vectors (fastembed MiniLM-L6-v2, 384 dimensions).

---

### 2.2 Deployment Topologies

`h-recall` natively supports two deployment topologies without code changes:

#### Topology A: Colocated Single-Redis (Standard / Development)
Both hot tier and audit tier share a single Redis Stack instance (`:6379`).
- `hot_redis_url` is omitted or matches `redis_url`.
- Connection pools are shared between clients to avoid connection overhead and socket duplication.

```
+-----------------------------------------------------------------------------+
|                           Single Redis Stack (6379)                         |
|  ├─ Hot Data:   <pod>:<agent>:chat:<chat_id>:<ts_ns>           (STRING)     |
|  ├─ Hot Index:  <pod>:<agent>:chat-index:<chat_id>             (ZSET)       |
|  ├─ Audit Data: <pod>:<agent>:chat-audit:<chat_id>:<ts_ns>     (RedisJSON)  |
|  └─ Audit Idx:  <pod>:<agent>:chat-audit:idx                   (RediSearch) |
+-----------------------------------------------------------------------------+
```

#### Topology B: Split / Dual-Redis (Production Multi-Tenant)
Hot recency state lives on a shared core Redis instance (low latency, high volume), while audit documents and vector indexes are sharded across dedicated per-agent or per-tenant Redis Stack instances (e.g. `:6380`, `:6381`).
- Eliminates RediSearch global index lock contention during heavy concurrent vector writes and searches.
- `h_semantic_sweep` accepts both `redis_url` (audit tier) and `hot_redis_url` (hot tier).
- `h_semantic_search` and `h_semantic_vectorize` operate directly against `redis_url`.

```
+─────────────────────────────────+           +─────────────────────────────────+
|     Shared Hot Core Redis       |           |   Per-Agent Vector Redis Stack  |
|            (:6379)              |           |        (:6380 / :6381)          |
+─────────────────────────────────+           +─────────────────────────────────+
| <pod>:<agent>:chat:<id>:<ts_ns> |           | <pod>:<agent>:chat-audit:...    |
| <pod>:<agent>:chat-index:<id>   |           | <pod>:<agent>:chat-audit:idx    |
+─────────────────────────────────+           +─────────────────────────────────+
                 ▲                                     ▲
                 │              Cross-Redis            │
                 └────────────── Migration ────────────┘
```

---

## 3. Key Design Principles

### 3.1 Operator-Controlled Cadence (No Background Daemons)
Per [ADR-002](../../docs/adrs/ADR-002-sweeper-workflow-callable.md) and [ADR-010](../../docs/adrs/ADR-010-split-semantic-memory.md), `h-recall` components never launch background threads, daemon processes, or unmanaged `asyncio.create_task` loops. Every operational step is an explicit, bounded NAT function invocation:
- **`h_semantic_sweep`**: Executes one bounded pass of eligible turns from hot to audit storage.
- **`h_semantic_vectorize`**: Executes one bounded batch pass replacing sentinel vectors with real embeddings.
- **Scheduling**: The caller (workflow orchestrator, periodic scheduler, cron, or turn-completion hook) controls execution frequency and resource allocation.

### 3.2 Underlay Enables, Consumer Tracks
Per the project architecture principle (*"Underlay enables, consumer tracks"*):
- `h-recall` provides deterministic storage and indexing primitives.
- `h_semantic_sweep` does not scan the entire Redis keyspace globally. Instead, the consumer passes an explicit list of `chat_ids` to sweep. Passing an empty list `chat_ids=[]` is a deliberate, instantaneous no-op.
- Discovery cost scales strictly as $O(\sum \text{hot turns in passed chats})$, completely decoupled from total database size.

### 3.3 Strict ADR-012 Multi-Tenancy
All keys and RediSearch indexes adhere strictly to [ADR-012](../../docs/adrs/ADR-012-redis-naming-contract.md):
- Keyspace template: `<pod>:<agent>:<scope>:<id>[:<extra>]`
- Scope tag for audit data: **`chat-audit`**
- Token validation: Both `<pod>` and `<agent>` tokens must match `^[A-Za-z0-9_-]+$`.
- Tenant isolation: Index queries enforce `@chat_id:{<escaped_cid>}` filters inside RediSearch.

### 3.4 Schema-Locked 384d Dense Vector Embedding
Vector representations use fast, deterministic, CPU-local embeddings:
- Default model: `sentence-transformers/all-MiniLM-L6-v2` via `fastembed`.
- Dimension: **384 dimensions** (`FLOAT32`, `COSINE` distance metric).
- Schema locking: The HNSW vector index definition locks the 384d dimension. Model swapping requires an index rebuild.

### 3.5 Fail-Soft Hybrid Search (RRF)
Search combines keyword/BM25 text search and dense vector KNN search via Reciprocal Rank Fusion (RRF):
- If the vector embedder fails or encounters an error, search degrades gracefully to pure lexical text search.
- If one search leg errors on the database side, the surviving leg provides the ranked result set without failing the agent turn.

---

## 4. Component Breakdown & Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            NAT Workflow Engine                              │
│                                                                             │
│  ┌───────────────────────┐ ┌───────────────────────┐ ┌───────────────────┐  │
│  │   h_semantic_sweep    │ │ h_semantic_vectorize  │ │ h_semantic_search │  │
│  └───────────┬───────────┘ └───────────┬───────────┘ └─────────┬─────────┘  │
└──────────────┼─────────────────────────┼───────────────────────┼────────────┘
               │                         │                       │
               ▼                         ▼                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       h-recall Internal Core Engine                         │
│                                                                             │
│  ┌───────────────────┐  ┌─────────────────────┐  ┌───────────────────────┐  │
│  │   sweep.migrate   │  │ vectorize_pending   │  │   store.search_hybrid │  │
│  └─────────┬─────────┘  └──────────┬──────────┘  └───────────┬───────────┘  │
│            │                       │                         │              │
│            ▼                       ▼                         ▼              │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                              AuditStore                               │  │
│  │  - ensure_index()             - write_audit()                         │  │
│  │  - read_hot() / delete_hot()  - update_embedding()                    │  │
│  │  - search_text()              - search_knn()                          │  │
│  └───────────────────┬───────────────────────────────────┬───────────────┘  │
│                      │                                   │                  │
│                      ▼                                   ▼                  │
│         ┌────────────────────────┐           ┌───────────────────────┐      │
│         │   FastEmbedEmbedder    │           │ escape_redisearch_query│      │
│         │  (MiniLM-L6-v2, 384d)  │           │   (Query Sanitizer)   │      │
│         └────────────────────────┘           └───────────────────────┘      │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 Migration Pipeline (`h_semantic_sweep`)
1. Caller provides `chat_ids` and `migration_threshold_sec` (e.g. 18000s / 5h).
2. For each `chat_id`, sweep queries the hot index ZSET `<pod>:<agent>:chat-index:<chat_id>` via `ZRANGEBYSCORE` for scores $\le \text{now\_ns} - (\text{threshold\_sec} \times 10^9)$.
3. For each matching key:
   - Reads hot turn payload via `GET` + `json.loads` (plain Redis STRING).
   - If audit document already exists (idempotency check), cleans up orphaned hot key and stale ZSET member.
   - Otherwise, writes RedisJSON audit document with sentinel vector (`[0.0]*384`) and `pending_vectorize="1"` TAG (**Long write FIRST**).
   - Deletes hot key via `DEL` (**Hot delete SECOND**).
   - Lazily removes key member from `chat-index` ZSET via `ZREM`.

### 4.2 Vectorization Pipeline (`h_semantic_vectorize`)
1. Queries RediSearch index for documents flagged with pending vectorization: `FT.SEARCH @pending_vectorize:{1}` sorted by `ts` ascending (FIFO).
2. Collects batch of contents (bounded by `batch_size`, default 64).
3. Invokes `FastEmbedEmbedder.embed()` in a worker thread (`asyncio.to_thread`) to produce 384d dense vectors.
4. For each document in batch:
   - Updates `$.embedding` with computed float vector (**Set embedding FIRST**).
   - Deletes `$.pending_vectorize` field via `JSON.DEL` (**Clear flag SECOND**).

### 4.3 Hybrid Search Pipeline (`h_semantic_search`)
1. Sanitizes user query text and chat ID via `escape_redisearch_query`.
2. Evaluates requested mode (`text`, `semantic`, or `hybrid`):
   - **`text`**: Executes `FT.SEARCH @chat_id:{<cid>} @content:<query>` sorted by `ts` descending.
   - **`semantic`**: Computes query embedding vector, executes KNN query `(@chat_id:{<cid>})=>[KNN <k> @embedding $vec AS d]` sorted by vector distance `d`.
   - **`hybrid`**: Concurrently gathers candidate pools ($k \times \text{multiplier}$) from both Text and Semantic legs, then merges results via Reciprocal Rank Fusion (RRF).

---

## 5. Data Models & Keyspace Contract

### 5.1 Keyspace Naming Table

| Key Type | Substrate | Key Format | Notes |
|---|---|---|---|
| **Hot Turn Data** | Core Redis | `<pod>:<agent>:chat:<chat_id>:<ts_ns>` | Written by `h-network-memory` (STRING) |
| **Hot Chat Index** | Core Redis | `<pod>:<agent>:chat-index:<chat_id>` | Written by `h-network-memory` (ZSET) |
| **Audit Document** | Redis Stack | `<pod>:<agent>:chat-audit:<chat_id>:<ts_ns>` | Written by `h-recall` (RedisJSON) |
| **Audit Index** | Redis Stack | `<pod>:<agent>:chat-audit:idx` | Global RediSearch index for tenant |

### 5.2 RedisJSON Document Schema

```json
{
  "chat_id": "session-42",
  "role": "user",
  "ts": 1715500000.123456,
  "content": "What was the previous configuration value?",
  "embedding": [0.0123, -0.0456, "... (384 floats) ..."],
  "pending_vectorize": "1"
}
```
*Note: `"pending_vectorize"` is present only while awaiting embedding, and is removed once vectorization completes.*

### 5.3 RediSearch Index Schema

```
Index Name: <pod>:<agent>:chat-audit:idx
Prefix:     <pod>:<agent>:chat-audit:
Type:       JSON

Fields:
  - $.chat_id           TAG
  - $.role              TAG
  - $.pending_vectorize TAG
  - $.ts                NUMERIC  SORTABLE
  - $.content           TEXT
  - $.embedding         VECTOR HNSW (TYPE FLOAT32, DIM 384, DISTANCE_METRIC COSINE)
```

---

## 6. Hybrid Retrieval Algorithm (RRF)

Reciprocal Rank Fusion fuses ranked lists from heterogeneous retrieval mechanisms without score normalization anomalies:

$$\text{RRF\_Score}(d) = \sum_{m \in \{\text{text}, \text{semantic}\}} \frac{1}{k_{\text{rrf}} + \text{rank}_m(d) + 1}$$

- $k_{\text{rrf}}$: Smoothing constant (default: 60). Larger values produce a flatter distribution across ranks.
- $\text{rank}_m(d)$: Zero-indexed position of document $d$ in retrieval mode $m$'s candidate pool.
- **Deterministic Tie-Breaking**:
  1. Primary: $\text{RRF\_Score}$ descending
  2. Secondary: Timestamp `ts` descending (more recent documents favored)
  3. Tertiary: Redis document key string ascending (`__key` lexical sort)

---

## 7. Operational & Invariant Guarantees

`h-recall` implements strict behavioral invariants to guarantee zero data loss and crash consistency:

1. **Write-First Migration**: `write_audit` always completes before `delete_hot`. A crash mid-migration preserves data in both tiers; subsequent sweeps detect existing audit copies and safely delete the orphaned hot copy.
2. **Key Suffix Timestamp Derivation**: Nanosecond timestamp `ts_ns` is parsed strictly from the hot Redis key suffix, preventing floating-point rounding drift from JSON float serialization.
3. **Write-First Vectorization**: Real embeddings are stored into `$.embedding` before `$.pending_vectorize` is removed. A crash mid-vectorization re-embeds the document on the next cycle without data corruption.
4. **Lazy Initialization**: Construction of `AuditStore`, `FastEmbedEmbedder`, and NAT function builders is entirely I/O free. TCP sockets and model weights load only on first functional execution.

---

- **End-to-End Testing**: Verified as stage `with-semantic-memory` in the h-network integration test suite.
