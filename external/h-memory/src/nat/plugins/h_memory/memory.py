"""Bounded-buffer hot-tier store (library code; not a NAT plugin).

Implements the write side of the ADR-012 keyspace::

    <pod>:<agent>:chat:{chat_id}:{ts_ns}                    STRING (JSON)
                                                              {"role":..., "content":..., "ts":<unix-seconds>}
                                                              SET ... EX <per-call ttl_seconds>
    <pod>:<agent>:chat-index:{chat_id}                       ZSET
                                                              score = ts_ns
                                                              member = full turn key
                                                              EXPIRE ttl_seconds_max (refreshed per write)

Round 28 migrated this from ADR-001's shape
(``<community>:h-network-memory:turn:{chat_id}:{ts_ns}``) to ADR-012's
``<pod>:<agent>:chat:...`` shape — see ``docs/adrs/ADR-012-redis-naming-contract.md``.

Round 26 (per round-25 architectural-pivot consultation) had already
dropped the NAT memory-plugin abstraction; reads (``recent`` /
``search`` / index lazy-ZREM) belong to ``h-recall`` / ``h-network-semantic-memory``
(per ADR-010). What stays here is the narrow write surface plus ``delete_chat`` for cleanup.

Index ownership (shared substrate contract — see ``INSTALL.md`` § 7):

  - Memory module owns the **write contract**: ZADD on every write,
    EXPIRE refreshed to ``ttl_seconds_max``, ZREMRANGEBYSCORE per write
    to bound the ZSET against the largest TTL window, and (round 70,
    when ``hot_keep_count`` is set) ZREMRANGEBYRANK per write to bound
    the ZSET to the most-recent N entries.
  - Semantic-memory / recall module owns the **read contract**: ZREVRANGE for
    newest-first, MGET to filter expired data keys, ZREM for stale
    members. This module does NOT read the index at runtime.

JSON payload ``ts`` is unix-seconds (caller compat with sealed
round-18/20/22/24 evidence); sub-second ordering lives in the key
suffix and the ZSET score (both ``ts_ns``).
"""
from __future__ import annotations

import json
import time
from typing import Optional

import redis.asyncio as aioredis


class BoundedBufferStore:
    """Hot-tier turn writer, ``<pod>:<agent>``-scoped, TTL-evicted.

    Per-instance state:

      - ``_client``: Redis async client (caller owns its lifecycle).
      - ``_pod`` / ``_agent``: ADR-012 ``<pod>:<agent>`` primitive
        segments; immutable post-construction.
      - ``_ttl_max``: operator-set ceiling on per-call ``ttl_seconds``.
        Used by ``write_turn`` to refresh the ZSET index TTL (so the
        index outlives the longest possible turn TTL) and to size the
        ZREMRANGEBYSCORE cleanup window.
      - ``_last_ts_ns``: monotonic guard — same-nanosecond writes get
        distinct keys via ``max(time_ns, _last_ts_ns + 1)``. Per-instance
        only; concurrent-writer disambiguation across processes is the
        BGP-community contract's job (per ADR-012's ``<pod>:<agent>``
        primitive).
    """

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

    @property
    def client(self) -> aioredis.Redis:
        return self._client

    @property
    def pod(self) -> str:
        return self._pod

    @property
    def agent(self) -> str:
        return self._agent

    @property
    def ttl_seconds_max(self) -> int:
        return self._ttl_max

    # -- key derivation ----------------------------------------------------

    def _turn_key(self, chat_id: str, ts_ns: int) -> str:
        return f"{self._pod}:{self._agent}:{self.SCOPE}:{chat_id}:{ts_ns}"

    def _index_key(self, chat_id: str) -> str:
        return f"{self._pod}:{self._agent}:{self.INDEX_SCOPE}:{chat_id}"

    def _next_ts_ns(self) -> int:
        ns = time.time_ns()
        if ns <= self._last_ts_ns:
            ns = self._last_ts_ns + 1
        self._last_ts_ns = ns
        return ns

    # -- writes ------------------------------------------------------------

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
        """Write one turn to the hot tier; return the full turn key.

        Caller is responsible for validating ``ttl_seconds`` is in
        ``[1, ttl_seconds_max]`` — see ``register.h_memory_write_turn``
        for the function-`_type:` layer that does this. The store
        does not re-validate (single source of truth in the function
        builder; the store applies whatever it's given).

        Pipeline (one round-trip):

          1. ``SET turn_key payload EX ttl_seconds``
          2. ``ZADD index_key ts_ns turn_key``
          3. ``EXPIRE index_key ttl_seconds_max`` — index lives at least
             as long as the longest possible turn TTL.
          4. ``ZREMRANGEBYSCORE index_key 0 cutoff_ns`` where
             ``cutoff_ns = now_ns - ttl_seconds_max * 1e9``. Trims
             entries that *cannot* still have a live data key (their
             TTL has provably elapsed against the max window). This
             is the conservative bound — never false-positive-removes
             a still-live entry.
          5. (round 70) ``ZREMRANGEBYRANK index_key 0 -(hot_keep_count+1)``
             — count-based bound, only when ``hot_keep_count`` is not
             None. Keeps the most-recent N index entries (ranks
             ``-N..-1``); evicts the rest from the index. The orphaned
             data keys remain in Redis until their own ``EX`` fires;
             the chat-index reflects the effective working-set window.

        ``ts_seconds`` overrides the JSON payload's ``ts`` field
        (defaults to ``ts_ns // 1e9``). The key suffix and ZSET score
        always use the just-derived ``ts_ns`` for sub-second uniqueness
        and ordering.

        ``hot_keep_count`` is the per-call effective count cap. When
        ``None`` (default), no count cap is applied — the index is
        bounded only by step 4's score-based cleanup. When set
        (caller has already validated ``>= 1``), step 5 trims to the
        most-recent N entries.
        """
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
                # Keep the most-recent N entries (top N by score).
                # ZREMRANGEBYRANK key 0 -(N+1) removes ranks 0..(end-N-1),
                # leaving the top-N highest-scored entries intact.
                pipe.zremrangebyrank(index_key, 0, -(hot_keep_count + 1))
            await pipe.execute()

        return turn_key

    # -- delete ------------------------------------------------------------

    async def delete_chat(self, chat_id: str) -> int:
        """Wipe one chat's hot-tier state. Returns count of keys deleted.

        Reads the index, deletes every still-live data key it points at,
        then deletes the index itself. Already-expired data keys are
        already gone from Redis (TTL); the count reflects what was
        actually still in the store, not the historical write count.

        The count includes the index key itself (so a chat with N live
        turns returns ``N + 1``).
        """
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
        # results is [data_delete_count, index_delete_count] when
        # members exist, else [index_delete_count].
        for r in results:
            deleted += int(r or 0)
        return deleted
