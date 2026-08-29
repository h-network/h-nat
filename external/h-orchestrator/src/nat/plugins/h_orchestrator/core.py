"""Transport-neutral prompt construction and parser contracts."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ParseResult:
    """Normalized result returned by a unary output parser."""

    text: str
    ok: bool
    raw: dict[str, Any] | None = None
    error_message: str = ""


@runtime_checkable
class OutputParser(Protocol):
    """Parse one command execution through an OpenShell-like client."""

    streaming: bool

    async def consume(
        self,
        client: Any,
        sandbox: str,
        script: bytes,
        rpc_timeout: float,
        step_manager: Any | None,
    ) -> ParseResult: ...


def with_context(context_block: str, prompt: str) -> str:
    """Prepend non-empty static context to a prompt."""

    if not context_block.strip():
        return prompt
    return f"{context_block}\n\n{prompt}"


def build_script(
    *, command: str, args: list[str], prompt: str, prompt_via: str
) -> bytes:
    """Build a quoted bash script that delivers one prompt to a command."""

    head = " ".join(shlex.quote(part) for part in (command, *args))
    lines = ["set -e"]
    if prompt_via == "arg":
        lines.append(f"exec {head} {shlex.quote(prompt)}")
    elif prompt_via == "stdin":
        delimiter = _heredoc_delimiter(prompt)
        lines.extend((f"exec {head} <<'{delimiter}'", prompt, delimiter))
    elif prompt_via.startswith("env:"):
        variable = prompt_via.partition(":")[2]
        if not _valid_env_name(variable):
            raise ValueError(f"invalid environment variable: {variable!r}")
        lines.extend((f"export {variable}={shlex.quote(prompt)}", f"exec {head}"))
    else:
        raise ValueError(f"unknown prompt_via: {prompt_via!r}")
    return ("\n".join(lines) + "\n").encode()


def _valid_env_name(value: str) -> bool:
    return bool(value) and (value[0].isalpha() or value[0] == "_") and all(
        character.isalnum() or character == "_" for character in value
    )


def _heredoc_delimiter(prompt: str) -> str:
    """Choose a delimiter that cannot terminate the supplied prompt."""

    base = "__H_AGENT_PROMPT__"
    delimiter = base
    suffix = 0
    prompt_lines = set(prompt.splitlines())
    while delimiter in prompt_lines:
        suffix += 1
        delimiter = f"{base}_{suffix}"
    return delimiter

