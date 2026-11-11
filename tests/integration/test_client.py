"""Integration tests for the client layer: connector, operations and DirectExecutor."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
from mcp.types import TextContent

from mcpc import paths
from mcpc.catalog.models import ServerSnapshot
from mcpc.client.connector import choose_mode, open_client
from mcpc.client.direct import DirectExecutor
from mcpc.config.models import HttpServer, ServerConfig, StdioServer
from mcpc.errors import ConnectionFailedError, ExitCode, McpcError, TimeoutFailedError
from mcpc.executor import ResolvedServer

MODERN = "2026-07-28"
LEGACY = "2025-11-25"


def make_server(
    spec: StdioServer | HttpServer,
    name: str = "bookshop",
    *,
    mode: str = "auto",
    connect_timeout: float = 30,
    request_timeout: float = 30,
) -> ResolvedServer:
    cfg = ServerConfig.model_validate(
        {"name": name, "spec": spec, "mode": mode, "scope": "global", "source": Path("x")}
    )
    return ResolvedServer(
        config=cfg, spec=spec, connect_timeout=connect_timeout, request_timeout=request_timeout
    )


def stdio(cmd: list[str], **kw: float | str) -> ResolvedServer:
    return make_server(StdioServer(command=cmd[0], args=cmd[1:]), **kw)  # type: ignore[arg-type]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def text_of(result_content: list) -> str:
    return "\n".join(b.text for b in result_content if isinstance(b, TextContent))


# --- choose_mode (pure) ---------------------------------------------------------------------------


def _snap(server: ResolvedServer, **kw: object) -> ServerSnapshot:
    return ServerSnapshot(server=server.name, spec_hash=server.config.spec_hash(), **kw)  # type: ignore[arg-type]


def test_choose_mode_matrix() -> None:
    server = stdio(["python", "x.py"])
    assert choose_mode(server, None) == ("auto", None)
    assert choose_mode(stdio(["python"], mode="legacy"), _snap(server)) == ("legacy", None)

    # legacy era snapshot
    assert choose_mode(server, _snap(server, protocol_version=LEGACY)) == ("legacy", None)
    # stale spec hash
    stale = ServerSnapshot(server="bookshop", spec_hash="old", protocol_version=LEGACY)
    assert choose_mode(server, stale) == ("auto", None)
    # last refresh failed
    assert choose_mode(server, _snap(server, protocol_version=LEGACY, last_error="boom")) == ("auto", None)
    # nothing negotiated yet
    assert choose_mode(server, _snap(server)) == ("auto", None)
    # modern version but a discover payload that does not validate
    bad = _snap(server, protocol_version=MODERN, discover_result={"nonsense": True})
    assert choose_mode(server, bad) == ("auto", None)
    # a version we cannot pin
    odd = _snap(server, protocol_version="2099-01-01", discover_result={"nonsense": True})
    assert choose_mode(server, odd) == ("auto", None)


# --- stdio ----------------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.anyio
async def test_stdio_snapshot_and_requests(
    bookshop_stdio_cmd: list[str], capfd: pytest.CaptureFixture[str]
) -> None:
    server = stdio(bookshop_stdio_cmd)
    ex = DirectExecutor()

    snap = await ex.snapshot(server)
    assert {t.name for t in snap.tools} == {
        "search_books",
        "lookup_book",
        "add_review",
        "cover_image",
        "crash",
    }
    assert len(snap.resources) == 1
    assert len(snap.resource_templates) == 1
    assert [p.name for p in snap.prompts] == ["recommend"]
    assert snap.protocol_version == MODERN
    assert snap.discover_result is not None
    assert snap.server_info is not None and snap.server_info.name == "Bookshop"
    assert snap.instructions and snap.instructions.startswith("Search the catalog")
    assert snap.spec_hash == server.config.spec_hash()
    assert snap.capabilities and "tools" in snap.capabilities
    # the snapshot survives a JSON round trip
    restored = ServerSnapshot.model_validate_json(snap.model_dump_json(by_alias=True))
    assert restored.tool("lookup_book") is not None

    ok = await ex.call_tool(server, "search_books", {"query": "dune"})
    assert not ok.is_error
    assert "dune" in text_of(ok.content)

    missing = await ex.call_tool(server, "lookup_book", {"title": "X"})
    assert missing.is_error
    assert "No book titled" in text_of(missing.content)

    res = await ex.read_resource(server, "catalog://genres/poetry")
    assert "poetry" in str(res.contents[0].text)  # type: ignore[union-attr]

    prompt = await ex.get_prompt(server, "recommend", {"genre": "poetry"})
    assert "poetry" in str(prompt.messages[0].content)

    await ex.aclose()

    # The child's stderr went to the log file, not to our stdout/stderr.
    captured = capfd.readouterr()
    assert "bookshop starting" not in captured.out + captured.err
    log = paths.server_log_path("bookshop").read_text(encoding="utf-8")
    assert "bookshop starting" in log
    assert " start bookshop ---" in log.splitlines()[0]
    assert log.startswith("--- ")


@pytest.mark.slow
@pytest.mark.anyio
async def test_unknown_tool_is_error_result(bookshop_stdio_cmd: list[str]) -> None:
    result = await DirectExecutor().call_tool(stdio(bookshop_stdio_cmd), "nope", {})
    assert result.is_error


@pytest.mark.slow
@pytest.mark.anyio
async def test_prior_snapshot_pins_mode(bookshop_stdio_cmd: list[str]) -> None:
    server = stdio(bookshop_stdio_cmd)
    snap = await DirectExecutor().snapshot(server)
    mode, discover = choose_mode(server, snap)
    assert mode == MODERN and discover is not None

    async with open_client(server, snap) as client:
        assert client.protocol_version == MODERN
        assert client.server_info is not None
        assert client.server_info.name == "Bookshop"
        # Pinned with prior_discover: capabilities are known without a probe, requests still work.
        result = await client.call_tool("search_books", {"query": "x"})
        assert not result.is_error

    # Executor methods accept the prior too, and a snapshot taken through it is complete.
    again = await DirectExecutor().snapshot(server, snap)
    assert len(again.tools) == 5 and again.protocol_version == MODERN


@pytest.mark.slow
@pytest.mark.anyio
async def test_legacy_config_mode_uses_old_handshake(bookshop_stdio_cmd: list[str]) -> None:
    server = stdio(bookshop_stdio_cmd, mode="legacy")
    async with open_client(server) as client:
        assert client.protocol_version == LEGACY
    snap = await DirectExecutor().snapshot(server)
    assert snap.protocol_version == LEGACY
    assert snap.discover_result is None
    assert snap.is_legacy()
    assert len(snap.tools) == 5

    # A legacy snapshot as prior reconnects with the legacy handshake.
    assert choose_mode(stdio(bookshop_stdio_cmd), snap) == ("legacy", None)
    async with open_client(stdio(bookshop_stdio_cmd), snap) as client:
        assert client.protocol_version == LEGACY


@pytest.mark.anyio
async def test_unsupported_version_pin_retries_once_with_auto(
    bookshop_stdio_cmd: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned connect that fails with -32022 is retried once with mode 'auto'."""
    from mcp import MCPError

    from mcpc.client import connector

    server = stdio(bookshop_stdio_cmd)
    snap = await DirectExecutor().snapshot(server)
    real = connector._connect_once
    modes: list[str] = []

    async def fake(stack, srv, mode, prior_discover):  # type: ignore[no-untyped-def]
        modes.append(mode)
        if mode != "auto":
            raise MCPError(-32022, "unsupported protocol version")
        return await real(stack, srv, mode, prior_discover)

    monkeypatch.setattr(connector, "_connect_once", fake)
    async with open_client(server, snap) as client:
        assert client.protocol_version == MODERN
    assert modes == [MODERN, "auto"]


