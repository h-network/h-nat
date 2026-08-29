"""NAT registration for h-network-semantic-memory.

Three workflow-callable function ``_type:``s — operator-controlled
long-term memory over Redis Stack (RediSearch + RedisJSON):

  - ``_type: h_semantic_search``     hybrid retrieval (text + vector via RRF)
  - ``_type: h_semantic_sweep``      one bounded hot→audit migration pass
  - ``_type: h_semantic_vectorize``  one bounded embed-pending pass

Per round-38 announcement § 2.1 / § 3 (lazy-builder requirement):

  - The redis client is constructed at builder time but does NOT issue
    any Redis commands. ``redis.asyncio.Redis.from_url(url)`` opens no
    TCP; the connection lazily opens on the first command.
  - The fastembed model is constructed (wrapper object only) at
    builder time; the model file is loaded on first :meth:`embed`.
  - The RediSearch index is created lazily inside the function bodies
    via ``store.ensure_index()`` (idempotent).

Round-26 lessons preserved:

  - **NO ``from __future__ import annotations``** at module top — NAT
    1.6.0's ``FunctionInfo.from_fn`` introspects via
    ``typing.get_type_hints`` against an internally-generated wrapper;
    PEP 563 stringified forward-refs resolve in NAT's namespace, not
    ours, and ``NameError`` on input models.
  - **``str → PydanticModel`` converters** registered alongside each
    function so ``nat run --input '<JSON>'`` (CLI) and any future
    ``nat serve``-style structured-input front-ends bridge ``str`` to
    the input dataclass.
  - **Output converter for non-``str`` returns** so NAT's
    ``runner.result(to_type=str)`` (used by ``nat run`` console output)
    can stringify a ``dict``-returning function.
  - **``ConfigDict(extra="forbid")``** on every input model — JSON-key
    typos surface at parse time, not silently as ``None``.
"""
# NOTE: no `from __future__ import annotations` — round-26 fix-up #1.

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any, Optional

import redis.asyncio as aioredis
from pydantic import BaseModel, ConfigDict, Field

from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig

from ._internal.embedder import FastEmbedEmbedder
from ._internal.store import AuditStore
from ._internal.sweep import migrate
from ._internal.vectorize import vectorize_pending

logger = logging.getLogger(__name__)

# ADR-012 ``<pod>:<agent>`` primitive — single token regex per segment.
_POD_AGENT_TOKEN_PATTERN = r"^[A-Za-z0-9_-]+$"


# ---------------------------------------------------------------------------
# Output converter — used by all three functions to render dict / list output
# as JSON when NAT's CLI front-end coerces to ``str``.
# ---------------------------------------------------------------------------

def _obj_to_str(value: object) -> str:
    """JSON-stringify a dict / list / int return value for ``nat run``."""
    return json.dumps(value, default=str)


# ===========================================================================
# h_semantic_search — hybrid retrieval (text + vector via RRF)
# ===========================================================================

class HSemanticSearchConfig(FunctionBaseConfig, name="h_semantic_search"):
    """Workflow-config for ``_type: h_semantic_search``.

    Per-workflow operator settings (Redis URL, ``<pod>:<agent>``,
    embedding-model name, RRF tuning). Per-call args (``chat_id``,
    ``query``, ``top_k``, ``mode``) live on
    :class:`SemanticSearchInput`.
    """
    redis_url: str = Field(
        default="redis://localhost:6379",
        description=(
            "Redis URL — must point at a Redis Stack instance with "
            "RediSearch + RedisJSON. Defaults to localhost."
        ),
    )
    pod: str = Field(
        ...,
        pattern=_POD_AGENT_TOKEN_PATTERN,
        description="ADR-012 first segment of <pod>:<agent>. Required.",
    )
    agent: str = Field(
        ...,
        pattern=_POD_AGENT_TOKEN_PATTERN,
        description="ADR-012 second segment of <pod>:<agent>. Required.",
    )
    embed_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        description=(
            "fastembed model name. Schema-locked at 384d (round-38); "
            "swapping models is an index-rebuild operation."
        ),
    )
    rrf_k: int = Field(
        default=60, ge=1,
        description="RRF smoothing constant; larger = flatter fusion.",
    )
    candidate_pool_multiplier: int = Field(
        default=2, ge=1,
        description=(
            "Hybrid pulls top_k * this from each leg before RRF; "
            "raises recall at the cost of more docs to fuse."
        ),
    )


