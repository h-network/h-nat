# Low-Level Design (LLD) — `h-memory`

> **Canonical Document Status:**
> This document is the canonical low-level specification for `h-memory`. It describes the **current code as implemented** (not aspirational). Where implementations, schemas, or behaviors differ from earlier designs or high-level documents, this document serves as the authoritative technical reference.

---

## 1. Module Layout & Packaging

### 1.1 Directory Structure
```
external/h-memory/
├── pyproject.toml                     # Package metadata, NAT entry point registration
├── requirements.txt                   # Production dependencies (redis>=5,<7)
├── requirements-test.txt              # Test dependencies (pytest, pytest-asyncio)
├── HLD.md                             # High-level architecture and system fit
├── LLD.md                             # Canonical low-level specification (this file)
└── src/nat/plugins/h_network_memory/  # Plugin source tree (or h_memory)
    ├── __init__.py                    # Public exports (BoundedBufferStore, functions)
    ├── memory.py                      # BoundedBufferStore core engine
    └── register.py                    # NAT function registration, Pydantic models, CLI converters
```

### 1.2 Package Entry Point & Dependencies
- **Package Name**: `h-network-memory` (in `pyproject.toml`) / `h-memory` (in `h-nat` module suite).
- **Entry Point**: `[project.entry-points."nat.components"]` maps `h_network_memory = "nat.plugins.h_network_memory.register"`.
- **Runtime Dependency**: `redis>=5,<7` (`redis.asyncio` client).
- **Python Version**: `>=3.11,<3.14`.

---

## 2. Data Models & Schemas

All schemas are defined using Pydantic v2. Input models use `model_config = ConfigDict(extra="forbid")` to catch malformed payloads at validation time.

### 2.1 Pydantic Input Models (`register.py`)

#### `WriteTurnInput`
Defines the per-invocation payload for `h_memory_write_turn`:
```python
class WriteTurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    content: str
    ttl_seconds: int = Field(ge=1)
    hot_keep_count: Optional[int] = Field(default=None, ge=1)
```
- `chat_id`: Identifies the conversation stream. Must be non-empty.
- `role`: Role identifier (e.g. `"user"`, `"assistant"`, `"system"`). Must be non-empty.
- `content`: Dialogue text content.
- `ttl_seconds`: Per-turn retention duration in seconds. Must be `>= 1`. Evaluated against `ttl_seconds_max` inside the function builder.
- `hot_keep_count`: Optional per-call override for count-based rank pruning. Must be `>= 1` if provided.

#### `DeleteChatInput`
Defines the per-invocation payload for `h_memory_delete_chat`:
```python
class DeleteChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(min_length=1)
```

### 2.2 Pydantic Config Models (`register.py`)

#### `HMemoryWriteTurnConfig`
Workflow-level configuration for `_type: h_memory_write_turn`:
```python
_POD_AGENT_TOKEN_PATTERN = r"^[A-Za-z0-9_-]+$"

class HMemoryWriteTurnConfig(FunctionBaseConfig, name="h_memory_write_turn"):
    redis_url: str = Field(
        default="redis://localhost:6379",
        description="Redis connection URL.",
    )
    pod: str = Field(
        ...,
        pattern=_POD_AGENT_TOKEN_PATTERN,
        description="First segment of ADR-012 multi-tenancy primitive <pod>:<agent>.",
    )
    agent: str = Field(
        ...,
        pattern=_POD_AGENT_TOKEN_PATTERN,
        description="Second segment of ADR-012 multi-tenancy primitive <pod>:<agent>.",
    )
    ttl_seconds_max: int = Field(
        default=2592000,  # 30 days
        ge=1,
        description="Operator ceiling on per-call ttl_seconds and ZSET index TTL.",
    )
    hot_keep_count: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional count-based cap on the per-chat ZSET index.",
    )
```

#### `HMemoryDeleteChatConfig`
Workflow-level configuration for `_type: h_memory_delete_chat`:
```python
class HMemoryDeleteChatConfig(FunctionBaseConfig, name="h_memory_delete_chat"):
    redis_url: str = Field(
        default="redis://localhost:6379",
        description="Redis connection URL.",
    )
    pod: str = Field(..., pattern=_POD_AGENT_TOKEN_PATTERN)
    agent: str = Field(..., pattern=_POD_AGENT_TOKEN_PATTERN)
```