@pytest.mark.anyio
async def test_other_pinned_failures_are_not_retried(
    bookshop_stdio_cmd: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp import MCPError

    from mcpc.client import connector
    from mcpc.errors import ProtocolFailedError

    server = stdio(bookshop_stdio_cmd)
    snap = await DirectExecutor().snapshot(server)
    modes: list[str] = []

    async def fake(stack, srv, mode, prior_discover):  # type: ignore[no-untyped-def]
        modes.append(mode)
        raise MCPError(-32602, "invalid params")

    monkeypatch.setattr(connector, "_connect_once", fake)
    with pytest.raises(ProtocolFailedError):
        async with open_client(server, snap):
            pass
    assert modes == [MODERN]


# --- HTTP -----------------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.anyio
async def test_http_snapshot_and_call(bookshop_http_url: str) -> None:
    server = make_server(HttpServer(url=bookshop_http_url), connect_timeout=10, request_timeout=20)
    ex = DirectExecutor()
    snap = await ex.snapshot(server)
    assert len(snap.tools) == 5
    assert snap.protocol_version == MODERN
    result = await ex.call_tool(server, "search_books", {"query": "dune", "limit": 2})
    assert not result.is_error
    assert "dune" in text_of(result.content)


@pytest.mark.slow
@pytest.mark.anyio
async def test_http_headers_are_sent(bookshop_http_url: str) -> None:
    """Custom headers go on our own httpx2 client; a server ignoring them still works."""
    spec = HttpServer(url=bookshop_http_url, headers={"X-Test": "1"})
    snap = await DirectExecutor().snapshot(make_server(spec, connect_timeout=10))
    assert len(snap.tools) == 5


# --- errors ---------------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.anyio
async def test_bad_command_is_connection_failed() -> None:
    server = make_server(StdioServer(command="mcpc-no-such-command-xyz"), name="ghost", connect_timeout=5)
    with pytest.raises(ConnectionFailedError) as info:
        await DirectExecutor().snapshot(server)
    assert info.value.exit_code == ExitCode.CONNECTION
    assert info.value.server == "ghost"
    assert isinstance(info.value.__cause__, BaseException)


@pytest.mark.slow
@pytest.mark.anyio
async def test_http_closed_port() -> None:
    url = f"http://127.0.0.1:{free_port()}/mcp"
    server = make_server(HttpServer(url=url), name="closed", connect_timeout=3, request_timeout=5)
    with pytest.raises((ConnectionFailedError, TimeoutFailedError)) as info:
        await DirectExecutor().snapshot(server)
    assert info.value.exit_code == ExitCode.CONNECTION
    assert info.value.server == "closed"


@pytest.mark.slow
@pytest.mark.anyio
async def test_server_crash_during_call(bookshop_stdio_cmd: list[str]) -> None:
    """A server dying mid-call is a transport failure (exit 3), and must not hang."""
    server = stdio(bookshop_stdio_cmd, connect_timeout=15, request_timeout=15)
    with pytest.raises(McpcError) as info:
        await DirectExecutor().call_tool(server, "crash", {})
    assert info.value.exit_code == ExitCode.CONNECTION
    assert info.value.kind == "connection"
    assert info.value.server == "bookshop"


@pytest.mark.slow
@pytest.mark.anyio
async def test_connect_timeout_on_slow_command() -> None:
    # Starts fine but never speaks MCP.
    server = stdio([sys.executable, "-c", "import time; time.sleep(60)"], name="slow", connect_timeout=1.0)
    with pytest.raises(TimeoutFailedError) as info:
        await DirectExecutor().snapshot(server)
    assert info.value.exit_code == ExitCode.CONNECTION
    assert info.value.server == "slow"


@pytest.mark.slow
@pytest.mark.anyio
async def test_connect_timeout_does_not_limit_the_body(bookshop_stdio_cmd: list[str]) -> None:
    import anyio

    server = stdio(bookshop_stdio_cmd, connect_timeout=15)
    async with open_client(server) as client:
        await anyio.sleep(0.1)
        assert (await client.list_tools()).tools


@pytest.mark.slow
@pytest.mark.anyio
async def test_errors_raised_in_body_are_translated(bookshop_stdio_cmd: list[str]) -> None:
    server = stdio(bookshop_stdio_cmd)
    with pytest.raises(McpcError):
        async with open_client(server) as client:
            await client.read_resource("catalog://does-not-exist/at/all")
