# Low-Level Design (LLD) — `h-recall`

**Module:** `h-recall` (`h-network-semantic-memory`)  
**Status:** Living Technical Reference (Reflects Live Codebase)  
**Implementation Package:** `src/nat/plugins/h_network_semantic_memory/`  
**Maintainer Agent:** `mod-h-recall`  
**Last Synchronized:** 2026-08-29 (Round 67 dual-Redis baseline)

---

## 1. Document Overview & Scope

This Low-Level Design document specifies the exact implementation details, behavioral invariants, data structures, and function-level mechanics of `h-recall`.

> **Note on Code Synchronization**: Per h-network development conventions, this LLD accurately reflects the production code implemented in `src/nat/plugins/h_network_semantic_memory/`. Any behavioral modification to the code must be accompanied by an immediate update to this document.

---

## 2. Module File Layout

```
src/nat/plugins/h_network_semantic_memory/
├── __init__.py            # Public module surface; re-exports AuditStore
├── register.py            # NAT plugin builders, Pydantic configs, converters
└── _internal/
    ├── __init__.py        # Internal package marker
    ├── store.py           # Async AuditStore (RedisJSON, RediSearch, RRF fusion)
    ├── sweep.py           # migrate() — hot-to-audit migration orchestration
    ├── vectorize.py       # vectorize_pending() — flag-driven batch embedding
    ├── embedder.py        # FastEmbedEmbedder — lazy threaded model wrapper
    └── sanitize.py        # escape_redisearch_query — interpolation safety
```

---

## 3. Core Behavioral Invariants

The `h-recall` codebase enforces eleven strict invariants across all execution paths:

| # | Invariant | Enforcement Location | Architectural Rationale |
|---|---|---|---|
| **1** | `SWEEP_INTERVAL <= MIGRATION_THRESHOLD <= HOT_TTL_SEC` | Operator config (`register.py`, `INSTALL.md`) | Ensures turns are migrated to the audit tier before the hot key expires from Redis. |
| **2** | **Migration Write Order**: Audit-write FIRST, Hot-delete SECOND | `_internal/sweep.py:migrate` | Guarantees zero data loss on crash. An uncompleted migration leaves duplicates, never lost turns. |
| **3** | **Migration Idempotency**: Guarded by `audit_exists` | `_internal/sweep.py:migrate` | If a previous sweep crashed post-write, the next sweep skips re-writing audit data and cleans up the orphaned hot key. |
| **4** | **Timestamp Extraction**: `ts_ns` derived from hot key suffix, never `doc["ts"]` | `_internal/store.py`, `_internal/sweep.py` | Prevents floating-point rounding drift from JSON float serialization. |
| **5** | **Sentinel Vector on Sweep**: Audit doc created with `[0.0]*384` + `pending_vectorize="1"` | `_internal/sweep.py:migrate` | Makes the document immediately visible to full-text search while flagging it for vectorization. |
| **6** | **Vectorization Write Order**: Update embedding FIRST, delete flag SECOND | `_internal/vectorize.py:vectorize_pending` | Prevents a document from losing its pending flag before real vector weights are persisted. |
| **7** | **Flag-Based Discovery**: Vectorization queries `@pending_vectorize:{1}` | `_internal/vectorize.py`, `_internal/store.py` | Discovery is $O(\text{pending})$ via RediSearch TAG index rather than $O(\text{corpus})$ keyspace scanning. |
| **8** | **Hot Expiry Ownership**: `h-network-memory` owns hot `TTL` | `external/h-network-memory` | `h-recall` reads and deletes hot keys during sweep, but never configures or refreshes hot TTLs. |
| **9** | **Schema-Locked 384d Vectors**: `DIM 384 FLOAT32 COSINE` | `_internal/store.py:ensure_index` | RediSearch HNSW vector field schema explicitly enforces 384 dimensions matching MiniLM-L6-v2. |
| **10** | **Deterministic RRF Deduplication**: `__key` stamped on every result | `_internal/store.py:_search_to_docs` | Ensures unique document identification across text and semantic candidate pools. |
| **11** | **Fail-Soft Search Resilience**: Independent per-leg execution | `_internal/store.py:search_hybrid` | Search degrades to the surviving leg if text search, KNN search, or vector embedding fails. |

---

## 4. Module Constants & Configuration Defaults

### 4.1 Global Constants

