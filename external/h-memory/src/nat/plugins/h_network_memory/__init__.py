"""Compatibility re-export of h_memory as h_network_memory."""
from nat.plugins.h_memory import (
    BoundedBufferStore,
    DeleteChatInput,
    HMemoryDeleteChatConfig,
    HMemoryWriteTurnConfig,
    WriteTurnInput,
    h_memory_delete_chat,
    h_memory_write_turn,
)

__all__ = [
    "BoundedBufferStore",
    "DeleteChatInput",
    "HMemoryDeleteChatConfig",
    "HMemoryWriteTurnConfig",
    "WriteTurnInput",
    "h_memory_delete_chat",
    "h_memory_write_turn",
]
