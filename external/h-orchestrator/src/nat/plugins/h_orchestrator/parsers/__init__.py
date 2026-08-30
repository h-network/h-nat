"""Unary output parser registry."""

from __future__ import annotations

from importlib.metadata import entry_points

from ..core import OutputParser

_ENTRY_POINT_GROUP = "nat.orchestrator.output_parsers"
_REGISTRY: dict[str, OutputParser] = {}


def register_parser(name: str, parser: OutputParser) -> None:
    if not name:
        raise ValueError("parser name must not be empty")
    if not isinstance(parser, OutputParser):
        raise TypeError(f"parser {name!r} does not implement OutputParser")
    _REGISTRY[name] = parser


def get_parser(name: str) -> OutputParser:
    if name in _REGISTRY:
        return _REGISTRY[name]
    for entry_point in entry_points(group=_ENTRY_POINT_GROUP):
        if entry_point.name == name:
            loaded = entry_point.load()
            parser = loaded() if isinstance(loaded, type) else loaded
            register_parser(name, parser)
            return parser
    raise KeyError(f"unknown output_parser {name!r}; available: {known_parsers()}")


def known_parsers() -> list[str]:
    return sorted(_REGISTRY)


from .claude_json import ClaudeJsonParser
from .raw import RawParser

register_parser("raw", RawParser())
register_parser("claude_json", ClaudeJsonParser())

__all__ = ["get_parser", "known_parsers", "register_parser"]

