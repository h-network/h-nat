"""Unit tests verifying core invariants and AuditStore mechanics."""

import pytest
from nat.plugins.h_network_semantic_memory._internal.store import (
    AuditStore,
    VECTOR_DIM,
    HOT_SCOPE,
    AUDIT_SCOPE,
)


def test_audit_store_construction_shapes():
    # Mock redis client object
    mock_client1 = object()
    mock_client2 = object()

    # Legacy / Colocated
    store = AuditStore(pod="pod1", agent="agent1", client=mock_client1)
    assert store.audit_client is mock_client1
    assert store.hot_client is mock_client1
    assert store.client is mock_client1

    # Split
    store_split = AuditStore(pod="p", agent="a", audit_client=mock_client1, hot_client=mock_client2)
    assert store_split.audit_client is mock_client1
    assert store_split.hot_client is mock_client2
    assert store_split.client is mock_client1

    # Mixed -> rejected
    with pytest.raises(TypeError, match="not both"):
        AuditStore(pod="p", agent="a", client=mock_client1, audit_client=mock_client2)

    # Incomplete split -> rejected
    with pytest.raises(TypeError, match="requires both"):
        AuditStore(pod="p", agent="a", audit_client=mock_client1)

    # Empty -> rejected
    with pytest.raises(TypeError, match="pass `client=` OR"):
        AuditStore(pod="p", agent="a")


def test_key_derivation_invariants():
    mock_client = object()
    store = AuditStore(pod="my-pod", agent="my-agent", client=mock_client)

    chat_id = "test-chat-42"
    ts_ns = 1715500000123456789

    # Hot key derivation
    hot_k = store.hot_key(chat_id, ts_ns)
    assert hot_k == "my-pod:my-agent:chat:test-chat-42:1715500000123456789"
    assert store.hot_index_key(chat_id) == "my-pod:my-agent:chat-index:test-chat-42"

    # Audit key derivation
    audit_k = store.audit_key(chat_id, ts_ns)
    assert audit_k == "my-pod:my-agent:chat-audit:test-chat-42:1715500000123456789"
    assert store.audit_index_name == "my-pod:my-agent:chat-audit:idx"

    # Invariant 4: parse ts_ns and chat_id strictly from hot key
    assert AuditStore.chat_id_from_hot_key(hot_k) == chat_id
    assert AuditStore.ts_ns_from_hot_key(hot_k) == ts_ns