```python
# _internal/store.py
VECTOR_DIM: int = 384
HOT_SCOPE: str = "chat"
AUDIT_SCOPE: str = "chat-audit"
PENDING_VECTORIZE_TAG_VALUE: str = "1"

# _internal/embedder.py
DEFAULT_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"

# _internal/sanitize.py
_REDISEARCH_SPECIALS: frozenset[str] = frozenset(',.<>{}[]"\':;!@#$%^&*()-+=~|/?\\')

# register.py
_POD_AGENT_TOKEN_PATTERN: str = r"^[A-Za-z0-9_-]+$"
```

---

## 5. Detailed Component Specifications

### 5.1 `AuditStore` (`_internal/store.py`)

`AuditStore` is the core data-access class managing RedisJSON documents, RediSearch querying, and cross-module hot-tier reads.

#### 5.1.1 Constructor & Client Topologies
`AuditStore` supports both colocated and split dual-client configurations:

```python
def __init__(
    self,
    pod: str,
    agent: str,
    *,
    client: Optional[aioredis.Redis] = None,
    audit_client: Optional[aioredis.Redis] = None,
    hot_client: Optional[aioredis.Redis] = None,
):
```

- **Colocated Mode**: Pass `client=c`. Sets both `self._audit_client` and `self._hot_client` to `c`.
- **Split Mode**: Pass `audit_client=ac` and `hot_client=hc`. Requires both clients.
- **Validation**: Passing `client` together with `audit_client` raises `TypeError`. Omitting all clients raises `TypeError`.

#### 5.1.2 Properties & Key Derivation
- `audit_client` $\to$ `aioredis.Redis` (audit tier operations: `JSON.SET`, `FT.SEARCH`, `FT.CREATE`)
- `hot_client` $\to$ `aioredis.Redis` (hot tier reads: `GET`, `DEL`, `ZRANGEBYSCORE`, `ZREM`)
- `client` $\to$ Backwards-compatibility alias returning `audit_client`
- `hot_prefix` $\to$ `f"{pod}:{agent}:chat:"`
- `hot_index_prefix` $\to$ `f"{pod}:{agent}:chat-index:"`
- `audit_prefix` $\to$ `f"{pod}:{agent}:chat-audit:"`
- `audit_index_name` $\to$ `f"{pod}:{agent}:chat-audit:idx"`
- `hot_key(chat_id, ts_ns)` $\to$ `f"{hot_prefix}{chat_id}:{ts_ns}"`
- `hot_index_key(chat_id)` $\to$ `f"{hot_index_prefix}{chat_id}"`
- `audit_key(chat_id, ts_ns)` $\to$ `f"{audit_prefix}{chat_id}:{ts_ns}"`
- `chat_id_from_hot_key(hot_key)` $\to$ Extracted second-to-last segment from `:` split.
- `ts_ns_from_hot_key(hot_key)` $\to$ `int(hot_key.rsplit(":", 1)[-1])` (Invariant 4).

#### 5.1.3 Index Lifecycle (`ensure_index`)
Performs idempotent RediSearch index creation:
1. Fast-path exit if `self._index_ready == True`.
2. Declares schema:
   - `TagField("$.chat_id", as_name="chat_id")`
   - `TagField("$.role", as_name="role")`
   - `TagField("$.pending_vectorize", as_name="pending_vectorize")`
   - `NumericField("$.ts", as_name="ts", sortable=True)`
   - `TextField("$.content", as_name="content")`
   - `VectorField("$.embedding", "HNSW", {"TYPE": "FLOAT32", "DIM": 384, "DISTANCE_METRIC": "COSINE"}, as_name="embedding")`
3. Calls `_audit_client.ft(audit_index_name).create_index(...)` with prefix `[self.audit_prefix]` and `IndexType.JSON`.
4. Traps `"Index already exists"` exceptions and caches `self._index_ready = True`.

#### 5.1.4 Audit Storage Operations
- `write_audit(chat_id, ts_ns, role, content, ts_seconds, embedding, pending_vectorize)`:
  - Generates key via `self.audit_key(chat_id, ts_ns)`.
  - Stuffs dictionary. If `pending_vectorize=True`, sets `doc["pending_vectorize"] = "1"`.
  - Executes `await self._audit_client.json().set(key, "$", doc)`.
