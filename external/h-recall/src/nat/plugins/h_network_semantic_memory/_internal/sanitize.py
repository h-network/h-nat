"""RediSearch query interpolation safety.

The audit-tier search path drops a free-text ``query`` into ``FT.SEARCH``
expressions of two shapes:

  - ``@chat_id:{<chat_id>}``        — TAG-field tenancy filter.
  - ``@content:<query>``            — TEXT-field full-text match.

Without escaping, a query containing RediSearch syntax-special characters
(``)``, ``|``, ``-``, ``{`` ...) either errors out or, worse, breaks
the tenancy scoping by closing the TAG group early. This module
provides one helper, :func:`escape_redisearch_query`, that prefixes the
documented punctuation set with ``\\`` so each character matches itself
literally.

This is **not** the same problem as ``source/sanitise.py`` (which is
mcp-server credential-scrub for command output — PEM keys, bearer
tokens, etc.). The two domains are unrelated.
"""

# Per RediSearch v2 query-syntax docs, these chars are punctuation /
# tokenization specials. Backslash is in the set because an unescaped
# trailing ``\\`` is a syntax error inside an FT.SEARCH expression.
_REDISEARCH_SPECIALS = frozenset(
    ',.<>{}[]"\':;!@#$%^&*()-+=~|/?\\'
)


def escape_redisearch_query(value: str, *, escape_whitespace: bool = False) -> str:
    """Return ``value`` with RediSearch syntax-special chars backslash-escaped.

    Empty strings pass through unchanged. Alphanumerics and Unicode word
    characters pass through; everything in :data:`_REDISEARCH_SPECIALS`
    gets a leading ``\\``.

    ``escape_whitespace=False`` (the default) preserves spaces — this is
    what TEXT-field free-text queries want (``foo bar`` parses as
    ``foo AND bar``). ``escape_whitespace=True`` escapes space, tab, and
    newline too — use this for TAG-field values that should match
    literally with whitespace inside (e.g. a chat_id that happens to
    contain a space, even though that's discouraged).

    Idempotent under some inputs but NOT in general — calling this twice
    on the same string double-escapes. Apply once at the boundary,
    immediately before string-interpolating into the FT.SEARCH expression.
    """
    if not value:
        return value
    out: list[str] = []
    for ch in value:
        if ch in _REDISEARCH_SPECIALS or (escape_whitespace and ch.isspace()):
            out.append("\\")
        out.append(ch)
    return "".join(out)
