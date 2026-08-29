"""Unit tests for Pydantic models, configs, converters, and registration in register.py."""
import json
import pytest
from pydantic import ValidationError

from nat.plugins.h_memory.register import (
    DeleteChatInput,
    HMemoryDeleteChatConfig,
    HMemoryWriteTurnConfig,
    WriteTurnInput,
    _int_to_str,
    _json_str_to_delete_chat_input,
    _json_str_to_write_turn_input,
)


def test_write_turn_input_valid():
    inp = WriteTurnInput(
        chat_id="chat-1",
        role="user",
        content="hello",
        ttl_seconds=60,
        hot_keep_count=10,
    )
    assert inp.chat_id == "chat-1"
    assert inp.role == "user"
    assert inp.content == "hello"
    assert inp.ttl_seconds == 60
    assert inp.hot_keep_count == 10


def test_write_turn_input_defaults():
    inp = WriteTurnInput(
        chat_id="chat-1",
        role="user",
        content="hello",
        ttl_seconds=60,
    )
    assert inp.hot_keep_count is None


def test_write_turn_input_validation_errors():
    # Empty chat_id
    with pytest.raises(ValidationError):
        WriteTurnInput(chat_id="", role="user", content="hi", ttl_seconds=60)

    # Empty role
    with pytest.raises(ValidationError):
        WriteTurnInput(chat_id="c1", role="", content="hi", ttl_seconds=60)

    # ttl_seconds < 1
    with pytest.raises(ValidationError):
        WriteTurnInput(chat_id="c1", role="user", content="hi", ttl_seconds=0)

    # hot_keep_count < 1
    with pytest.raises(ValidationError):
        WriteTurnInput(chat_id="c1", role="user", content="hi", ttl_seconds=60, hot_keep_count=0)

    # Extra fields forbidden
    with pytest.raises(ValidationError):
        WriteTurnInput(chat_id="c1", role="user", content="hi", ttl_seconds=60, unexpected_field="foo")


def test_delete_chat_input():
    inp = DeleteChatInput(chat_id="c1")
    assert inp.chat_id == "c1"

    with pytest.raises(ValidationError):
        DeleteChatInput(chat_id="")

    with pytest.raises(ValidationError):
        DeleteChatInput(chat_id="c1", extra_field=123)


def test_config_models_validation():
    cfg = HMemoryWriteTurnConfig(
        pod="prod-pod",
        agent="bot-1",
        ttl_seconds_max=3600,
        hot_keep_count=20,
    )
    assert cfg.pod == "prod-pod"
    assert cfg.agent == "bot-1"
    assert cfg.ttl_seconds_max == 3600
    assert cfg.hot_keep_count == 20
    assert cfg.redis_url == "redis://localhost:6379"

    # Invalid pod/agent characters (must match ^[A-Za-z0-9_-]+$)
    with pytest.raises(ValidationError):
        HMemoryWriteTurnConfig(pod="bad pod", agent="bot")

    with pytest.raises(ValidationError):
        HMemoryWriteTurnConfig(pod="pod", agent="bad:agent")

    # ttl_seconds_max < 1
    with pytest.raises(ValidationError):
        HMemoryWriteTurnConfig(pod="pod", agent="agent", ttl_seconds_max=0)

    # hot_keep_count < 1
    with pytest.raises(ValidationError):
        HMemoryWriteTurnConfig(pod="pod", agent="agent", hot_keep_count=0)


def test_delete_chat_config():
    cfg = HMemoryDeleteChatConfig(pod="prod", agent="agent")
    assert cfg.pod == "prod"
    assert cfg.agent == "agent"


def test_converters():
    json_str = '{"chat_id":"c1","role":"user","content":"test","ttl_seconds":120,"hot_keep_count":5}'
    turn_inp = _json_str_to_write_turn_input(json_str)
    assert turn_inp.chat_id == "c1"
    assert turn_inp.role == "user"
    assert turn_inp.ttl_seconds == 120
    assert turn_inp.hot_keep_count == 5

    del_json = '{"chat_id":"c1"}'
    del_inp = _json_str_to_delete_chat_input(del_json)
    assert del_inp.chat_id == "c1"

    assert _int_to_str(42) == "42"