### 2.3 Type Converters (`register.py`)
To allow `nat run --input '<JSON>'` CLI string inputs and format return values:
- `_json_str_to_write_turn_input(value: str) -> WriteTurnInput`: Parses JSON string to `WriteTurnInput`.
- `_json_str_to_delete_chat_input(value: str) -> DeleteChatInput`: Parses JSON string to `DeleteChatInput`.
- `_int_to_str(value: int) -> str`: Converts integer delete count to string for CLI output formatting.

---

## 3. Storage Architecture & Keyspace Contract

The keyspace strictly implements the ADR-012 Redis naming contract:

```
<pod>:<agent>:chat:<chat_id>:<ts_ns>          STRING (JSON payload, EX ttl_seconds)
<pod>:<agent>:chat-index:<chat_id>           ZSET   (score=ts_ns, member=turn_key, EXPIRE ttl_seconds_max)
```

### 3.1 Key Segments & Roles
- **`<pod>` / `<agent>`**: ADR-012 tenant isolation segments. Validated against `^[A-Za-z0-9_-]+$`.
- **`chat`**: ADR-012 scope tag allocated to `h-memory` hot-tier turn data.
- **`chat-index`**: ADR-012 scope-tag-with-suffix variant allocated to the per-chat ZSET index.
- **`<chat_id>`**: Application-level session identifier.
- **`<ts_ns>`**: Monotonically increasing Unix nanosecond timestamp.

### 3.2 Payload Format
Turn data keys store JSON UTF-8 strings with separators `(",", ":")`:
```json
{"role": "user", "content": "Hello", "ts": 1778572465}
```
*Note*: `ts` in the JSON payload represents Unix epoch seconds (for backwards compatibility), whereas `ts_ns` in the key suffix and ZSET score represents nanosecond resolution.

---

## 4. Core Implementation (`BoundedBufferStore`)

The `BoundedBufferStore` class (`memory.py`) manages all underlying Redis interactions:

```python
class BoundedBufferStore:
    SCOPE = "chat"
    INDEX_SCOPE = "chat-index"

    def __init__(
        self,
        client: aioredis.Redis,
        pod: str,
        agent: str,
        ttl_seconds_max: int,
    ):
        self._client = client
        self._pod = pod
        self._agent = agent
        self._ttl_max = ttl_seconds_max
        self._last_ts_ns = 0
```

### 4.1 Monotonic Clock Guard
To guarantee sub-second ordering and prevent key collision for high-frequency writes occurring within the same nanosecond on a process instance:
```python
def _next_ts_ns(self) -> int:
    ns = time.time_ns()
    if ns <= self._last_ts_ns:
        ns = self._last_ts_ns + 1
    self._last_ts_ns = ns
    return ns
```

### 4.2 Write Turn Pipeline (`write_turn`)
Executes an atomic multi-command batch using a single non-transactional Redis pipeline (`pipeline(transaction=False)`):

```python
async def write_turn(
    self,
    chat_id: str,
    role: str,
    content: str,
    ttl_seconds: int,
    *,
    ts_seconds: Optional[int] = None,
    hot_keep_count: Optional[int] = None,
) -> str:
    ts_ns = self._next_ts_ns()
    if ts_seconds is None:
        ts_seconds = ts_ns // 1_000_000_000
    payload = json.dumps(
        {"role": role, "content": content, "ts": ts_seconds},
        separators=(",", ":"),
    )
    turn_key = self._turn_key(chat_id, ts_ns)
    index_key = self._index_key(chat_id)
    cutoff_ns = ts_ns - self._ttl_max * 1_000_000_000

    async with self._client.pipeline(transaction=False) as pipe:
        pipe.set(turn_key, payload, ex=ttl_seconds)
        pipe.zadd(index_key, {turn_key: ts_ns})
        pipe.expire(index_key, self._ttl_max)
        pipe.zremrangebyscore(index_key, 0, cutoff_ns)
        if hot_keep_count is not None:
            pipe.zremrangebyrank(index_key, 0, -(hot_keep_count + 1))
        await pipe.execute()

    return turn_key
```

