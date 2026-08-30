"""Lazy access to the optional h-openshell transport."""

from pathlib import Path
from typing import Any

_MISSING_MESSAGE = (
    "OpenShell-backed invocation requires the optional h-openshell dependency; "
    "install it with `pip install 'h-orchestrator[openshell]'`"
)


def create_openshell_client(
    *, gateway_home: str | None, endpoint: str | None, target_override: str
) -> Any:
    """Create a client only when an OpenShell-backed function is invoked."""

    try:
        from nat.plugins.h_openshell import OpenShellClient
    except ImportError as error:
        raise RuntimeError(_MISSING_MESSAGE) from error

    return OpenShellClient.from_default_home(
        gateway_home=Path(gateway_home) if gateway_home else None,
        endpoint_override=endpoint,
        target_override=target_override,
    )
