"""Integration tests for h_memory_write_turn and h_memory_delete_chat builders."""
import json

import pytest
import redis.asyncio as aioredis
from nat.plugins.h_memory.register import (
    DeleteChatInput,
    HMemoryDeleteChatConfig,
    HMemoryWriteTurnConfig,
    WriteTurnInput,
    h_memory_delete_chat,
    h_memory_write_turn,
)


@pytest.mark.asyncio
async def test_write_turn_builder_flow(unique_pod_agent: tuple[str, str], redis_client: aioredis.Redis):
    pod, agent = unique_pod_agent
    config = HMemoryWriteTurnConfig(
        pod=pod,
        agent=agent,
        ttl_seconds_max=600,
        hot_keep_count=10,
    )

    async with h_memory_write_turn(config) as fn_info:
        invoke_fn = getattr(fn_info, "single_fn", getattr(fn_info, "fn", None))

        # Valid turn write
        turn_inp = WriteTurnInput(
            chat_id="session-int-1",
            role="user",
            content="Integration test prompt",
            ttl_seconds=300,
        )
        turn_key = await invoke_fn(turn_inp)
        assert turn_key.startswith(f"{pod}:{agent}:chat:session-int-1:")

        # Attempt turn with ttl_seconds > ttl_seconds_max
        invalid_ttl_inp = WriteTurnInput(
            chat_id="session-int-1",
            role="user",
            content="Too long TTL",
            ttl_seconds=700,
        )
        with pytest.raises(ValueError, match="out of range"):
            await invoke_fn(invalid_ttl_inp)


@pytest.mark.asyncio
async def test_delete_chat_builder_flow(unique_pod_agent: tuple[str, str], redis_client: aioredis.Redis):
    pod, agent = unique_pod_agent
    chat_id = "session-to-wipe"

    # Write a turn first using write_turn builder
    w_config = HMemoryWriteTurnConfig(pod=pod, agent=agent, ttl_seconds_max=600)
    async with h_memory_write_turn(w_config) as w_info:
        w_fn = getattr(w_info, "single_fn", getattr(w_info, "fn", None))
        await w_fn(WriteTurnInput(chat_id=chat_id, role="user", content="msg", ttl_seconds=300))

    # Now use delete_chat builder
    del_config = HMemoryDeleteChatConfig(pod=pod, agent=agent)
    async with h_memory_delete_chat(del_config) as del_info:
        del_fn = getattr(del_info, "single_fn", getattr(del_info, "fn", None))
        deleted = await del_fn(DeleteChatInput(chat_id=chat_id))
        assert deleted == 2  # 1 data key + 1 index key


@pytest.mark.asyncio
async def test_read_contract_simulation(unique_pod_agent: tuple[str, str], redis_client: aioredis.Redis):
    """Simulates the read contract used by h-recall and h-orchestrator."""
    pod, agent = unique_pod_agent
    chat_id = "chat-read-sim"

    w_config = HMemoryWriteTurnConfig(pod=pod, agent=agent, ttl_seconds_max=600)
    async with h_memory_write_turn(w_config) as w_info:
        w_fn = getattr(w_info, "single_fn", getattr(w_info, "fn", None))
        for i in range(3):
            await w_fn(WriteTurnInput(
                chat_id=chat_id,
                role="user" if i % 2 == 0 else "assistant",
                content=f"Turn {i}",
                ttl_seconds=300,
            ))

        # Sibling reader logic: ZREVRANGE + MGET
        index_key = f"{pod}:{agent}:chat-index:{chat_id}"
        keys = await redis_client.zrevrange(index_key, 0, -1)
        assert len(keys) == 3

        raw_payloads = await redis_client.mget(*keys)
        turns = [json.loads(p) for p in raw_payloads if p]
        assert len(turns) == 3
        # keys are newest-first, so raw turns are newest-first
        assert turns[0]["content"] == "Turn 2"
        assert turns[1]["content"] == "Turn 1"
        assert turns[2]["content"] == "Turn 0"

        # Reversed for oldest-first chronological prompt assembly
        turns.reverse()
        assert turns[0]["content"] == "Turn 0"
        assert turns[2]["content"] == "Turn 2"

    # Cleanup
    del_config = HMemoryDeleteChatConfig(pod=pod, agent=agent)
    async with h_memory_delete_chat(del_config) as del_info:
        del_fn = getattr(del_info, "single_fn", getattr(del_info, "fn", None))
        await del_fn(DeleteChatInput(chat_id=chat_id))
