"""Smoke tests for the scaffold and the shared contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client
from mcp.types import CallToolResult
from typer.testing import CliRunner

from mcpc.catalog.models import ServerSnapshot
from mcpc.cli.app import app
from mcpc.config.models import ServerConfig, StdioServer
from mcpc.errors import ExitCode, ProtocolFailedError, from_dict, translate


def test_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("mcpc ")


@pytest.mark.anyio
async def test_in_process_client_and_snapshot_roundtrip() -> None:
    from bookshop_server import mcp as bookshop

    async with Client(bookshop) as client:
        tools = (await client.list_tools()).tools
        snap = ServerSnapshot(
            server="bookshop",
            spec_hash="x",
            protocol_version=client.protocol_version,
            server_info=client.server_info,
            tools=tools,
        )
        result = await client.call_tool("search_books", {"query": "dune"})

    restored = ServerSnapshot.model_validate_json(snap.model_dump_json(by_alias=True))
    assert restored.tool("search_books") is not None
    assert restored.tool("search_books").input_schema["required"] == ["query"]  # type: ignore[union-attr]

    # CallToolResult must survive the daemon JSON boundary.
    wire = result.model_dump(mode="json", by_alias=True)
    assert CallToolResult.model_validate(wire).content == result.content


def test_spec_hash_ignores_nothing_but_order() -> None:
    a = ServerConfig(
        name="a", spec=StdioServer(command="x", env={"A": "1", "B": "2"}), scope="global", source=Path("g")
    )
    b = ServerConfig(
        name="a", spec=StdioServer(command="x", env={"B": "2", "A": "1"}), scope="global", source=Path("g")
    )
    c = ServerConfig(name="a", spec=StdioServer(command="y"), scope="global", source=Path("g"))
    assert a.spec_hash() == b.spec_hash() != c.spec_hash()


def test_error_translation() -> None:
    from mcp import MCPError

    err = translate(ExceptionGroup("tg", [MCPError(-32602, "bad")]), server="s")
    assert isinstance(err, ProtocolFailedError) and err.exit_code == ExitCode.PROTOCOL and err.server == "s"
    assert translate(FileNotFoundError("nope")).exit_code == ExitCode.CONNECTION
    assert translate(TimeoutError()).kind == "timeout"
    rebuilt = from_dict(err.to_dict())
    assert type(rebuilt) is ProtocolFailedError and rebuilt.server == "s"
