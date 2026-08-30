"""Async audit-tier store — the long counterpart to memory module's hot tier.

Owns the ``<pod>:<agent>:chat-audit:`` keyspace per ADR-012:

    <pod>:<agent>:chat-audit:<chat_id>:<ts_ns>     # data — RedisJSON document
    <pod>:<agent>:chat-audit:idx                    # global RediSearch index

Document shape (RedisJSON):

    {
      "chat_id": "<chat_id>",
      "role": "user" | "assistant" | ...,
      "ts": <unix-seconds>,
      "content": "<text>",
      "embedding": [<float>, ... 384 ...],
      "pending_vectorize": true   # absent once vectorize replaces the sentinel
    }

The ``embedding`` field is locked at 384 dimensions (MiniLM); the index
schema embeds the dim, so a model swap is an index-rebuild operation
(announcement § 2.3).

Cross-module read of memory's hot tier (per memory's INSTALL.md § 7):
the hot tier at ``<pod>:<agent>:chat:<chat_id>:<ts_ns>`` is plain Redis
**STRING** (JSON payload encoded as UTF-8 bytes), NOT RedisJSON. This
store reads it via ``GET`` + ``json.loads``; ``JSON.GET`` would error
because memory's hot tier is on a non-Redis-Stack substrate. Only the
audit tier (this module) opts into Redis Stack.

LLD invariants (carried over from h-sessions; preserved here):

  1. SWEEP_INTERVAL ≤ MIGRATION_THRESHOLD — caller-config; INSTALL.md § 5.
  2. ``migrate``: long-write FIRST, hot-delete SECOND.
  3. ``migrate``: idempotent via explicit existence check on long key.
  4. ``migrate``: ts_ns derived from hot key SUFFIX, never from doc["ts"].
  5. ``migrate``: writes sentinel ``[0.0]*384`` + ``pending_vectorize=true``.
  6. ``vectorize_pending``: SET embedding FIRST, DEL flag SECOND.
  7. ``vectorize_pending``: discovery is **flag-based**, not
     missing-embedding-based.
  8. ``write_turn`` EXPIRE at HOT_TTL_SEC — memory module's job, not ours.
  9. Embedding dim 384 — locked in the index schema.
 10. ``_search_to_docs`` stamps ``__key`` for deterministic RRF dedup.
 11. ``search_hybrid`` is fail-soft per leg — Redis blip on one leg
     leaves the other leg's ranking intact.

NOTE on lazy connection: the store constructor takes a redis client
but does NOT issue any commands. The first command (and the idempotent
``FT.CREATE``) only fire when a ``_type:`` actually invokes.
``redis.asyncio.Redis.from_url(url)`` returns a client without opening
a TCP connection — that connection opens on first command.
"""
import asyncio
import json
import logging
from collections.abc import Iterable
from typing import Any

import redis.asyncio as aioredis
from redis.commands.search.field import (
    NumericField,
    TagField,
    TextField,
    VectorField,
)
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

from .sanitize import escape_redisearch_query

logger = logging.getLogger(__name__)

VECTOR_DIM = 384

# Memory module's hot-tier scope tag (ADR-012). We read here per the
# shared-substrate contract (memory's INSTALL.md); we never write.
HOT_SCOPE = "chat"

# This module's audit-tier scope tag (ADR-012).
AUDIT_SCOPE = "chat-audit"

# The canonical TAG-value used for the ``$.pending_vectorize``
# discovery flag. Stored as a JSON string in the audit doc; matched as
# ``@pending_vectorize:{1}`` in FT.SEARCH. See ``write_audit`` docstring
# for the rationale (RediSearch TAG values are strings; explicit
# encoding side-steps version-dependent boolean stringification).
PENDING_VECTORIZE_TAG_VALUE = "1"


