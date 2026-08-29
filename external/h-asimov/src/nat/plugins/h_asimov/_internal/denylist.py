"""Layer 1: pattern denylist.

Ported from h-network-asimov-firewall/source/denylist.py (commit
bcb4e374) — see LLD.md §2.2, §4. `_normalize` and the substring-match
`check` are unchanged. `from_env` is replaced by `from_texts`: this
port is config-driven (NAT workflow YAML), not env-driven, and the
packaged-default path is resolved by register.py via
`importlib.resources` rather than a `Path(__file__).parent.parent`
computation — see LLD.md §5 for why the predecessor's equivalent
computation was not carried over as-is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(command: str) -> str:
    """Lowercase, strip both quote types, collapse whitespace runs."""
    cmd = command.lower().replace('"', "").replace("'", "")
    return _WHITESPACE_RE.sub(" ", cmd).strip()


def _parse_patterns(text: str) -> list[str]:
    """One pattern per line, normalized to lowercase + stripped.
    Comments (`#`) and blank lines ignored.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lower()
        if line and not line.startswith("#"):
            out.append(line)
    return out


@dataclass(frozen=True)
class DenylistHit:
    """Match result. `pattern_name` is the *pattern* string (sanitized
    by construction — never echoed user input).
    """

    pattern_name: str


class Denylist:
    """Substring-match denylist over the normalized command string."""

    def __init__(self, patterns: list[str]) -> None:
        self._patterns = list(patterns)

    @classmethod
    def from_texts(cls, *, default_text: str, override_path: str | None) -> "Denylist":
        """Loads the packaged default patterns, then appends an
        optional operator override file.

        Loud on operator misconfig: an override path that is
        configured but doesn't exist raises rather than silently
        falling through.
        """
        patterns = _parse_patterns(default_text)
        if override_path:
            path = Path(override_path)
            if not path.is_file():
                raise RuntimeError(
                    f"denylist override configured but file not found: {override_path}"
                )
            patterns.extend(_parse_patterns(path.read_text(encoding="utf-8")))
        return cls(patterns)

    def check(self, command: str) -> DenylistHit | None:
        norm = _normalize(command)
        for pattern in self._patterns:
            if pattern in norm:
                return DenylistHit(pattern_name=pattern)
        return None