class SemanticSearchInput(BaseModel):
    """Per-call input for ``h_semantic_search``."""
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=100)
    mode: str = Field(default="hybrid")  # text | semantic | hybrid


def _json_str_to_search_input(value: str) -> SemanticSearchInput:
    return SemanticSearchInput.model_validate_json(value)


@register_function(config_type=HSemanticSearchConfig)
async def h_semantic_search(
    config: HSemanticSearchConfig,
    builder: Builder,
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the ``h_semantic_search`` NAT function (lazy)."""
    # Construct the client — NO ping. ``from_url`` is I/O-free.
    client = aioredis.Redis.from_url(config.redis_url, decode_responses=True)
    try:
        store = AuditStore(client=client, pod=config.pod, agent=config.agent)
        embedder = FastEmbedEmbedder(model_name=config.embed_model)
        logger.info(
            "h_semantic_search built (lazy): redis_url=%s pod=%s agent=%s "
            "embed_model=%s rrf_k=%d",
            config.redis_url, config.pod, config.agent,
            config.embed_model, config.rrf_k,
        )

        async def _invoke(input: SemanticSearchInput) -> list[dict[str, Any]]:
            mode = input.mode
            if mode not in ("text", "semantic", "hybrid"):
                raise ValueError(
                    f"mode must be one of 'text'/'semantic'/'hybrid'; got {mode!r}"
                )
            if mode == "text":
                return await store.search_text(
                    input.chat_id, input.query, k=input.top_k,
                )
            if mode == "semantic":
                vecs = await embedder.embed([input.query])
                if not vecs:
                    return []
                from ._internal.store import _vec_to_bytes
                return await store.search_knn(
                    input.chat_id, _vec_to_bytes(vecs[0]), k=input.top_k,
                )
            # hybrid
            return await store.search_hybrid(
                input.chat_id, input.query, embedder,
                k=input.top_k,
                rrf_k=config.rrf_k,
                candidate_pool_multiplier=config.candidate_pool_multiplier,
            )

        yield FunctionInfo.from_fn(
            _invoke,
            converters=[_json_str_to_search_input, _obj_to_str],
            description=(
                "Hybrid retrieval over the long (audit) tier. mode = "
                "text | semantic | hybrid (RRF fusion). Returns ranked "
                "docs scoped to chat_id."
            ),
        )
    finally:
        await client.aclose()


# ===========================================================================
# h_semantic_sweep — one bounded hot→audit migration pass
# ===========================================================================

class HSemanticSweepConfig(FunctionBaseConfig, name="h_semantic_sweep"):
    """Workflow-config for ``_type: h_semantic_sweep``.

    Operator-scheduled — call this on whatever cadence fits the
    deployment. One call = one bounded pass; idempotent.
    """
    redis_url: str = Field(
        default="redis://localhost:6379",
        description=(
            "Audit-tier Redis URL. Points at the operator's vector-tier "
            "Redis Stack instance under a split topology (per round 64+ "
            "per-agent vector Redis pattern), or at a colocated Redis "
            "that serves both tiers under a single-instance topology. "
            "All audit-tier writes (chat-audit:* JSON.SET, FT.CREATE on "
            "chat-audit:idx) land here."
        ),
    )
    hot_redis_url: Optional[str] = Field(
        default=None,
        description=(
            "Hot-tier Redis URL — points at memory module's substrate "
            "(``<pod>:<agent>:chat:*`` data keys + ``chat-index:*`` ZSETs). "
            "Used by sweep for cross-module READ of the chat-index ZSET "
            "(ZRANGEBYSCORE), hot-key reads (GET), hot-key deletes (DEL), "
            "and the lazy ZREM of stale members. "
            "If ``None`` (default), sweep uses ``redis_url`` for both "
            "tiers — colocated topology, backwards-compatible with "
            "round-38 / round-54 / round-56 single-URL workflows. "
            "Set explicitly to enable the round-67 split topology."
        ),
    )
    pod: str = Field(..., pattern=_POD_AGENT_TOKEN_PATTERN)
    agent: str = Field(..., pattern=_POD_AGENT_TOKEN_PATTERN)
    migration_threshold_sec: int = Field(
        default=18000, ge=1,  # 5h — h-sessions LLD default
        description=(
            "Age threshold for migration (round-56 semantic): hot turns whose "
            "ts_ns is older than ``now - migration_threshold_sec`` are "
            "migrated to the audit tier. Constraint: "
            "sweep_interval ≤ migration_threshold_sec ≤ hot_ttl_sec "
            "(operator-policed; documented in INSTALL.md)."
        ),
    )
    max_per_cycle: int = Field(
        default=0, ge=0,
        description=(
            "Cap on docs migrated per call; 0 = unbounded. "
            "Use to bound work when many chats have large backlogs."
        ),
    )


class SemanticSweepInput(BaseModel):
    """Per-call input for ``h_semantic_sweep`` (round-56 shape).

    Round 56 changed sweep from "walk the whole tenant" to "sweep the
    chats the consumer tracks." The consumer (workflow / harness /
    overlay) is responsible for tracking which chats have hot turns
    needing migration; sweep no longer enumerates globally.

    - ``chat_ids=[]`` (or omitted) → no-op. Returns zero counts.
      This is the deliberate end of the global-enumeration path; not a
      fallback to SCAN.
    - ``chat_ids=["c1","c2"]`` → sweep each chat via its
      ``<pod>:<agent>:chat-index:<chat_id>`` ZSET (memory module's
      cross-module read contract per ``INSTALL.md`` § 6).
    """
    model_config = ConfigDict(extra="forbid")

    chat_ids: list[str] = Field(
        default_factory=list,
        description=(
            "List of chat_ids to sweep this call. Empty list = no-op "
            "(no global enumeration; consumer-tracks-everything per "
            "feedback_underlay_enables_consumer_tracks.md)."
        ),
    )


def _json_str_to_sweep_input(value: str) -> SemanticSweepInput:
    # Allow ``nat run --input '{}'`` ; an empty / missing payload also
    # validates to the empty (no-op) model.
    if not value or not value.strip():
        return SemanticSweepInput()
    return SemanticSweepInput.model_validate_json(value)


@register_function(config_type=HSemanticSweepConfig)
async def h_semantic_sweep(
    config: HSemanticSweepConfig,
    builder: Builder,
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the ``h_semantic_sweep`` NAT function (lazy).

    Round 67 — dual-Redis support: ``hot_redis_url`` (new, optional)
    routes chat-index ZRANGEBYSCORE / hot-key reads / ZREM-on-success
    at memory module's substrate; ``redis_url`` (existing field,
    semantic re-pointed) targets the audit-tier (vector) Redis. If
    ``hot_redis_url`` is omitted, both clients point at ``redis_url``
    — colocated topology, backwards-compatible with round-38 /
    round-54 / round-56 workflows.
    """
    audit_url = config.redis_url
    hot_url = config.hot_redis_url or config.redis_url
    colocated = (hot_url == audit_url)

    audit_client = aioredis.Redis.from_url(audit_url, decode_responses=True)
    # Aliasing intent for colocated: a single connection pool is enough;
    # passing the same client twice keeps the round-trip pool shared and
    # avoids accidental double-close on teardown.
    if colocated:
        hot_client = audit_client
    else:
        hot_client = aioredis.Redis.from_url(hot_url, decode_responses=True)

    try:
        store = AuditStore(
            audit_client=audit_client,
            hot_client=hot_client,
            pod=config.pod,
            agent=config.agent,
        )
        logger.info(
            "h_semantic_sweep built (lazy): redis_url=%s hot_redis_url=%s "
            "topology=%s pod=%s agent=%s "
            "migration_threshold_sec=%d max_per_cycle=%d",
            audit_url, hot_url, "colocated" if colocated else "split",
            config.pod, config.agent,
            config.migration_threshold_sec, config.max_per_cycle,
        )

        async def _invoke(input: SemanticSweepInput) -> dict[str, int]:
            return await migrate(
                store,
                chat_ids=input.chat_ids,
                migration_threshold_sec=config.migration_threshold_sec,
                max_per_cycle=config.max_per_cycle,
            )

        yield FunctionInfo.from_fn(
            _invoke,
            converters=[_json_str_to_sweep_input, _obj_to_str],
            description=(
                "One bounded hot→audit migration pass over the supplied "
                "chat_ids. Empty chat_ids = no-op. Operator-scheduled. "
                "Returns counts: migrated, skipped_existing, skipped_fresh, "
                "scanned."
            ),
        )
    finally:
        await audit_client.aclose()
        if not colocated:
            await hot_client.aclose()


