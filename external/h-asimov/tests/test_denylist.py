"""Layer 1 denylist — port verification + loader.

Ported from h-network-asimov-firewall/tests/test_denylist.py (commit
bcb4e374). `_normalize` and `check` tests are unchanged. The
`from_env` tests are replaced with `from_texts` tests: this port is
config-driven, not env-driven (see LLD.md §5 for why the
predecessor's env-based default-path resolution was not carried over
as-is — it doesn't actually work, see LLD.md §5.3).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from nat.plugins.h_asimov._internal.denylist import Denylist, DenylistHit, _normalize

# ---- _normalize port ----


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  |   BASH  ", "| bash"),
        ('"|bash"', "|bash"),
        ("| BASH ", "| bash"),
        ('rm    -rf  "/etc"', "rm -rf /etc"),
        ("MIXED 'Quotes'", "mixed quotes"),
        ("\t\nfoo\n\t", "foo"),
    ],
)
def test_normalize_port(raw: str, expected: str) -> None:
    assert _normalize(raw) == expected


# ---- Substring match logic ----


def test_check_returns_hit_for_blocked_pattern() -> None:
    dl = Denylist(patterns=["| bash", "base64 -d"])
    hit = dl.check("echo hi | bash")
    assert isinstance(hit, DenylistHit)
    assert hit.pattern_name == "| bash"


def test_check_returns_none_when_no_pattern_matches() -> None:
    dl = Denylist(patterns=["| bash"])
    assert dl.check("ls -la /") is None


def test_check_normalizes_before_matching() -> None:
    dl = Denylist(patterns=["| bash"])
    assert dl.check('"echo hi   |    BASH"') is not None


def test_pattern_name_is_returned_not_user_input() -> None:
    dl = Denylist(patterns=["| bash"])
    hit = dl.check("evil-attacker-input | bash")
    assert hit is not None
    assert hit.pattern_name == "| bash"
    assert "attacker" not in hit.pattern_name


# ---- Loader (from_texts) ----


def test_from_texts_loads_default() -> None:
    dl = Denylist.from_texts(default_text="| bash\nbase64 -d\n", override_path=None)
    assert dl.check("foo | bash") is not None


def test_from_texts_appends_operator_override(tmp_path: Path) -> None:
    extra = tmp_path / "extra.txt"
    extra.write_text("# operator extras\nfoo-secret-pattern\n")
    dl = Denylist.from_texts(default_text="| bash\n", override_path=str(extra))
    assert dl.check("a foo-secret-pattern b") is not None
    # Defaults still apply.
    assert dl.check("foo | bash") is not None


def test_from_texts_loud_on_missing_override_file(tmp_path: Path) -> None:
    """Operator misconfig is loud — silent fall-through is a release blocker."""
    missing = tmp_path / "nope.txt"
    with pytest.raises(RuntimeError, match="not found"):
        Denylist.from_texts(default_text="| bash\n", override_path=str(missing))


def test_from_texts_ignores_comments_and_blank_lines() -> None:
    dl = Denylist.from_texts(
        default_text="# a comment\n\n| bash\n   \n# another\nbase64 -d\n",
        override_path=None,
    )
    assert dl.check("x | bash") is not None
    assert dl.check("base64 -d x") is not None