- `update_embedding(key, embedding)`:
  - Executes `await self._audit_client.json().set(key, "$.embedding", embedding)`.
- `clear_pending_flag(key)`:
  - Executes `await self._audit_client.json().delete(key, "$.pending_vectorize")`.
- `audit_exists(key)`:
  - Executes `bool(await self._audit_client.exists(key))`.

#### 5.1.5 Hot-Tier Read Operations
- `read_hot(key)`:
  - Executes `raw = await self._hot_client.get(key)`.
  - Decodes UTF-8 if bytes; parses JSON via `json.loads(raw)`.
  - Returns `None` if key missing or payload invalid.
- `delete_hot(key)`:
  - Executes `await self._hot_client.delete(key)`.
- `hot_ttl(key)`:
  - Executes `await self._hot_client.ttl(key)`.

#### 5.1.6 Search Implementations
- `search_text(chat_id, query, k=10)`:
  - Sanitizes `chat_id` (with whitespace escaped) and `query`.
  - Constructs `Query(f"@chat_id:{{{cid}}} @content:{q_text}").sort_by("ts", asc=False).paging(0, k)`.
  - Dispatches to `_search_to_docs(q)`.
- `search_knn(chat_id, qvec_bytes, k=10)`:
  - Sanitizes `chat_id`.
  - Constructs `Query(f"(@chat_id:{{{cid}}})=>[KNN {k} @embedding $vec AS d]").sort_by("d").dialect(2).paging(0, k)`.
  - Dispatches to `_search_to_docs(q, query_params={"vec": qvec_bytes})`.
- `search_hybrid(chat_id, query, embedder, k=10, rrf_k=60, candidate_pool_multiplier=2)`:
  - Computes `pool = max(k, k * candidate_pool_multiplier)`.
  - Asynchronously embeds query via `embedder.embed([query])` and packs to bytes via `_vec_to_bytes`.
  - Gathers `_safe_text()` and `_safe_knn()` concurrently using `asyncio.gather`.
  - Traps exceptions per leg, falling back to empty lists (`[]`).
  - Merges hits via `_rrf_merge(text_hits, knn_hits, k=k, rrf_k=rrf_k)`.

#### 5.1.7 Helper Functions
- `_search_to_docs(q, **kwargs)`:
  - Executes search against `_audit_client`.
  - Iterates results, extracting `d.json` payload or falling back to `JSON.GET`.
  - Stamps `doc["__key"] = d.id` (Invariant 10).
- `_rrf_merge(text_hits, semantic_hits, k, rrf_k)`:
  - Accumulates RRF score: $\text{score} += \frac{1}{\text{rrf\_k} + \text{rank} + 1}$.
  - Sorts documents with key: `(-score, -float(doc.get("ts", 0)), doc["__key"])`.
  - Returns top `k` documents.
- `_vec_to_bytes(vec)`:
  - Encodes 384 floats using `struct.pack(f"<{VECTOR_DIM}f", *vec)` (little-endian IEEE 754 float32).

---

### 5.2 Sweep Migration (`_internal/sweep.py`)

Function signature:
```python
async def migrate(
    store: AuditStore,
    chat_ids: list[str],
    *,
    migration_threshold_sec: int,
    max_per_cycle: int = 0,
) -> dict[str, int]:
```

#### Execution Logic:
1. **No-Op Guard**: If `not chat_ids`, immediately return `{"migrated": 0, "skipped_existing": 0, "skipped_fresh": 0, "scanned": 0}`.
2. **Index Assurance**: Call `await store.ensure_index()`.
3. **Timestamp Calculation**:
   - `now_ns = time.time_ns()`
   - `cutoff_ns = now_ns - (migration_threshold_sec * 1_000_000_000)`
4. **Chat Iteration**:
   - For each `chat_id` in `chat_ids`:
     - Retrieve matching keys: `store.hot_client.zrangebyscore(chat_index_key, 0, cutoff_ns)`.
     - For each `turn_key`:
       - `scanned += 1`
       - Check `max_per_cycle` bound.
       - Read hot turn via `await store.read_hot(turn_key)`.
       - If `doc is None` (stale index entry): `await store.hot_client.zrem(chat_index_key, turn_key)` $\to$ continue.
       - Parse `cid` and `ts_ns` from key string.
       - Compute `long_key = store.audit_key(cid, ts_ns)`.
       - If `await store.audit_exists(long_key)`:
         - Clean up hot copy: `await store.delete_hot(turn_key)`
         - Clean up ZSET member: `await store.hot_client.zrem(chat_index_key, turn_key)`
         - `skipped_existing += 1` $\to$ continue.
       - Write audit document with sentinel vector (`[0.0]*384`) and `pending_vectorize=True` (**Invariant 2**).
       - Delete hot key: `await store.delete_hot(turn_key)`.
       - Clean up ZSET member: `await store.hot_client.zrem(chat_index_key, turn_key)`.
       - `migrated += 1`.
