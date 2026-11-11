"""Choose how the CLI reaches servers: through the daemon when available, else directly.

Shared contract for Wave 2. The daemon worker implements `mcpc.daemon.client.connect_or_start`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcpc.config.models import Settings
from mcpc.executor import Executor

if TYPE_CHECKING:
    from mcpc.client.direct import DirectExecutor

log = logging.getLogger(__name__)


def _direct() -> DirectExecutor:
    """Create a DirectExecutor (imports the MCP SDK client stack on first use)."""
    from mcpc.client.direct import DirectExecutor

    return DirectExecutor()


async def make_executor(settings: Settings, *, no_daemon: bool = False) -> Executor:
    """Return a DaemonExecutor if the daemon is enabled and reachable/startable, else a DirectExecutor.

    Never raises for daemon problems: any failure falls back to direct mode (with a warning logged).
    """
    if no_daemon or not settings.daemon.enabled:
        return _direct()
    try:
        from mcpc.daemon.client import connect_or_start
    except ImportError:  # daemon package not built yet
        return _direct()
    try:
        executor = await connect_or_start(settings)
    except Exception as exc:  # pragma: no cover - defensive; connect_or_start should not raise
        log.warning("daemon unavailable, using direct mode: %s", exc)
        return _direct()
    return executor if executor is not None else _direct()
