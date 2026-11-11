"""Stateful fixture server for daemon tests: proves whether a session/process is reused."""

from __future__ import annotations

import os
import time

from mcp.server import MCPServer

mcp = MCPServer("Counter", instructions="Counts calls within one server process.")

_count = 0


@mcp.tool()
def bump() -> str:
    """Increment and return the in-process call counter."""
    global _count
    _count += 1
    return str(_count)


@mcp.tool()
def pid() -> str:
    """Return the server process id."""
    return str(os.getpid())


@mcp.tool()
def sleep(seconds: float) -> str:
    """Sleep, then return. For concurrency and timeout tests."""
    time.sleep(seconds)
    return "slept"


@mcp.tool()
def die() -> str:
    """Exit the process abruptly (crash test)."""
    os._exit(3)


if __name__ == "__main__":
    mcp.run()