5. Return results dictionary.

---

### 5.3 Batch Vectorization (`_internal/vectorize.py`)

Function signature:
```python
async def vectorize_pending(
    store: AuditStore,
    embedder: FastEmbedEmbedder,
    *,
    batch_size: int = 64,
    max_per_cycle: int = 0,
) -> dict[str, int]:
```

#### Execution Logic:
1. `await store.ensure_index()`.
2. Define `page_limit = max_per_cycle if max_per_cycle > 0 else DEFAULT_PAGING_LIMIT` (10,000).
3. Formulate discovery query:
   ```python
   Query(f"@pending_vectorize:{{{PENDING_VECTORIZE_TAG_VALUE}}}")
       .sort_by("ts", asc=True)
       .paging(0, page_limit)
   ```
4. Execute search: `res = await store.audit_client.ft(store.audit_index_name).search(query)`.
5. For each document:
   - Extract JSON content.
   - Append `(d.id, content)` to pending buffer.
   - When `len(pending) >= batch_size`, invoke `await _flush()`.
   - Check `max_per_cycle` cap.
6. Invoke `_flush()` on remaining buffer items.
7. **`_flush()` Implementation**:
   - `vectors = await embedder.embed(contents)`.
   - For each `(key, vec)`:
     - `await store.update_embedding(key, vec)` (**Invariant 6**).
     - `await store.clear_pending_flag(key)`.
8. Return `{"vectorized": vectorized, "scanned": scanned, "batches": batches}`.

---

### 5.4 FastEmbed Wrapper (`_internal/embedder.py`)

#### Design:
- **Lazy Initialization**: Constructor does not import `fastembed` or load model weights.
- **Concurrency Safety**: `_ensure_loaded()` uses `asyncio.Lock()` to prevent duplicate loads during concurrent cold starts.
- **Thread Offloading**: FastEmbed execution is CPU-bound; model loading and inference execute inside `asyncio.to_thread`.
- **Numpy Isolation**: Converts raw numpy arrays to `list[float]` to prevent numpy leakage into JSON serialization paths.

---

### 5.5 Query Sanitization (`_internal/sanitize.py`)

Function signature:
```python
def escape_redisearch_query(value: str, *, escape_whitespace: bool = False) -> str:
```

#### Logic:
- Iterates characters in `value`.
- If `ch in _REDISEARCH_SPECIALS` or (`escape_whitespace and ch.isspace()`):
  - Prepends `\` to character.
- Used with `escape_whitespace=True` for TAG fields (`@chat_id:{...}`) and `escape_whitespace=False` for TEXT fields (`@content:...`).

---

### 5.6 NAT Registration Layer (`register.py`)

Registers the three workflow-callable components:

```
[project.entry-points."nat.components"]
h_network_semantic_memory = "nat.plugins.h_network_semantic_memory.register"
```

#### Pydantic Configuration Models:
1. `HSemanticSearchConfig(FunctionBaseConfig, name="h_semantic_search")`
   - `redis_url: str = "redis://localhost:6379"`
   - `pod: str` (Regex pattern `^[A-Za-z0-9_-]+$`)
   - `agent: str` (Regex pattern `^[A-Za-z0-9_-]+$`)
   - `embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"`
   - `rrf_k: int = 60`
   - `candidate_pool_multiplier: int = 2`

2. `HSemanticSweepConfig(FunctionBaseConfig, name="h_semantic_sweep")`
   - `redis_url: str = "redis://localhost:6379"` (Audit tier)
   - `hot_redis_url: Optional[str] = None` (Hot tier; defaults to `redis_url`)
   - `pod: str`
   - `agent: str`
   - `migration_threshold_sec: int = 18000` (5h default)
   - `max_per_cycle: int = 0`

