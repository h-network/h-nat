"""Hot → audit migration (a.k.a. ``migrate``) — sweep orchestration.

One bounded, idempotent pass per call. Operator schedules cadence via
their workflow YAML / Python harness; this module never sleeps, never
loops indefinitely, never spawns a background task. The
``h_semantic_sweep`` ``_type:`` invokes :func:`migrate` once per
invocation.

LLD-locked behavior (see ``store.py`` § "LLD invariants"):

  - Each migrated turn is processed individually: long-write FIRST,
    hot-delete SECOND. A crash between the two leaves the entry in
    both tiers briefly; the next call's idempotency check
    (``audit_exists``) skips the long-write and just removes the
    stale hot copy.
  - ``ts_ns`` is derived from the hot key suffix (canonical), never
    from ``doc["ts"]`` (which is RedisJSON float round-trip lossy).
  - The audit doc carries a sentinel ``[0.0]*384`` embedding plus
    ``$.pending_vectorize: "1"`` (round-54 encoding) — vectorize
    fills the real vector asynchronously; the sentinel keeps the
    doc visible to FT.SEARCH text queries immediately.

Round-56 discovery shape — chat-index ZSET driven (was: SCAN +
TTL-check over the full hot-tier prefix in round 38). Round 55's
streaming-stress run found the next ceiling at iter 4000 with
sweep cycle p50 growing 62.9 → 169.4 → 279.0 → 392.6 ms across four
windows — the SCAN cost was scaling O(total DB keys) per call. This
round refactors discovery to ZRANGEBYSCORE on memory module's
``<pod>:<agent>:chat-index:<chat_id>`` per-chat ZSETs (cross-module
read per memory ``INSTALL.md`` § 7.2 + this module's INSTALL.md § 6).
The cost becomes O(sum_of_per_chat_hot_counts) — typically
O(num_chat_ids_in_input × few_hot_per_chat), independent of audit
corpus size.

Consumer-tracks-everything posture (per
`feedback_underlay_enables_consumer_tracks.md`): the consumer
(workflow / harness / overlay) tracks which chats have hot turns
needing migration; semantic-memory accepts that list as input.
``chat_ids=[]`` = explicit no-op, NOT a fallback to global SCAN.

Per-call bounds:

  - ``chat_ids`` — the list of chats to sweep. Consumer-tracked.
  - ``migration_threshold_sec`` (round-56 semantic) — turns whose
    ``ts_ns`` is older than ``now - migration_threshold_sec`` are
    migrated. (Round 38 used "remaining TTL ≤ threshold" which
    required a per-key TTL round-trip; round 56's age-based
    semantic uses the chat-index ZSET score directly, removing
    that round-trip. The two semantics agree when all writers use
    the same per-call TTL — a constraint operators already follow
    via ``ttl_seconds_max``.)
  - ``max_per_cycle`` — hard cap on docs processed; 0 = unbounded.

Returns:

    {
      "migrated":         int,  # docs newly moved hot→audit this call
      "skipped_existing": int,  # idempotency hits (already in audit)
      "skipped_fresh":    int,  # always 0 in round 56 (kept for shape
                                # parity; ZRANGEBYSCORE pre-filters by
                                # age — there's no "fresh-and-skipped"
                                # case here)
      "scanned":          int,  # turn_keys enumerated via ZRANGEBYSCORE
                                # across all chat_ids
    }

The ``scanned`` semantics changed across rounds 38 → 56 (sister to
round 54's vectorize change): now it counts turn_keys that the
chat-index ZSET filtered down to "old enough." Not directly
comparable to round-38's count.

Lazy ZSET cleanup: when this module migrates a turn (or finds the
data already gone), it ``ZREM``'s the corresponding member from the
chat-index ZSET. This is the read-side lazy cleanup memory's
``INSTALL.md`` § 7.3 documents. The race against another reader
doing the same ZREM is safe — ``ZREM`` is idempotent.
"""
import logging
import time
from typing import Any

from .store import VECTOR_DIM, AuditStore

logger = logging.getLogger(__name__)


# Sentinel embedding written to audit on migrate; vectorize replaces
# this with a real fastembed vector and DELs the pending flag.
SENTINEL_EMBEDDING: list[float] = [0.0] * VECTOR_DIM