# ===========================================================================
# h_semantic_vectorize — one bounded embed-pending pass
# ===========================================================================

class HSemanticVectorizeConfig(FunctionBaseConfig, name="h_semantic_vectorize"):
    """Workflow-config for ``_type: h_semantic_vectorize``."""
    redis_url: str = Field(default="redis://localhost:6379")
    pod: str = Field(..., pattern=_POD_AGENT_TOKEN_PATTERN)
    agent: str = Field(..., pattern=_POD_AGENT_TOKEN_PATTERN)
    embed_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        description="Schema-locked at 384d.",
    )
    batch_size: int = Field(
        default=64, ge=1,
        description="Embedder batch size; fastembed batches efficiently.",
    )
    max_per_cycle: int = Field(
        default=0, ge=0,
        description="Cap on docs vectorized per call; 0 = unbounded.",
    )


class SemanticVectorizeInput(BaseModel):
    """Per-call input for ``h_semantic_vectorize``.

    Optional per-call overrides for batch_size / max_per_cycle so a
    Python-harness caller can tune at invoke time without rebuilding
    the workflow. ``None`` falls back to the config defaults.
    """
    model_config = ConfigDict(extra="forbid")
    batch_size: int | None = Field(default=None, ge=1)
    max_per_cycle: int | None = Field(default=None, ge=0)


