"""Raw collected-output parser."""

from __future__ import annotations

from typing import Any

from ..core import ParseResult


class RawParser:
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
        if result.exit_code == 0:
            return ParseResult(text=result.stdout_text, ok=True)
        error = result.stderr_text or f"exit_code={result.exit_code}"
        return ParseResult(text="", ok=False, error_message=error)

