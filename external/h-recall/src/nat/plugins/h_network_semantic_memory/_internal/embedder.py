"""Embedder wrapper.

The audit-tier RediSearch index schema locks the embedding dimension at
``384`` (``HNSW`` over ``$.embedding`` ``FLOAT32`` ``COSINE``). The
default model that produces those vectors is fastembed's MiniLM-L6,
which matches the dim and is the carried-over choice from the
h-sessions reference implementation.

Lazy-load posture per round-38 announcement § 2.3 / § 3:

  - ``FastEmbedEmbedder(model_name=...)`` constructs the wrapper object
    only — no fastembed import, no model weights. Safe to call from a
    NAT function builder at workflow-build time.
  - ``await embedder.embed(...)`` (the first invocation) imports
    fastembed and downloads / loads the model (~70 MB MiniLM cache).
    Subsequent invocations reuse the loaded model.

The model load happens inside :func:`asyncio.to_thread` so the NAT
event loop stays responsive during the cold-start. Subsequent embed
calls are also threaded — fastembed is CPU-bound, never asyncio-aware.

No ``Embedder`` Protocol shim this round (announcement § 2.3). The
class is concrete; if a future consumer asks for a runtime-swappable
embedder, that's an index-rebuild operation (the dim is schema-locked)
and lands as its own round.
"""
import asyncio
import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

VECTOR_DIM = 384
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class FastEmbedEmbedder:
    """fastembed-backed embedder. Lazy import + lazy model load.

    The fastembed ``TextEmbedding`` constructor downloads the model on
    first use (~70 MB for MiniLM, cached under ``~/.cache/fastembed``
    by default). Holding off until first :meth:`embed` keeps the NAT
    builder cold-start cheap.

    Thread-safety note: model construction is single-flight via an
    :class:`asyncio.Lock`; concurrent first-call invocations from
    different coroutines won't race to load twice.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self._model_name = model_name
        self._model = None  # type: ignore[var-annotated]
        self._lock = asyncio.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        return VECTOR_DIM

    async def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        async with self._lock:
            if self._model is not None:
                return
            # Heavy import deferred to first call so that the
            # ``h_semantic_sweep`` builder (which never embeds) and the
            # ``nat info components`` / build-check paths never pay the
            # fastembed cost.
            def _load():
                from fastembed import TextEmbedding  # type: ignore
                logger.info(
                    "Loading fastembed model %s (dim=%d) — first use",
                    self._model_name, VECTOR_DIM,
                )
                model = TextEmbedding(model_name=self._model_name)
                logger.info("fastembed model %s ready", self._model_name)
                return model

            self._model = await asyncio.to_thread(_load)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        # ``self._model.embed(...)`` is a generator yielding numpy arrays;
        # materialize to plain Python floats so the result is
        # JSON-serializable (RedisJSON ``$.embedding`` storage shape) and
        # numpy-free for callers that don't import numpy.
        return [list(map(float, v)) for v in self._model.embed(texts)]

    async def embed(self, texts: Iterable[str]) -> list[list[float]]:
        """Embed an iterable of texts. Returns one vector per input.

        Empty iterable returns ``[]`` without loading the model.
        """
        materialized = list(texts)
        if not materialized:
            return []
        await self._ensure_loaded()
        return await asyncio.to_thread(self._embed_sync, materialized)