#### Pipeline Steps Breakdown:
1. `SET turn_key payload EX ttl_seconds`: Writes turn data with per-call TTL expiration.
2. `ZADD index_key ts_ns turn_key`: Adds the turn key to the chat index sorted by timestamp.
3. `EXPIRE index_key ttl_seconds_max`: Refreshes index TTL to operator ceiling, ensuring the index outlives the longest possible turn TTL.
4. `ZREMRANGEBYSCORE index_key 0 cutoff_ns`: Conservative cleanup removing index entries whose timestamps are older than `now_ns - ttl_seconds_max * 1e9` (entries guaranteed to have expired).
5. `ZREMRANGEBYRANK index_key 0 -(hot_keep_count + 1)`: (If count cap active) Trims index entries older than the $N$ most recent ranks.
6. Returns `turn_key`.

### 4.3 Delete Chat Pipeline (`delete_chat`)
Purges all resident turns and the index for a chat:

```python
async def delete_chat(self, chat_id: str) -> int:
    if not chat_id:
        return 0
    index_key = self._index_key(chat_id)
    members = await self._client.zrange(index_key, 0, -1) or []
    deleted = 0
    async with self._client.pipeline(transaction=False) as pipe:
        if members:
            pipe.delete(*members)
        pipe.delete(index_key)
        results = await pipe.execute()
    for r in results:
        deleted += int(r or 0)
    return deleted
```
- Retrieves all indexed member keys via `ZRANGE index_key 0 -1`.
- Executes batch `DELETE` on all member keys and the `index_key`.
- Returns total count of deleted keys resident at execution time.

---

## 5. NAT Function Registration & Lifecycle (`register.py`)

### 5.1 Eager Initialization & Teardown
Both `h_memory_write_turn` and `h_memory_delete_chat` are implemented as async generator builders:
1. **Connection Initialization**: Opens `aioredis.Redis.from_url(config.redis_url, decode_responses=True)`.
2. **Eager Health Check**: Invokes `await client.ping()`. Misconfigurations or unreachable instances immediately fail workflow initialization rather than failing mid-call.
3. **Structured Logging**: Logs connection details with pinned substring format (`h_memory_write_turn connected: redis_url=... pod=... agent=... ttl_seconds_max=... hot_keep_count=...`).
4. **Invocation Yield**: Yields `FunctionInfo.from_fn(_invoke, ...)`.
5. **Teardown**: Executes `await client.aclose()` in a `finally:` block.

### 5.2 Deferred Type Hints Resolution
> **Important Implementation Detail**:
> `register.py` intentionally omits `from __future__ import annotations`. In NAT 1.6.0, `FunctionInfo.from_fn` introspects `_invoke` type hints using `typing.get_type_hints()` against NAT's module globals. Deferred annotations cause `NameError` on custom Pydantic models (`WriteTurnInput`). Non-deferred (eager) annotations resolve at module definition time and prevent this failure.

---

## 6. Precision, Compatibility & Substrate Constraints

### 6.1 IEEE-754 64-bit Floating-Point Scores in Redis ZSETs
- Redis Sorted Set scores are IEEE-754 64-bit double-precision floats (53-bit mantissa, precision up to $\approx 9 \times 10^{15}$).
- Nanosecond timestamps ($\approx 1.7 \times 10^{18}$) exceed double-precision integer exactness, resulting in slight ulp rounding deltas (typically 50–100 ns) when reading scores via `ZREVRANGE WITHSCORES` or `ZSCORE`.
- **System Invariant**:
  - The ZSET member string (`<pod>:<agent>:chat:<chat_id>:<ts_ns>`) is the exact, canonical source of identity and timestamp.
  - Scores are used exclusively for relative ordering and range trimming (`ZREMRANGEBYSCORE`, `ZREMRANGEBYRANK`).
  - Equality assertions in test harnesses must assert member string equality, not float score exactness.

### 6.2 Redis Command Substrate
- `h-memory` requires only Redis 7.x core commands: `SET`, `EXPIRE`, `ZADD`, `ZREMRANGEBYSCORE`, `ZREMRANGEBYRANK`, `ZRANGE`, `DELETE`, `PING`.
- It intentionally does not use `JSON.SET`, `JSON.GET`, or `FT.*` commands.

---

## 7. Shared Substrate Contract & Invariants (with `h-recall`)