3. `HSemanticVectorizeConfig(FunctionBaseConfig, name="h_semantic_vectorize")`
   - `redis_url: str = "redis://localhost:6379"`
   - `pod: str`
   - `agent: str`
   - `embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"`
   - `batch_size: int = 64`
   - `max_per_cycle: int = 0`

#### Input Models (`ConfigDict(extra="forbid")`):
- `SemanticSearchInput`: `chat_id: str`, `query: str`, `top_k: int = 10`, `mode: str = "hybrid"`
- `SemanticSweepInput`: `chat_ids: list[str] = []`
- `SemanticVectorizeInput`: `batch_size: Optional[int] = None`, `max_per_cycle: Optional[int] = None`

#### Converter Functions:
- `_json_str_to_search_input`
- `_json_str_to_sweep_input` (empty/blank string parses to `SemanticSweepInput(chat_ids=[])`)
- `_json_str_to_vectorize_input`
- `_obj_to_str` (`json.dumps(value, default=str)`)

---

## 6. Failure Recovery Matrix

| Failure Event | Immediate System Impact | State Integrity | Recovery Mechanism |
|---|---|---|---|
| **Crash during `migrate` after `write_audit` before `delete_hot`** | Turn exists in both Hot and Audit tiers. | Consistent. Audit doc has sentinel vector + `pending_vectorize="1"`. | Next `migrate` call hits `audit_exists()`, deletes orphaned hot key, cleans up ZSET member, and increments `skipped_existing`. |
| **Crash during `vectorize_pending` after `update_embedding` before `clear_pending_flag`** | Document has real 384d vector but still carries `pending_vectorize="1"`. | Consistent. Full-text and KNN search function normally. | Next `vectorize_pending` cycle rediscovers document, re-computes embedding, updates vector, and successfully clears flag. |
| **Embedder failure during `search_hybrid`** | KNN semantic search leg cannot execute. | Search leg fails softly; text search executes independently. | `search_hybrid` merges text results with empty KNN results, returning ranked BM25 documents. |
| **Hot Redis instance outage during split-mode sweep** | `migrate` cannot read hot turns or query ZSET index. | Audit tier unmodified; hot turns remain untouched in hot Redis. | `migrate` logs exception and skips affected chat IDs. Retry executes on next scheduled cycle. |
| **Stale member in `chat-index` ZSET (hot key expired)** | `read_hot` returns `None`. | Data already expired per hot TTL policy. | `migrate` executes lazy cleanup via `hot_client.zrem` and proceeds to next turn. |

---

## 7. Performance & Big-O Analysis

| Operation | Time Complexity | Space / Memory Complexity | Notes |
|---|---|---|---|
| `migrate` | $O(\sum_{c \in \text{chat\_ids}} N_c)$ | $O(\text{batch})$ | $N_c$ is count of turns older than threshold in chat $c$. Decoupled from total DB size. |
| `vectorize_pending` | $O(M_{\text{pending}} \times D / B)$ | $O(B \times D)$ | $M_{\text{pending}}$ is count of un-embedded docs; $B$ is batch size (64); $D$ is vector dim (384). |
| `search_text` | $O(\log N_{\text{audit}} + K)$ | $O(K)$ | RediSearch inverted index text search scoped to TAG filter. |
| `search_knn` | $O(M \cdot \log N_{\text{audit}} + K \log K)$ | $O(K)$ | HNSW vector index nearest neighbor search scoped to TAG filter. |
| `search_hybrid` | $O(\text{Cost}_{\text{text}} + \text{Cost}_{\text{knn}} + P \log P)$ | $O(P)$ | $P = K \times \text{multiplier}$ (candidate pool size). Merged via RRF. |

---

## 8. Verification & Reference Implementation

- **Unit Test Suite**: `tests/` contains 13 automated tests (`test_invariants.py`, `test_search_hybrid.py`, `test_sanitize.py`, `test_fill_and_search_example.py`).
- **Build Checks**: `_verify/check.py` validates colocated (`build-check.yaml`) and split-topology (`build-check-split.yaml`) configurations with zero network substrate.
- **End-to-End Example**: `examples/fill-and-search/` provides a complete, runnable demonstration (`run_demo.py`, `workflow.yaml`, `sweep.yaml`, `vectorize.yaml`, `search.yaml`) exercising hot memory planting, sweep migration, vectorize batching, and hybrid search against live Redis Stack.
