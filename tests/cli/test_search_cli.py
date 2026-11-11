"""CLI tests for `mcpc search` against the real fixture servers (stdio, direct mode)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mcpc import paths
from mcpc.cli.app import app

runner = CliRunner()


def stdio(cmd: list[str]) -> dict:
    return {"command": cmd[0], "args": cmd[1:]}


def run(ws: Path, *args: str):
    return runner.invoke(app, ["--no-daemon", "-w", str(ws), *args])


@pytest.fixture
def ws(write_config, bookshop_stdio_cmd, weather_stdio_cmd) -> Path:
    return write_config(
        global_servers={
            "bookshop": stdio(bookshop_stdio_cmd) | {"description": "Books and reviews"},
            "weather": stdio(weather_stdio_cmd) | {"description": "Weather forecasts"},
        }
    )


def cache_files(prefix: str) -> list[Path]:
    return list(paths.cache_dir().glob(f"{prefix}-*.json"))


def test_ranks_search_books_first_and_populates_cache(ws: Path) -> None:
    result = run(ws, "search", "find a book by author")
    assert result.exit_code == 0, result.stderr
    first = result.stdout.splitlines()[0]
    assert first.startswith("bookshop/search_books")
    assert cache_files("bookshop") and cache_files("weather")
    assert "bookshop starting" not in result.stdout


def test_search_uses_warm_cache_without_connecting(ws: Path) -> None:
    assert run(ws, "search", "book").exit_code == 0
    before = {p: p.stat().st_mtime_ns for p in cache_files("bookshop") + cache_files("weather")}
    assert len(before) == 2
    result = run(ws, "search", "find a book by author")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("bookshop/search_books")
    assert {p: p.stat().st_mtime_ns for p in before} == before


def test_limit_and_type_tool_default(ws: Path) -> None:
    result = run(ws, "search", "search", "--limit", "1")
    assert result.exit_code == 0, result.stderr
    assert len(result.stdout.strip().splitlines()) == 1


def test_type_server(ws: Path) -> None:
    result = run(ws, "search", "weather forecast", "--type", "server")
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].startswith("weather")
    assert "tools:" in lines[1]


def test_server_filter(ws: Path) -> None:
    result = run(ws, "search", "search", "--server", "weather")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip()
    assert all(line.startswith("weather/") for line in result.stdout.splitlines())


def test_server_filter_with_type_server(ws: Path) -> None:
    result = run(ws, "--json", "search", "search", "--server", "weather", "--type", "server")
    assert result.exit_code == 0, result.stderr
    assert [r["server"] for r in json.loads(result.stdout)["results"]] == ["weather"]


def test_unknown_server_filter_is_usage_error(ws: Path) -> None:
    result = run(ws, "search", "book", "--server", "nope")
    assert result.exit_code == 2
    assert "unknown server 'nope'" in result.stderr


def test_invalid_type(ws: Path) -> None:
    result = run(ws, "search", "book", "--type", "banana")
    assert result.exit_code == 2


def test_no_matches(ws: Path) -> None:
    result = run(ws, "search", "zzzqqq")
    assert result.exit_code == 0
    assert result.stdout.strip() == 'no matches for "zzzqqq"'


def test_no_matches_json(ws: Path) -> None:
    result = run(ws, "--json", "search", "zzzqqq")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"query": "zzzqqq", "type": "tool", "results": []}


def test_json_output_shape(ws: Path) -> None:
    result = run(ws, "--json", "search", "find a book by author")
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["query"] == "find a book by author"
    assert data["type"] == "tool"
    top = data["results"][0]
    assert (top["server"], top["tool"]) == ("bookshop", "search_books")
    assert top["score"] > 0 and top["summary"]


def test_json_server_type(ws: Path) -> None:
    result = run(ws, "--json", "search", "book", "--type", "server")
    data = json.loads(result.stdout)
    assert data["type"] == "server"
    assert data["results"][0]["server"] == "bookshop"
    assert data["results"][0]["top_tools"]


def test_untrusted_workspace_server_is_not_started(
    write_config, bookshop_stdio_cmd, weather_stdio_cmd
) -> None:
    ws = write_config(
        global_servers={"weather": stdio(weather_stdio_cmd)},
        workspace_servers={"wsbooks": stdio(bookshop_stdio_cmd)},
    )
    result = run(ws, "search", "forecast")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("weather/forecast")
    assert cache_files("weather")
    assert cache_files("wsbooks") == []
    assert "wsbooks" not in result.stdout


def test_untrusted_server_uses_existing_cache(write_config, bookshop_stdio_cmd, monkeypatch) -> None:
    ws = write_config(workspace_servers={"wsbooks": stdio(bookshop_stdio_cmd)})
    monkeypatch.setenv("MCPC_TRUST_ALL", "1")
    assert run(ws, "search", "book").exit_code == 0
    assert cache_files("wsbooks")
    monkeypatch.delenv("MCPC_TRUST_ALL")
    result = run(ws, "search", "find a book by author")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("wsbooks/search_books")


def test_failing_server_only_warns(write_config, bookshop_stdio_cmd) -> None:
    ws = write_config(
        global_servers={
            "bookshop": stdio(bookshop_stdio_cmd),
            "broken": {"command": "definitely-not-a-real-command-xyz"},
        }
    )
    result = run(ws, "search", "find a book by author")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("bookshop/search_books")
    assert "warning" in result.stderr and "broken" in result.stderr


def test_unresolvable_server_only_warns(write_config, bookshop_stdio_cmd, monkeypatch) -> None:
    monkeypatch.delenv("MCPC_TEST_MISSING_VAR", raising=False)
    ws = write_config(
        global_servers={
            "bookshop": stdio(bookshop_stdio_cmd),
            "needsenv": {"command": "x", "env": {"K": "${env:MCPC_TEST_MISSING_VAR}"}},
        }
    )
    result = run(ws, "search", "book")
    assert result.exit_code == 0, result.stderr
    assert "needsenv" in result.stderr


def test_refresh_flag_reconnects(ws: Path) -> None:
    assert run(ws, "search", "book").exit_code == 0
    before = {p: p.stat().st_mtime_ns for p in cache_files("bookshop")}
    result = run(ws, "--refresh", "search", "book")
    assert result.exit_code == 0, result.stderr
    after = cache_files("bookshop")
    assert after and any(p.stat().st_mtime_ns != before.get(p) for p in after)