Per ADR-010 and ADR-012, `h-memory` and `h-recall` share the `<pod>:<agent>:chat-index:<chat_id>` ZSET index:

| Responsibility | `h-memory` (Hot Tier) | `h-recall` (Semantic Memory / Reader) |
| :--- | :--- | :--- |
| **Write Turn** | `SET` turn key + `ZADD` to index | Does not write hot-tier turns |
| **Index Expiration** | Refreshes `EXPIRE ttl_seconds_max` | Does not modify index TTL |
| **Conservative Cleanup** | `ZREMRANGEBYSCORE` & `ZREMRANGEBYRANK` | Lazy `ZREM` during reads for expired keys |
| **Read Operations** | None (writer only) | `ZREVRANGE` + `MGET` (recency reads, search) |
| **Audit / Vector Tier** | None (never accesses vector namespace) | Manages vector embeddings & hybrid search |

### 7.1 Count-Eviction / Orphan Invariant
When `hot_keep_count` trims an index entry via `ZREMRANGEBYRANK`, the underlying data key is **not** immediately deleted with `DEL`. It remains in Redis until its individual key TTL (`EX`) expires. Consequently:
- `chat-index` reflects the active working-set window.
- `delete_chat` purges only keys currently indexed; orphaned keys expire via TTL.

---

## 8. Discrepancies, Evolution & Decisions Log

This section explicitly documents where current code differs from historical precursors, high-level roadmaps, or earlier documentation, along with the rationale.

### 8.1 Pivot from Memory Plugin (`bounded_buffer`) to Function Verbs
- **Historical Precursor**: In early iterations (Rounds 13–24), memory was registered as a NAT Memory Plugin using `@register_memory(BoundedBufferMemoryConfig)` implementing the `MemoryEditor` protocol (`LPUSH`/`LTRIM` lists).
- **Current Code**: Dropped in Round 26. Memory is now exposed as discrete NAT functions (`h_memory_write_turn`, `h_memory_delete_chat`) backed by `BoundedBufferStore`.
- **Rationale**: NAT's `MemoryEditor` protocol coupled memory tightly to specific orchestrator lifecycle hooks. Exposing discrete function verbs allows framework-agnostic composition (e.g. `h_chat_cycle`, custom harnesses, external API servers).

### 8.2 Keyspace Migration (ADR-001 to ADR-012)
- **Historical Precursor**: Keys were formatted as `<community>:h-network-memory:turn:{chat_id}:{ts_ns}` and `<community>:h-network-memory:turn-index:{chat_id}` with a single `community: str` config field.
- **Current Code**: Formatted as `<pod>:<agent>:chat:{chat_id}:{ts_ns>` and `<pod>:<agent>:chat-index:{chat_id}`, configured via separate `pod: str` and `agent: str` fields.
- **Rationale**: Standardized under ADR-012 to establish a unified multi-tenancy primitive across all `h-nat` modules.

### 8.3 Addition of Count-Based Bounding (`hot_keep_count`)
- **Historical Precursor**: Earlier versions relied solely on time-based TTL expiration (`ttl_seconds_max` / `ttl_seconds`).
- **Current Code**: Round 70 added optional rank-based trimming (`ZREMRANGEBYRANK`) at both the configuration level and as a per-call input override.
- **Rationale**: Supports applications requiring strict context window turn counts (e.g., fixed $N$-turn LLM prompts) regardless of dialogue elapsed time.

### 8.4 Separation of Semantic Memory (ADR-010)
- **Historical Precursor**: Early monolithic prototypes combined hot-tier turn buffering with vector search, embeddings, and TTL sweeping.
- **Current Code**: All vectorization, hybrid search, and background sweepers are decoupled into `h-recall` (`h-network-semantic-memory`).
- **Rationale**: Preserves `h-memory` as an ultra-lightweight, zero-dependency hot tier running on standard Redis without forcing operators to deploy Redis Stack or embedding infrastructure.

### 8.5 Packaging & Repository Naming (`h-nat` vs `h-network-nemo-agent-toolkit`)
- **Current Code**: In `h-nat`, modules are structured under `external/h-memory/` (alongside `external/h-openshell/`, `external/h-orchestrator/`, `external/h-recall/`, `external/h-asimov/`).
- **Rationale**: Reflects the standalone public release structure of `h-nat`.