async def migrate(
    store: AuditStore,
    chat_ids: list[str],
    *,
    migration_threshold_sec: int,
    max_per_cycle: int = 0,
) -> dict[str, int]:
    """Run one bounded migration pass over ``chat_ids``.

    See module docstring for the full contract. Empty ``chat_ids`` =
    no-op (deliberate; consumer-tracks-everything).
    """
    # Empty chat_ids: no-op. Return zero counts and skip everything —
    # including ``ensure_index`` (no audit writes will happen).
    if not chat_ids:
        return {
            "migrated": 0,
            "skipped_existing": 0,
            "skipped_fresh": 0,
            "scanned": 0,
        }

    # Ensure the audit index exists before we start writing audit docs;
    # otherwise the first ``JSON.SET`` lands but the doc isn't yet
    # discoverable to ``vectorize_pending``'s flag-based discovery.
    await store.ensure_index()

    # Round-56 cutoff: turn_keys with score (ts_ns) ≤ cutoff_ns are old
    # enough to migrate. ``time.time_ns()`` is the same monotonic-ish
    # clock memory's ``BoundedBufferStore.write_turn`` uses for ts_ns
    # (per memory's ``memory.py`` ``_next_ts_ns``), so the comparison
    # is apples-to-apples.
    now_ns = time.time_ns()
    cutoff_ns = now_ns - migration_threshold_sec * 1_000_000_000

    migrated = 0
    skipped_existing = 0
    skipped_fresh = 0  # always 0 in round 56; kept for return-shape parity
    scanned = 0

    for chat_id in chat_ids:
        if max_per_cycle and migrated >= max_per_cycle:
            break

        chat_index_key = store.hot_index_key(chat_id)

        # ZRANGEBYSCORE for "old enough to migrate" — score range [0, cutoff_ns].
        # Score is ts_ns per memory's chat-index write contract
        # (INSTALL.md § 7.1).
        try:
            turn_keys = await store.hot_client.zrangebyscore(
                chat_index_key, 0, cutoff_ns,
            )
        except Exception:
            logger.exception(
                "migrate: ZRANGEBYSCORE failed for chat_index=%s; "
                "skipping this chat_id this cycle",
                chat_index_key,
            )
            continue

        for turn_key in turn_keys:
            scanned += 1
            if max_per_cycle and migrated >= max_per_cycle:
                break

            doc = await store.read_hot(turn_key)
            if doc is None:
                # Stale ZSET entry — data key TTL'd out but ZSET still
                # holds the member. Lazy cleanup per memory § 7.3.
                try:
                    await store.hot_client.zrem(chat_index_key, turn_key)
                except Exception:
                    logger.exception(
                        "migrate: lazy ZREM of stale member %s failed; "
                        "memory's per-write ZREMRANGEBYSCORE will eventually "
                        "clean up", turn_key,
                    )
                continue

            # LLD invariant #4: derive ts_ns from the key suffix; chat_id
            # from the second-to-last segment. Memory's hot-tier JSON
            # payload doesn't include chat_id (it lives in the key) so we
            # MUST extract from the key.
            try:
                cid = AuditStore.chat_id_from_hot_key(turn_key)
                ts_ns = AuditStore.ts_ns_from_hot_key(turn_key)
            except (IndexError, ValueError):
                logger.warning(
                    "migrate: malformed hot key %s in chat-index %s; skipping",
                    turn_key, chat_index_key,
                )
                continue

            long_key = store.audit_key(cid, ts_ns)
            if await store.audit_exists(long_key):
                # Already migrated by a previous (crashed?) call.
                # LLD invariant #2/#3: drop the hot copy + clean up the
                # now-stale ZSET entry; move on.
                try:
                    await store.delete_hot(turn_key)
                except Exception:
                    logger.exception(
                        "migrate: cleanup hot-delete for already-migrated "
                        "%s failed; will retry next cycle", turn_key,
                    )
                try:
                    await store.hot_client.zrem(chat_index_key, turn_key)
                except Exception:
                    pass
                skipped_existing += 1
                continue

            # LLD invariant #2: long-write FIRST.
            try:
                await store.write_audit(
                    chat_id=cid,
                    ts_ns=ts_ns,
                    role=doc.get("role", ""),
                    content=doc.get("content", ""),
                    ts_seconds=float(doc.get("ts", 0)),
                    embedding=SENTINEL_EMBEDDING,
                    pending_vectorize=True,
                )
            except Exception:
                logger.exception(
                    "migrate: write_audit failed for %s; leaving hot in place",
                    turn_key,
                )
                continue

            # Hot-delete SECOND, then lazy-clean the chat-index entry
            # (the ZSET member now points at a deleted key — no value to
            # any reader). Failure here is non-fatal: next cycle's
            # ``audit_exists`` check picks it up via the idempotency path.
            try:
                await store.delete_hot(turn_key)
            except Exception:
                logger.exception(
                    "migrate: hot-delete failed for %s; audit copy lives on, "
                    "next cycle will re-detect via existence check + clean up",
                    turn_key,
                )
            try:
                await store.hot_client.zrem(chat_index_key, turn_key)
            except Exception:
                # Index cleanup is best-effort; memory's per-write
                # ZREMRANGEBYSCORE bounds the ZSET against ttl_seconds_max
                # so a missed ZREM doesn't leak forever.
                pass

            migrated += 1

    if migrated or skipped_existing:
        logger.info(
            "migrate cycle: chats=%d migrated=%d skipped_existing=%d "
            "scanned=%d",
            len(chat_ids), migrated, skipped_existing, scanned,
        )

    return {
        "migrated": migrated,
        "skipped_existing": skipped_existing,
        "skipped_fresh": skipped_fresh,
        "scanned": scanned,
    }
