"""Parser for Claude CLI collected JSON output."""

from __future__ import annotations

import json
from typing import Any

from ..core import ParseResult


class ClaudeJsonParser:
    streaming = False

    async def consume(
        self,
        client: Any,
        sandbox: str,
        script: bytes,
        rpc_timeout: float,
        step_manager: Any | None,
    ) -> ParseResult:
        result = await client.exec(
            sandbox, ["bash"], stdin=script, rpc_timeout=rpc_timeout
        )
        envelope = _last_json_object(result.stdout_text)
        is_error = bool(envelope.get("is_error", result.exit_code != 0))
        if result.exit_code != 0 or is_error or not envelope:
            error = (result.stderr_text.strip() or result.stdout_text.strip()[-500:])[
                :500
            ]
            if not error:
                error = f"exit_code={result.exit_code}"
            return ParseResult(
                text="", ok=False, raw=envelope or None, error_message=error
            )
        return ParseResult(
            text=envelope.get("result", "") or "", ok=True, raw=envelope
        )


def _last_json_object(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}

