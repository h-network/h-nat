"""Exceptions exposed by h-openshell."""

from __future__ import annotations

from typing import Any


class OpenShellError(Exception):
    """Base class for module-defined failures."""


class ConfigurationError(OpenShellError):
    """The local OpenShell gateway configuration is missing or invalid."""


class GatewayRPCError(OpenShellError):
    """A gateway RPC failed.

    ``cause`` remains available for callers that need the original gRPC status.
    """

    def __init__(self, operation: str, cause: BaseException) -> None:
        super().__init__(f"OpenShell {operation} RPC failed: {cause}")
        self.operation = operation
        self.cause = cause

    @property
    def status_code(self) -> Any:
        code = getattr(self.cause, "code", None)
        return code() if callable(code) else None


class SandboxLifecycleError(OpenShellError):
    """A sandbox entered a terminal error phase."""


class SandboxTimeoutError(TimeoutError, OpenShellError):
    """A sandbox did not reach the requested state before its deadline."""
