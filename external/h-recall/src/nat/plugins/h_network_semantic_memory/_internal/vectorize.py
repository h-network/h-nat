"""Vectorize-pending — replace sentinel embeddings on flagged audit docs.

One bounded, idempotent pass per call. Operator schedules cadence;
this module never sleeps, never loops indefinitely, never spawns
background work.

LLD-locked behavior (see ``store.py`` § "LLD invariants"):

  - **Discovery is flag-based** (LLD invariant #7) — ``$.pending_vectorize``
    is the canonical "needs real embedding" signal. Sentinel embeddings
    are still real embeddings to FT.SEARCH (text queries don't skip
    them), so the flag is what tells us they're sentinel-not-real.
  - **Write order**: SET embedding FIRST, DEL flag SECOND (LLD invariant
    #6). A crash between the two leaves a doc with the real embedding
    but the flag still set; next cycle re-embeds (wasted compute, no
    corruption) and re-clears the flag.
  - **Per-batch fail-soft**: an embedder error on one batch logs and
    skips that batch; the per-key flag persists so a future cycle
    retries.

Round-54 discovery shape — FT.SEARCH on the indexed flag (was: SCAN +
JSON.GET in round 38). Round 53's streaming-stress run hit a hard
ceiling at iter 2000 (audit_corpus 8000 docs) where ``vectorize`` cycle
p50 inflated 985 → 2876 ms (+192 %) — the SCAN cost was scaling
O(audit_corpus_size) per call. ``store.ensure_index()`` now declares a
TAG field on ``$.pending_vectorize`` (round-54 schema update); FT.SEARCH
``@pending_vectorize:{1}`` returns only docs that still carry the flag,
so the discovery cost becomes O(actual_pending_count) — typically
~migrate_rate * cycle_interval, regardless of how big the audit
corpus has grown.

The encoding ``"1"`` (JSON string, not Python ``True``) is round-54's
deliberate choice — see :func:`store.write_audit` docstring + the
``PENDING_VECTORIZE_TAG_VALUE`` module constant.

Returns:

    {
      "vectorized": int,  # docs successfully embedded + flag-cleared
      "scanned":    int,  # docs FT.SEARCH returned (post-flag-filter)
      "batches":    int,  # embedder.embed() invocations
    }

The ``scanned`` count semantics changed in round 54: previously this
counted every audit-tier key SCAN visited (most of which were already
vectorized and skipped in Python); now it counts only docs the index
already filtered down to "currently flagged." The two are NOT
directly comparable across rounds 38 ↔ 54.
"""
import logging
from typing import Any

from redis.commands.search.query import Query

from .embedder import FastEmbedEmbedder
from .store import AuditStore, PENDING_VECTORIZE_TAG_VALUE

logger = logging.getLogger(__name__)


# Default upper bound on docs returned per FT.SEARCH discovery query.
# RediSearch's LIMIT clause requires a number; ``max_per_cycle == 0``
# (the unbounded mode) maps to this. Sized generously so a backlog
# from a long sweep + slow vectorize run doesn't silently truncate
# the queue. If the backlog routinely exceeds this, surface a routing
# question (operator may want to size up or paginate).
DEFAULT_PAGING_LIMIT = 10_000


async def vectorize_pending(
    store: AuditStore,
    embedder: FastEmbedEmbedder,
    *,
    batch_size: int = 64,
    max_per_cycle: int = 0,
) -> dict[str, int]:
    """Run one bounded vectorize pass.

    Discovers audit docs with ``$.pending_vectorize`` set via
    ``FT.SEARCH @pending_vectorize:{1}`` against the audit-tier index,
    then embeds in batches and writes the real vectors back.

    ``batch_size`` is the embedder batch size (fastembed is much faster
    on batches than singletons). ``max_per_cycle == 0`` means unbounded
    (capped by :data:`DEFAULT_PAGING_LIMIT` per call).
    """
    await store.ensure_index()

    page_limit = max_per_cycle if max_per_cycle > 0 else DEFAULT_PAGING_LIMIT

    # FT.SEARCH discovery query. The TAG value matches what
    # ``write_audit`` writes when ``pending_vectorize=True`` — see
    # store.PENDING_VECTORIZE_TAG_VALUE for the encoding choice.
    # Sort by ``ts`` ascending so older pending docs vectorize first
    # (FIFO over the backlog) — a small fairness property when a
    # backlog accumulates.
    query = (
        Query(f"@pending_vectorize:{{{PENDING_VECTORIZE_TAG_VALUE}}}")
        .sort_by("ts", asc=True)
        .paging(0, page_limit)
    )

    res = await store.audit_client.ft(store.audit_index_name).search(query)

    pending: list[tuple[str, str]] = []  # (key, content)
    vectorized = 0
    scanned = 0
    batches = 0

    async def _flush() -> int:
        nonlocal batches
        if not pending:
            return 0
        batches += 1
        try:
            contents = [c for _, c in pending]
            vectors = await embedder.embed(contents)
        except Exception:
            logger.exception(
                "vectorize: embedder.embed failed for batch of %d; "
                "leaving flags in place for retry",
                len(pending),
            )
            pending.clear()
            return 0
        written = 0
        for (k, _), vec in zip(pending, vectors):
            try:
                # LLD invariant #6: SET embedding FIRST, DEL flag SECOND.
                await store.update_embedding(k, vec)
                await store.clear_pending_flag(k)
                written += 1
            except Exception:
                logger.exception(
                    "vectorize: write failed for %s; leaving flag set",
                    k,
                )
        pending.clear()
        return written

    for d in res.docs:
        scanned += 1
        # Reuse the json-payload extraction shape from
        # ``store._search_to_docs``: prefer the inline ``json``
        # attribute (RediSearch returns JSON-indexed payloads here in
        # current redis-py); fall back to a direct ``JSON.GET`` if not.
        doc: Any = None
        payload = getattr(d, "json", None)
        if payload:
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8")
            try:
                import json as _json
                doc = _json.loads(payload)
            except (TypeError, ValueError):
                doc = None
        if doc is None:
            try:
                doc = await store.audit_client.json().get(d.id)
            except Exception:
                logger.exception(
                    "vectorize: JSON.GET fallback failed for %s; skipping",
                    d.id,
                )
                continue
        # ``redis-py`` JSON.GET returns the doc as parsed JSON; for a
        # ``$``-rooted set, the doc IS the dict we wrote. Some redis-py
        # versions return ``[doc]`` for ``$``-rooted gets; handle both.
        if isinstance(doc, list) and doc:
            doc = doc[0]
        if not isinstance(doc, dict):
            continue
        content = doc.get("content", "")
        if not content:
            # Nothing to embed; skip rather than silently embedding empty.
            # The flag persists so an admin who fixes the doc can pick it
            # up next cycle.
            continue
        pending.append((d.id, content))
        if len(pending) >= batch_size:
            vectorized += await _flush()
            if max_per_cycle and vectorized >= max_per_cycle:
                break

    # Final flush of the last partial batch.
    if not (max_per_cycle and vectorized >= max_per_cycle):
        vectorized += await _flush()

    if vectorized:
        logger.info(
            "vectorize cycle: vectorized=%d scanned=%d batches=%d",
            vectorized, scanned, batches,
        )

    return {
        "vectorized": vectorized,
        "scanned": scanned,
        "batches": batches,
    }
