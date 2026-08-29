"""Unit tests for query sanitization (sanitize.py)."""

from nat.plugins.h_network_semantic_memory._internal.sanitize import escape_redisearch_query


def test_escape_empty():
    assert escape_redisearch_query("") == ""
    assert escape_redisearch_query(None) is None


def test_escape_alphanumeric():
    assert escape_redisearch_query("hello123World") == "hello123World"


def test_escape_specials():
    raw = "test (query) with [brackets] and {braces} / special* chars!"
    escaped = escape_redisearch_query(raw)
    assert "\\(" in escaped
    assert "\\)" in escaped
    assert "\\[" in escaped
    assert "\\]" in escaped
    assert "\\{" in escaped
    assert "\\}" in escaped
    assert "\\/" in escaped
    assert "\\*" in escaped
    assert "\\!" in escaped


def test_escape_whitespace_option():
    text = "chat id with spaces"
    assert escape_redisearch_query(text, escape_whitespace=False) == "chat id with spaces"
    assert escape_redisearch_query(text, escape_whitespace=True) == "chat\\ id\\ with\\ spaces"