class AuditStore:
    """Async audit-tier store, ``<pod>:<agent>``-scoped.

    Construction is I/O-free; no Redis commands fire. Call sites are:

      - :meth:`ensure_index` — idempotent ``FT.CREATE``. Cached after
        first success so repeat calls within a long-lived store are
        cheap. Intended to be invoked at the start of every
        ``_invoke`` body (idempotent) so the index exists when needed.
      - :meth:`write_audit` / :meth:`update_embedding` /
        :meth:`clear_pending_flag` — the migrate / vectorize write paths.
      - :meth:`migrate` (in ``sweep.py``) — composes the hot-tier read
        with the audit-tier write.
      - :meth:`vectorize_pending` (in ``vectorize.py``) — composes
        flagged-doc discovery with embedding writes.
      - :meth:`search_text` / :meth:`search_knn` / :meth:`search_hybrid`
        — the search path.

    **Dual-client support.** The store can hold two redis clients:

      - ``_audit_client`` — for the audit-tier (this module's
        ``<pod>:<agent>:chat-audit:*`` keyspace + RediSearch index).
        Targets the operator's vector-tier Redis. Used by
        :meth:`ensure_index`, :meth:`write_audit`,
        :meth:`update_embedding`, :meth:`clear_pending_flag`,
        :meth:`audit_exists`, and all search paths.
      - ``_hot_client`` — for memory module's hot-tier keyspace
        (``<pod>:<agent>:chat:*`` data keys + ``<pod>:<agent>:chat-index:*``
        ZSETs). Cross-module READ-ONLY per memory ``INSTALL.md`` § 7.
        Used by :meth:`read_hot`, :meth:`hot_ttl`, :meth:`delete_hot`,
        :meth:`list_hot_keys`, and the ZRANGEBYSCORE / ZREM calls
        :func:`sweep.migrate` issues directly.

    Construction supports both legacy single-client and new
    dual-client shapes:

      - ``AuditStore(client=c, ...)`` — colocated topology; the same
        client services both tiers. Default when ``hot_redis_url`` is omitted from the
        function config.
      - ``AuditStore(audit_client=ac, hot_client=hc, ...)`` — split
        topology; explicit per-tier clients (operator's per-agent
        vector Redis architectural pattern).

    Exactly one of the two construction shapes must be used. Mixing
    (``client=`` + ``audit_client=``) is rejected.
    """

    def __init__(
        self,
        pod: str,
        agent: str,
        *,
        client: aioredis.Redis | None = None,
        audit_client: aioredis.Redis | None = None,
        hot_client: aioredis.Redis | None = None,
    ):
        # Validate exactly one construction shape.
        legacy = client is not None
        split = audit_client is not None or hot_client is not None
        if legacy and split:
            raise TypeError(
                "AuditStore: pass either `client=` (legacy, colocated) "
                "OR `audit_client=`+`hot_client=` (split), not both"
            )
        if legacy:
            self._audit_client = client
            self._hot_client = client
        elif split:
            if audit_client is None or hot_client is None:
                raise TypeError(
                    "AuditStore: split-client shape requires both "
                    "`audit_client=` and `hot_client=`"
                )
            self._audit_client = audit_client
            self._hot_client = hot_client
        else:
            raise TypeError(
                "AuditStore: pass `client=` OR `audit_client=`+`hot_client=`"
            )
        self._pod = pod
        self._agent = agent
        # Cache for the idempotent FT.CREATE so repeat invocations don't
        # round-trip every time. Reset to False if you suspect the index
        # was dropped externally.
        self._index_ready = False

    # -- identity ---------------------------------------------------------

    @property
    def audit_client(self) -> aioredis.Redis:
        """The audit-tier client — ``<pod>:<agent>:chat-audit:*`` writes,
        FT.SEARCH, JSON.GET/SET. Targets the operator's vector Redis
        under a split topology."""
        return self._audit_client

    @property
    def hot_client(self) -> aioredis.Redis:
        """The hot-tier client — cross-module READ of memory's
        ``<pod>:<agent>:chat:*`` data keys and ``chat-index:*`` ZSETs.
        Targets the operator's hot Redis under a split topology
        (memory module's substrate, not this module's). Reads only;
        memory module owns writes."""
        return self._hot_client

    @property
    def client(self) -> aioredis.Redis:
        """Back-compat alias for ``audit_client``. Existing call sites
        that pre-date the dual-client split see the audit
        client by default."""
        return self._audit_client

    @property
    def pod(self) -> str:
        return self._pod

    @property
    def agent(self) -> str:
        return self._agent

    # -- key derivation ---------------------------------------------------

    @property
    def hot_prefix(self) -> str:
        return f"{self._pod}:{self._agent}:{HOT_SCOPE}:"

    @property
    def hot_index_prefix(self) -> str:
        return f"{self._pod}:{self._agent}:{HOT_SCOPE}-index:"

    @property
    def audit_prefix(self) -> str:
        return f"{self._pod}:{self._agent}:{AUDIT_SCOPE}:"

    @property
    def audit_index_name(self) -> str:
        return f"{self._pod}:{self._agent}:{AUDIT_SCOPE}:idx"

    def hot_key(self, chat_id: str, ts_ns: int) -> str:
        return f"{self.hot_prefix}{chat_id}:{ts_ns}"

    def hot_index_key(self, chat_id: str) -> str:
        return f"{self.hot_index_prefix}{chat_id}"

    def audit_key(self, chat_id: str, ts_ns: int) -> str:
        return f"{self.audit_prefix}{chat_id}:{ts_ns}"

    @staticmethod
    def chat_id_from_hot_key(hot_key: str) -> str:
        # Hot key shape: <pod>:<agent>:chat:<chat_id>:<ts_ns>
        # chat_id is the second-to-last segment.
        parts = hot_key.split(":")
        return parts[-2]

    @staticmethod
    def ts_ns_from_hot_key(hot_key: str) -> int:
        # Per LLD invariant #4: derive ts_ns from the key suffix, never
        # from the JSON doc's "ts" field (RedisJSON float round-trip can
        # shift it by ±1 µs vs. the writer's int(time.time_ns())).
        return int(hot_key.rsplit(":", 1)[-1])

    # -- index ------------------------------------------------------------

    async def ensure_index(self) -> None:
        """Idempotent ``FT.CREATE`` on the audit-tier index.

        Schema:

          - TAG ``$.chat_id``           — tenancy filter.
          - TAG ``$.role``              — role filter.
          - TAG ``$.pending_vectorize`` — enables FT.SEARCH-based
            discovery in :func:`vectorize_pending` (O(actual_pending)
            instead of O(audit_corpus_size) via SCAN).
          - NUMERIC SORTABLE ``$.ts``   — recency sort.
          - TEXT ``$.content``          — full-text search target.
          - VECTOR HNSW ``$.embedding`` — KNN target, 384d FLOAT32 COSINE.

        Called from each ``_invoke`` body before any FT.SEARCH /
        FT.SEARCH-shaped writes.

        First-call behavior:
          - Cached `_index_ready` flag short-circuits subsequent calls.
          - On ``Index already exists`` we set the flag and return.
          - Any other error propagates — substrate misconfiguration
            (no Redis Stack modules loaded, no permissions, etc.).
        """
        if self._index_ready:
            return
        schema = (
            TagField("$.chat_id", as_name="chat_id"),
            TagField("$.role", as_name="role"),
            TagField("$.pending_vectorize", as_name="pending_vectorize"),
            NumericField("$.ts", as_name="ts", sortable=True),
            TextField("$.content", as_name="content"),
            VectorField(
                "$.embedding",
                "HNSW",
                {
                    "TYPE": "FLOAT32",
                    "DIM": VECTOR_DIM,
                    "DISTANCE_METRIC": "COSINE",
                },
                as_name="embedding",
            ),
        )
        try:
            await self._audit_client.ft(self.audit_index_name).create_index(
                schema,
                definition=IndexDefinition(
                    prefix=[self.audit_prefix],
                    index_type=IndexType.JSON,
                ),
            )
            logger.info(
                "Created RediSearch index %s on prefix %s",
                self.audit_index_name, self.audit_prefix,
            )
        except Exception as e:
            if "Index already exists" in str(e):
                logger.debug(
                    "RediSearch index %s already exists",
                    self.audit_index_name,
                )
            else:
                raise
        self._index_ready = True

    # -- audit-tier writes ------------------------------------------------

    async def write_audit(
        self,
        chat_id: str,
        ts_ns: int,
        role: str,
        content: str,
        ts_seconds: float,
        embedding: list[float],
        pending_vectorize: bool,
    ) -> str:
        """``JSON.SET`` an audit doc; return the key.

        Stores the full document at ``<pod>:<agent>:chat-audit:<chat_id>:<ts_ns>``.
        Caller chooses the embedding (sentinel ``[0.0]*384`` for migrate;
        real vector for vectorize). ``pending_vectorize`` is the flag
        that drives :meth:`vectorize_pending`'s discovery loop (LLD
        invariant #7).

        **Pending vectorization encoding.** When ``pending_vectorize=True``,
        the field is written as the JSON string ``"1"`` rather than the
        Python ``True`` boolean. Rationale: RediSearch TAG values are
        strings. Writing an explicit string side-steps version-dependent
        boolean → TAG stringification (which can show up as ``"true"``,
        ``"1"``, or "not indexable" depending on RediSearch /
        ``redis-py`` versions). ``"1"`` is short, unambiguous, and
        lets vectorize_pending query against the literal ``{1}`` tag.
        See :data:`PENDING_VECTORIZE_TAG_VALUE` if a query needs the
        canonical string.
        """
        key = self.audit_key(chat_id, ts_ns)
        doc: dict[str, Any] = {
            "chat_id": chat_id,
            "role": role,
            "ts": ts_seconds,
            "content": content,
            "embedding": embedding,
        }
        if pending_vectorize:
            doc["pending_vectorize"] = PENDING_VECTORIZE_TAG_VALUE
        await self._audit_client.json().set(key, "$", doc)
        return key

    async def update_embedding(self, key: str, embedding: list[float]) -> None:
        """Replace ``$.embedding`` on an existing audit doc.

        Per LLD invariant #6 + #7, vectorize SETs the real embedding
        FIRST and only then DELs the ``pending_vectorize`` flag — a
        crash between the two re-runs in the next cycle (no corruption,
        wasted compute only).
        """
        await self._audit_client.json().set(key, "$.embedding", embedding)

    async def clear_pending_flag(self, key: str) -> None:
        """``JSON.DEL`` the ``$.pending_vectorize`` flag.

        Discovery is flag-based (LLD invariant #7) — once the flag is
        gone, the doc is invisible to :meth:`vectorize_pending`'s loop,
        regardless of the embedding's content.
        """
        # redis-py's RedisJSON binding exposes ``delete`` (lower-case)
        # which issues ``JSON.DEL``.
        await self._audit_client.json().delete(key, "$.pending_vectorize")

    async def audit_exists(self, key: str) -> bool:
        """Cheap idempotency check for migrate."""
        return bool(await self._audit_client.exists(key))

    # -- hot-tier reads (cross-module per memory INSTALL.md § 7) ----------

    async def list_hot_keys(self) -> list[str]:
        """SCAN all hot-tier keys for this ``<pod>:<agent>``.

        Uses ``SCAN MATCH <pod>:<agent>:chat:*`` — chat-index keys at
        ``<pod>:<agent>:chat-index:*`` are NOT matched because the
        glob ``chat:*`` requires a literal ``:`` after ``chat`` and
        ``chat-index`` has ``-`` instead.
        """
        pattern = f"{self.hot_prefix}*"
        keys: list[str] = []
        async for key in self._hot_client.scan_iter(match=pattern):
            keys.append(key)
        return keys

    async def read_hot(self, key: str) -> dict[str, Any] | None:
        """Read one hot-tier turn.

        Per memory's INSTALL.md § 7.2: hot tier is plain Redis STRING
        with a JSON-encoded UTF-8 payload. Use ``GET`` + ``json.loads``;
        do **not** use ``JSON.GET``. Returns ``None`` if the key
        TTL'd out between SCAN and GET (a normal race).
        """
        raw = await self._hot_client.get(key)
        if raw is None:
            return None
        # ``decode_responses=True`` on the client returns str; otherwise
        # bytes. Handle both.
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Hot-tier key %s carries non-JSON payload; skipping", key)
            return None

    async def hot_ttl(self, key: str) -> int:
        """``TTL`` on a hot-tier key. Returns -2 if the key is missing,
        -1 if it has no expiry, ≥0 otherwise."""
        return int(await self._hot_client.ttl(key))

    async def delete_hot(self, key: str) -> int:
        """``DEL`` one hot-tier key. Returns count deleted (0 or 1).

        Used by :meth:`migrate` after the audit write succeeds (LLD
        invariant #2: long-write FIRST, hot-delete SECOND).
        """
        return int(await self._hot_client.delete(key))

    # -- search path ------------------------------------------------------

    async def search_text(
        self,
        chat_id: str,
        query: str,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        """FT.SEARCH text mode, scoped to ``chat_id`` via TAG filter."""
        await self.ensure_index()
        cid = escape_redisearch_query(chat_id, escape_whitespace=True)
        q_text = escape_redisearch_query(query)
        if not q_text:
            return []
        q = (
            Query(f"@chat_id:{{{cid}}} @content:{q_text}")
            .sort_by("ts", asc=False)
            .paging(0, k)
        )
        return await self._search_to_docs(q)

    async def search_knn(
        self,
        chat_id: str,
        qvec_bytes: bytes,
        k: int = 10,
    ) -> list[dict[str, Any]]:
        """FT.SEARCH KNN mode over ``$.embedding`` (HNSW), tenancy-scoped."""
        await self.ensure_index()
        cid = escape_redisearch_query(chat_id, escape_whitespace=True)
        q = (
            Query(f"(@chat_id:{{{cid}}})=>[KNN {k} @embedding $vec AS d]")
            .sort_by("d")
            .dialect(2)
            .paging(0, k)
        )
        return await self._search_to_docs(q, query_params={"vec": qvec_bytes})

    async def search_hybrid(
        self,
        chat_id: str,
        query: str,
        embedder,  # FastEmbedEmbedder; typed loosely so this module
                   # doesn't import embedder at module load time.
        k: int = 10,
        rrf_k: int = 60,
        candidate_pool_multiplier: int = 2,
    ) -> list[dict[str, Any]]:
        """RRF fusion of text + KNN legs over the audit tier.

        Fail-soft per LLD invariant #11: a Redis blip on one leg leaves
        the other leg's ranking intact (RRF merge of ``[]`` with the
        surviving leg = the surviving leg).
        """
        await self.ensure_index()
        pool = max(k, k * candidate_pool_multiplier)

        # Embed the query once; reuse for KNN.
        try:
            vecs = await embedder.embed([query])
            qvec_bytes = _vec_to_bytes(vecs[0]) if vecs else None
        except Exception:
            logger.exception(
                "search_hybrid: embedder failed; semantic leg degraded "
                "to empty for chat_id=%s", chat_id,
            )
            qvec_bytes = None

        async def _safe_text() -> list[dict[str, Any]]:
            try:
                return await self.search_text(chat_id, query, k=pool)
            except Exception:
                logger.exception("search_hybrid: text leg raised; using []")
                return []

        async def _safe_knn() -> list[dict[str, Any]]:
            if qvec_bytes is None:
                return []
            try:
                return await self.search_knn(chat_id, qvec_bytes, k=pool)
            except Exception:
                logger.exception("search_hybrid: knn leg raised; using []")
                return []

        # Run both legs concurrently.
        text_hits, knn_hits = await asyncio.gather(_safe_text(), _safe_knn())
        return _rrf_merge(text_hits, knn_hits, k=k, rrf_k=rrf_k)

    # -- internal --------------------------------------------------------

    async def _search_to_docs(
        self, q: Query, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Run FT.SEARCH and pull each doc's JSON payload back as a dict.

        Per LLD invariant #10, each returned dict gets a ``__key`` field
        with the doc's Redis key — bullet-proofs RRF dedup downstream
        without re-deriving keys from ``doc["ts"]`` (which would
        re-introduce float round-trip precision risk).

        ``json`` is populated by ``redis-py`` for JSON-indexed docs; if
        absent (some redis-py versions / coverage gaps), fall back to
        ``JSON.GET <id>``.
        """
        res = await self._audit_client.ft(self.audit_index_name).search(q, **kwargs)
        out: list[dict[str, Any]] = []
        for d in res.docs:
            doc: dict[str, Any] | None = None
            payload = getattr(d, "json", None)
            if payload:
                if isinstance(payload, (bytes, bytearray)):
                    payload = payload.decode("utf-8")
                try:
                    doc = json.loads(payload)
                except (TypeError, ValueError):
                    doc = None
            if doc is None:
                doc = await self._audit_client.json().get(d.id)
            if doc is None:
                continue
            doc["__key"] = d.id
            out.append(doc)
        return out


# ---------------------------------------------------------------------------
# RRF merge — module-level helper (no store state).
# ---------------------------------------------------------------------------

def _rrf_merge(
    text_hits: list[dict[str, Any]],
    semantic_hits: list[dict[str, Any]],
    k: int,
    rrf_k: int,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion over two ranked lists.

    score(doc) = Σ over modes m where doc ∈ m's results:
                   1 / (rrf_k + rank_in_mode(doc))

    Dedup identifier is ``doc["__key"]`` (stamped by
    :meth:`AuditStore._search_to_docs`).

    Tie-break: rrf desc → ts desc (newer wins) → key asc (full
    determinism).
    """
    scores: dict[str, float] = {}
    docs: dict[str, dict[str, Any]] = {}
    for hits in (text_hits, semantic_hits):
        for rank_zero, doc in enumerate(hits):
            kid = doc["__key"]
            scores[kid] = scores.get(kid, 0.0) + 1.0 / (rrf_k + rank_zero + 1)
            docs[kid] = doc
    ranked = sorted(
        scores.items(),
        key=lambda kv: (
            -kv[1],                            # rrf desc
            -float(docs[kv[0]].get("ts", 0)),  # ts desc
            kv[0],                             # key asc
        ),
    )
    return [docs[kid] for kid, _ in ranked[:k]]


def _vec_to_bytes(vec: Iterable[float]) -> bytes:
    """Pack a list of floats into FLOAT32 little-endian bytes for KNN.

    RediSearch's HNSW with ``TYPE FLOAT32`` expects raw little-endian
    f32 bytes as the ``$vec`` query param. Doing this without numpy
    keeps the search path numpy-free (vector dim 384 → 1.5 KiB; struct
    is plenty fast for query-time encoding).
    """
    import struct
    return struct.pack(f"<{VECTOR_DIM}f", *vec)
