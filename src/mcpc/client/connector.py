"""Turn a ResolvedServer into a connected MCP `Client`.

`open_client` is the only place that knows how to reach a server over stdio, Streamable HTTP
or SSE. Every exception leaving it is an `McpcError`.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any, TextIO

import anyio
import httpx2
from mcp import Client, MCPError, StdioServerParameters, stdio_client
from mcp.client.client import ConnectMode
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT, UNSUPPORTED_PROTOCOL_VERSION, DiscoverResult
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import ValidationError

from mcpc import paths
from mcpc.catalog.models import ServerSnapshot
from mcpc.config.models import HttpServer, StdioServer
from mcpc.errors import ConnectionFailedError, McpcError, TimeoutFailedError, translate
from mcpc.executor import ResolvedServer

__all__ = ["choose_mode", "open_client", "to_mcpc_error"]


def choose_mode(
    server: ResolvedServer, prior: ServerSnapshot | None
) -> tuple[ConnectMode, DiscoverResult | None]:
    """Pick how to connect: forced legacy, a pin from a fresh cached snapshot, or auto negotiation."""
    if server.config.mode == "legacy":
        return "legacy", None
    if prior is None or prior.last_error is not None or prior.spec_hash != server.config.spec_hash():
        return "auto", None
    if prior.is_legacy():
        return "legacy", None
    if prior.discover_result and prior.protocol_version in MODERN_PROTOCOL_VERSIONS:
        try:
            discover = DiscoverResult.model_validate(prior.discover_result)
        except ValidationError:
            return "auto", None
        return prior.protocol_version, discover
    return "auto", None


@asynccontextmanager
async def open_client(server: ResolvedServer, prior: ServerSnapshot | None = None) -> AsyncIterator[Client]:
    """Connect to `server` and yield a ready `Client`; always an `McpcError` on failure.

    Connecting is bounded by `server.connect_timeout`. The body of the `async with` is not.
    """
    try:
        async with AsyncExitStack() as stack:
            client = await _connect_bounded(stack, server, prior)
            yield client
    except Exception as exc:
        err = to_mcpc_error(exc, server.name)
        if err is exc:
            raise
        raise err from exc


async def _connect_bounded(
    stack: AsyncExitStack, server: ResolvedServer, prior: ServerSnapshot | None
) -> Client:
    """Connect under the connect timeout.

    The timeout is a cancel scope entered first on `stack` (so it is exited last, keeping anyio
    scope nesting valid) and disarmed once connected.
    """
    scope = anyio.CancelScope(deadline=anyio.current_time() + server.connect_timeout)
    stack.enter_context(scope)
    try:
        client = await _connect_with_fallback(stack, server, prior)
    except anyio.get_cancelled_exc_class():
        if scope.cancel_called and anyio.current_time() >= scope.deadline:
            raise TimeoutError(f"connect timed out after {server.connect_timeout:g}s") from None
        raise
    scope.deadline = math.inf
    return client


async def _connect_with_fallback(
    stack: AsyncExitStack, server: ResolvedServer, prior: ServerSnapshot | None
) -> Client:
    mode, prior_discover = choose_mode(server, prior)
    try:
        return await _connect_once(stack, server, mode, prior_discover)
    except Exception as exc:
        if mode in ("auto", "legacy") or not _is_unsupported_version(exc):
            raise
    return await _connect_once(stack, server, "auto", None)


async def _connect_once(
    stack: AsyncExitStack, server: ResolvedServer, mode: ConnectMode, prior_discover: DiscoverResult | None
) -> Client:
    """One connection attempt. Its resources are tied to `stack` only if it succeeds."""
    attempt = AsyncExitStack()
    try:
        transport = await _enter_transport(attempt, server)
        client = Client(
            transport,
            mode=mode,
            prior_discover=prior_discover,
            cache=None,
            read_timeout_seconds=server.request_timeout,
        )
        await attempt.enter_async_context(client)
    except BaseException as exc:
        # Unwind the failed attempt (log file, http client, half-started process) with the real error.
        await attempt.__aexit__(type(exc), exc, exc.__traceback__)
        raise
    stack.push_async_exit(attempt)
    return client


async def _enter_transport(attempt: AsyncExitStack, server: ResolvedServer) -> Any:
    """Build the transport context manager for the server's spec."""
    spec = server.spec
    if isinstance(spec, StdioServer):
        errlog = attempt.enter_context(_open_log(server.name))
        params = StdioServerParameters(
            command=spec.command, args=spec.args, env=spec.env or None, cwd=spec.cwd
        )
        return stdio_client(params, errlog=errlog)
    assert isinstance(spec, HttpServer)
    if spec.transport == "sse":
        return sse_client(
            spec.url,
            headers=spec.headers or None,
            timeout=server.connect_timeout,
            sse_read_timeout=server.request_timeout,
        )
    http = httpx2.AsyncClient(
        headers=spec.headers or None,
        timeout=httpx2.Timeout(server.connect_timeout, read=server.request_timeout),
    )
    await attempt.enter_async_context(http)
    return streamable_http_client(spec.url, http_client=http)


@contextmanager
def _open_log(name: str) -> Iterator[TextIO]:
    """Append-mode, line-buffered per-server log that receives the child's stderr."""
    with open(paths.server_log_path(name), "a", encoding="utf-8", buffering=1) as f:
        f.write(f"--- {datetime.now(UTC).isoformat(timespec='seconds')} start {name} ---\n")
        yield f


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for e in exc.exceptions for leaf in _leaves(e)]
    return [exc]


def _is_unsupported_version(exc: BaseException) -> bool:
    return any(isinstance(e, MCPError) and e.code == UNSUPPORTED_PROTOCOL_VERSION for e in _leaves(exc))


def to_mcpc_error(exc: Exception, name: str) -> McpcError:
    """`errors.translate`, plus: a dropped connection is a transport failure and an SDK request
    timeout is a timeout, not a protocol error."""
    for leaf in _leaves(exc):
        if isinstance(leaf, MCPError) and leaf.code == CONNECTION_CLOSED:
            return ConnectionFailedError(
                f"connection closed by server (it may have crashed; see {paths.server_log_path(name)})",
                server=name,
            )
        if isinstance(leaf, MCPError) and leaf.code == REQUEST_TIMEOUT:
            return TimeoutFailedError(f"request timed out: {leaf.message}", server=name)
    return translate(exc, server=name)
