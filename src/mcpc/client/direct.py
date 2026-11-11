"""Direct executor: opens a fresh `Client` for every call, in this process."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import anyio
from mcp import Client
from mcp.types import CallToolResult, GetPromptResult, ReadResourceResult

from mcpc.catalog.models import ServerSnapshot
from mcpc.client.connector import open_client, to_mcpc_error
from mcpc.client.operations import take_snapshot
from mcpc.errors import McpcError
from mcpc.executor import ResolvedServer

__all__ = ["DirectExecutor"]

_T = TypeVar("_T")


class DirectExecutor:
    """Implements `mcpc.executor.Executor` without a daemon: one connection per call."""

    async def snapshot(self, server: ResolvedServer, prior: ServerSnapshot | None = None) -> ServerSnapshot:
        """Connect and return a full catalog snapshot."""
        return await _run(server, prior, lambda c: take_snapshot(c, server))

    async def call_tool(
        self,
        server: ResolvedServer,
        tool: str,
        arguments: dict[str, Any],
        prior: ServerSnapshot | None = None,
    ) -> CallToolResult:
        """Call a tool. A result with `is_error=True` is returned, not raised."""
        return await _run(server, prior, lambda c: c.call_tool(tool, arguments))

    async def read_resource(
        self, server: ResolvedServer, uri: str, prior: ServerSnapshot | None = None
    ) -> ReadResourceResult:
        """Read one resource by URI."""
        return await _run(server, prior, lambda c: c.read_resource(uri))

    async def get_prompt(
        self,
        server: ResolvedServer,
        name: str,
        arguments: dict[str, str],
        prior: ServerSnapshot | None = None,
    ) -> GetPromptResult:
        """Render a prompt."""
        return await _run(server, prior, lambda c: c.get_prompt(name, arguments or None))

    async def aclose(self) -> None:
        """Nothing is kept open between calls."""


async def _run(
    server: ResolvedServer, prior: ServerSnapshot | None, request: Callable[[Client], Awaitable[_T]]
) -> _T:
    """Connect, run `request` under the request timeout, and raise only McpcError."""
    async with open_client(server, prior) as client:
        try:
            with anyio.fail_after(server.request_timeout):
                return await request(client)
        except McpcError:
            raise
        except Exception as exc:
            raise to_mcpc_error(exc, server.name) from exc
