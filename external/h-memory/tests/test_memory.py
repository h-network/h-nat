"""Unit and functional tests for BoundedBufferStore."""
import asyncio
import json
import time
import pytest
import redis.asyncio as aioredis

from nat.plugins.h_memory.memory import BoundedBufferStore


@pytest.mark.asyncio
async def test_store_properties_and_key_derivation(redis_client: aioredis.Redis, unique_pod_agent: tuple[str, str]):
    pod, agent = unique_pod_agent
    ttl_max = 3600
    store = BoundedBufferStore(client=redis_client, pod=pod, agent=agent, ttl_seconds_max=ttl_max)

    assert store.pod == pod
    assert store.agent == agent
    assert store.ttl_seconds_max == ttl_max
    assert store.client is redis_client

    turn_key = store._turn_key("chat-123", 1000000)
    assert turn_key == f"{pod}:{agent}:chat:chat-123:1000000"

    index_key = store._index_key("chat-123")
    assert index_key == f"{pod}:{agent}:chat-index:chat-123"


@pytest.mark.asyncio
async def test_monotonic_nanosecond_guard(redis_client: aioredis.Redis, unique_pod_agent: tuple[str, str]):
    pod, agent = unique_pod_agent
    store = BoundedBufferStore(client=redis_client, pod=pod, agent=agent, ttl_seconds_max=3600)

    timestamps = [store._next_ts_ns() for _ in range(1000)]
    assert len(timestamps) == 1000
    # Strictly increasing
    for i in range(len(timestamps) - 1):
        assert timestamps[i] < timestamps[i + 1]


@pytest.mark.asyncio
async def test_write_turn_and_payload(redis_client: aioredis.Redis, unique_pod_agent: tuple[str, str]):
    pod, agent = unique_pod_agent
    chat_id = "test-chat-1"
    store = BoundedBufferStore(client=redis_client, pod=pod, agent=agent, ttl_seconds_max=3600)

    turn_key = await store.write_turn(
        chat_id=chat_id,
        role="user",
        content="Hello world!",
        ttl_seconds=300,
    )

    assert turn_key.startswith(f"{pod}:{agent}:chat:{chat_id}:")

    # Verify STRING payload
    raw_payload = await redis_client.get(turn_key)
    assert raw_payload is not None
    data = json.loads(raw_payload)
    assert data["role"] == "user"
    assert data["content"] == "Hello world!"
    assert isinstance(data["ts"], int)

    # Verify TTL set
    ttl = await redis_client.ttl(turn_key)
    assert 0 < ttl <= 300

    # Verify ZSET index entry
    index_key = f"{pod}:{agent}:chat-index:{chat_id}"
    members = await redis_client.zrange(index_key, 0, -1)
    assert members == [turn_key]

    index_ttl = await redis_client.ttl(index_key)
    assert 0 < index_ttl <= 3600

    # Clean up
    deleted = await store.delete_chat(chat_id)
    assert deleted == 2  # 1 data key + 1 index key


@pytest.mark.asyncio
async def test_hot_keep_count_rank_pruning(redis_client: aioredis.Redis, unique_pod_agent: tuple[str, str]):
    pod, agent = unique_pod_agent
    chat_id = "test-chat-keep-count"
    store = BoundedBufferStore(client=redis_client, pod=pod, agent=agent, ttl_seconds_max=3600)

    # Write 5 turns with hot_keep_count = 3
    written_keys = []
    for i in range(5):
        k = await store.write_turn(
            chat_id=chat_id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"Message {i}",
            ttl_seconds=300,
            hot_keep_count=3,
        )
        written_keys.append(k)

    index_key = f"{pod}:{agent}:chat-index:{chat_id}"
    members = await redis_client.zrange(index_key, 0, -1)
    # Only the last 3 turns should remain in the index
    assert len(members) == 3
    assert members == written_keys[-3:]

    # The first 2 data keys should still be resident in Redis until their TTL expires
    for old_key in written_keys[:2]:
        val = await redis_client.get(old_key)
        assert val is not None

    # Clean up
    await store.delete_chat(chat_id)


@pytest.mark.asyncio
async def test_delete_chat_behavior(redis_client: aioredis.Redis, unique_pod_agent: tuple[str, str]):
    pod, agent = unique_pod_agent
    chat_id = "test-chat-del"
    store = BoundedBufferStore(client=redis_client, pod=pod, agent=agent, ttl_seconds_max=3600)

    # Empty chat returns 0
    del_empty = await store.delete_chat("non-existent-chat")
    assert del_empty == 0

    # Write 3 turns
    k1 = await store.write_turn(chat_id, "user", "1", 300)
    k2 = await store.write_turn(chat_id, "assistant", "2", 300)
    k3 = await store.write_turn(chat_id, "user", "3", 300)

    deleted_count = await store.delete_chat(chat_id)
    assert deleted_count == 4  # 3 turns + 1 index key

    # Assert keys are gone
    assert await redis_client.get(k1) is None
    assert await redis_client.get(k2) is None
    assert await redis_client.get(k3) is None
    assert await redis_client.zrange(f"{pod}:{agent}:chat-index:{chat_id}", 0, -1) == []