def _json_str_to_vectorize_input(value: str) -> SemanticVectorizeInput:
    if not value or not value.strip():
        return SemanticVectorizeInput()
    return SemanticVectorizeInput.model_validate_json(value)


@register_function(config_type=HSemanticVectorizeConfig)
async def h_semantic_vectorize(
    config: HSemanticVectorizeConfig,
    builder: Builder,
) -> AsyncGenerator[FunctionInfo, None]:
    """Build the ``h_semantic_vectorize`` NAT function (lazy)."""
    client = aioredis.Redis.from_url(config.redis_url, decode_responses=True)
    try:
        store = AuditStore(client=client, pod=config.pod, agent=config.agent)
        embedder = FastEmbedEmbedder(model_name=config.embed_model)
        logger.info(
            "h_semantic_vectorize built (lazy): redis_url=%s pod=%s agent=%s "
            "embed_model=%s batch_size=%d max_per_cycle=%d",
            config.redis_url, config.pod, config.agent,
            config.embed_model, config.batch_size, config.max_per_cycle,
        )

        async def _invoke(input: SemanticVectorizeInput) -> dict[str, int]:
            batch_size = input.batch_size or config.batch_size
            max_per_cycle = (
                input.max_per_cycle if input.max_per_cycle is not None
                else config.max_per_cycle
            )
            return await vectorize_pending(
                store, embedder,
                batch_size=batch_size,
                max_per_cycle=max_per_cycle,
            )

        yield FunctionInfo.from_fn(
            _invoke,
            converters=[_json_str_to_vectorize_input, _obj_to_str],
            description=(
                "One bounded embed-pending pass. Discovers audit docs "
                "flagged pending_vectorize=true, embeds, replaces sentinel, "
                "clears flag. Returns counts: vectorized, scanned, batches."
            ),
        )
    finally:
        await client.aclose()
