"""Unit tests for RRF fusion and search helper logic."""

from nat.plugins.h_network_semantic_memory._internal.store import (
    VECTOR_DIM,
    _rrf_merge,
    _vec_to_bytes,
)


def _make_doc(chat_id: str, ts_ns: int, ts: float, content: str) -> dict:
    return {
        "__key": f"pod:agent:chat-audit:{chat_id}:{ts_ns}",
        "chat_id": chat_id,
        "role": "user",
        "ts": ts,
        "content": content,
    }


def test_rrf_merge_empty():
    assert _rrf_merge([], [], k=10, rrf_k=60) == []


def test_rrf_merge_one_leg_empty():
    docs = [_make_doc("c1", i, 100.0 + i, f"doc {i}") for i in range(3)]
    merged = _rrf_merge(docs, [], k=10, rrf_k=60)
    assert [d["__key"] for d in merged] == [d["__key"] for d in docs]


def test_rrf_merge_overlap_scored_higher():
    overlap = _make_doc("c1", 1, 100.0, "shared doc")
    text_only = _make_doc("c1", 2, 50.0, "text only")
    sem_only = _make_doc("c1", 3, 75.0, "sem only")

    merged = _rrf_merge([overlap, text_only], [overlap, sem_only], k=3, rrf_k=60)
    keys = [d["__key"] for d in merged]

    # Overlap gets score in both legs (2 / 61), must rank first
    assert keys[0] == overlap["__key"]
    # Tie at 1 / 62 between sem_only (ts=75) and text_only (ts=50) -> sem_only wins by ts desc
    assert keys[1] == sem_only["__key"]
    assert keys[2] == text_only["__key"]


def test_rrf_merge_tie_break_determinism():
    # Same score, same ts -> tie-break on key string ascending
    text_doc = _make_doc("c1", 9, 100.0, "Z doc")
    sem_doc = _make_doc("c1", 1, 100.0, "A doc")

    merged = _rrf_merge([text_doc], [sem_doc], k=2, rrf_k=60)
    assert [d["__key"] for d in merged] == [
        "pod:agent:chat-audit:c1:1",
        "pod:agent:chat-audit:c1:9",
    ]


def test_vec_to_bytes():
    vec = [0.1 * i for i in range(VECTOR_DIM)]
    packed = _vec_to_bytes(vec)
    assert isinstance(packed, bytes)
    # 384 floats * 4 bytes per float32 = 1536 bytes
    assert len(packed) == VECTOR_DIM * 4
