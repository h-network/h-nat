"""Stable values returned by the public h-openshell client."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Sandbox:
    """Gateway-owned sandbox state rendered without protobuf coupling."""

    id: str
    name: str
    workspace: str
    phase: int
    phase_name: str


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Collected command output; ``exit_code`` is absent without an exit event."""

    exit_code: int | None
    stdout: bytes
    stderr: bytes

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode(errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode(errors="replace")
