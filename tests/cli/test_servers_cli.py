"""CLI tests for the server-management commands (list, show, refresh, check, config, trust, untrust)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import typer.testing
from typer.testing import CliRunner

from mcpc.cli.app import app
from mcpc.output.schemas import ServerListOutput

BAD_COMMAND = "mcpc-definitely-not-a-command-xyz"

Invoke = Callable[..., typer.testing.Result]
WriteConfig = Callable[..., Path]


def stdio(cmd: list[str], **extra: Any) -> dict[str, Any]:
    return {"command": cmd[0], "args": cmd[1:], **extra}


@pytest.fixture
def ws(write_config: WriteConfig, bookshop_stdio_cmd: list[str], weather_stdio_cmd: list[str]) -> Path:
    """Workspace with two healthy global servers."""
    return write_config(
        global_servers={
            "bookshop": stdio(bookshop_stdio_cmd, description="Books and reviews"),
            "weather": stdio(weather_stdio_cmd),
        }
    )


@pytest.fixture
def invoke() -> Invoke:
    runner = CliRunner()

    def run(ws_path: Path, *args: str, input: str | None = None) -> typer.testing.Result:
        return runner.invoke(app, ["--no-daemon", "-w", str(ws_path), *args], input=input)

    return run


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the CliRunner's stdin look like a terminal."""
    monkeypatch.setattr(typer.testing._NamedTextIOWrapper, "isatty", lambda self: True)


# ---- list ------------------------------------------------------------------------------------------


def test_list_compact_without_cache_does_not_connect(ws: Path, invoke: Invoke, mcpc_home: Path) -> None:
    result = invoke(ws, "list")
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].startswith("bookshop") and "Books and reviews" in lines[0]
    assert lines[1].startswith("weather") and "(no description)" in lines[1]
    assert not (mcpc_home / "cache").exists() or not list((mcpc_home / "cache").iterdir())


def test_list_full_shows_not_cached_and_scope(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "list", "--full")
    assert result.exit_code == 0
    assert "status:      not cached" in result.stdout
    assert "scope:       global" in result.stdout
    assert "transport:   stdio" in result.stdout


def test_list_refresh_connects_and_reports_counts(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "--refresh", "list", "--full")
    assert result.exit_code == 0, result.stderr
    assert "status:      ok (protocol" in result.stdout
    assert "5 tools" in result.stdout
    assert "bookshop starting" not in result.stdout
    # The refresh filled the cache, so a plain list now reports ok without connecting.
    again = invoke(ws, "list", "--full")
    assert "status:      ok" in again.stdout
    assert "cached 0.0h ago" in again.stdout


def test_list_refresh_error_row_keeps_exit_zero(
    write_config: WriteConfig, bookshop_stdio_cmd: list[str], invoke: Invoke
) -> None:
    ws = write_config(
        global_servers={
            "bookshop": stdio(bookshop_stdio_cmd),
            "broken": {"command": BAD_COMMAND},
            "needsenv": {"command": "x", "env": {"TOKEN": "${env:MCPC_W7_UNSET_VARIABLE}"}},
        }
    )
    result = invoke(ws, "--json", "--refresh", "list")
    assert result.exit_code == 0, result.stderr
    out = ServerListOutput.model_validate(json.loads(result.stdout))
    rows = {r.name: r for r in out.servers}
    assert rows["bookshop"].status == "ok" and rows["bookshop"].tools == 5
    assert rows["broken"].status == "error" and rows["broken"].error
    assert rows["needsenv"].status == "error"
    assert "MCPC_W7_UNSET_VARIABLE" in (rows["needsenv"].error or "")
    # Compact text marks failing servers.
    text = invoke(ws, "list")
    assert text.exit_code == 0
    assert re.search(r"^broken\s+.*\[error\]$", text.stdout, re.MULTILINE)


def test_list_statuses_untrusted_disabled_and_all(
    write_config: WriteConfig, bookshop_stdio_cmd: list[str], invoke: Invoke
) -> None:
    ws = write_config(
        global_servers={
            "bookshop": stdio(bookshop_stdio_cmd),
            "off": {**stdio(bookshop_stdio_cmd), "disabled": True},
        },
        workspace_servers={"docs": stdio(bookshop_stdio_cmd, description="Repo docs")},
    )
    default = json.loads(invoke(ws, "--json", "list").stdout)["servers"]
    assert {r["name"]: r["status"] for r in default} == {"bookshop": "not cached", "docs": "untrusted"}
    with_all = json.loads(invoke(ws, "--json", "list", "--all").stdout)["servers"]
    assert {r["name"]: r["status"] for r in with_all} == {
        "bookshop": "not cached",
        "off": "disabled",
        "docs": "untrusted",
    }
    compact = invoke(ws, "list", "--all").stdout
    assert re.search(r"^docs\s+Repo docs \[untrusted\]$", compact, re.MULTILINE)
    assert re.search(r"^off\s+.*\[disabled\]$", compact, re.MULTILINE)


def test_list_refresh_skips_untrusted_servers(
    write_config: WriteConfig, bookshop_stdio_cmd: list[str], invoke: Invoke, mcpc_home: Path
) -> None:
    ws = write_config(global_servers={}, workspace_servers={"docs": stdio(bookshop_stdio_cmd)})
    result = invoke(ws, "--json", "--refresh", "list")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["servers"][0]["status"] == "untrusted"
    assert not list((mcpc_home / "cache").glob("docs-*.json"))


def test_list_empty_config(write_config: WriteConfig, invoke: Invoke) -> None:
    ws = write_config(global_servers={})
    assert "No MCP servers configured" in invoke(ws, "list").stdout
    assert json.loads(invoke(ws, "--json", "list").stdout) == {"servers": []}


def test_list_redacts_secrets_in_target(write_config: WriteConfig, invoke: Invoke) -> None:
    ws = write_config(
        global_servers={
            "api": {
                "url": "https://user:hunter2@example.com/mcp",
                "headers": {"Authorization": "Bearer abc123"},
            }
        }
    )
    result = invoke(ws, "list", "--full")
    assert "hunter2" not in result.stdout and "abc123" not in result.stdout
    assert "https://***@example.com/mcp" in result.stdout


# ---- show ------------------------------------------------------------------------------------------


def test_show_server_compact_and_server_stderr_stays_out(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "show", "bookshop")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("bookshop: Books and reviews")
    assert "tools (5):" in result.stdout
    assert "  search_books: Search the catalog by title or author." in result.stdout
    assert "bookshop starting" not in result.stdout
    assert "bookshop starting" not in result.stderr


def test_show_server_full(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "show", "bookshop", "--full")
    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert "server: Bookshop" in out
    assert "parameters:" in out
    assert "resources (" in out and "prompts (" in out
    assert "catalog://genres" in out


def test_show_server_json_compact_and_full(ws: Path, invoke: Invoke) -> None:
    compact = json.loads(invoke(ws, "--json", "show", "bookshop").stdout)
    assert compact["name"] == "bookshop" and compact["status"] == "ok"
    assert {"name": "search_books", "summary": "Search the catalog by title or author."} in compact["tools"]
    full = json.loads(invoke(ws, "--json", "show", "bookshop", "--full").stdout)
    first = next(t for t in full["tools"] if t["name"] == "search_books")
    assert first["input_schema"]["properties"]["query"]["type"] == "string"
    assert full["resources"] and full["prompts"]


def test_show_uses_cache_without_reconnecting(
    ws: Path, invoke: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke(ws, "refresh", "bookshop").exit_code == 0
    calls: list[str] = []
    import mcpc.client.direct as direct

    original = direct.DirectExecutor.snapshot

    async def spy(self: Any, server: Any, prior: Any = None) -> Any:
        calls.append(server.name)
        return await original(self, server, prior)

    monkeypatch.setattr(direct.DirectExecutor, "snapshot", spy)
    result = invoke(ws, "show", "bookshop")
    assert result.exit_code == 0
    assert calls == []


def test_show_tool_detail_and_json(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "show", "bookshop/search_books")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("bookshop/search_books")
    assert "query  string  required" in result.stdout
    assert "example: mcpc call bookshop/search_books -a query=" in result.stdout
    data = json.loads(invoke(ws, "--json", "show", "bookshop/search_books").stdout)
    assert data["server"] == "bookshop" and data["name"] == "search_books"
    assert data["input_schema"]["required"] == ["query"]


def test_show_unknown_tool_suggests_close_match(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "show", "bookshop/serch_books")
    assert result.exit_code == 2
    assert "unknown tool 'serch_books'" in result.stderr
    assert "Did you mean: search_books" in result.stderr
    assert result.stdout == ""


def test_show_unknown_tool_lists_available_when_nothing_is_close(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "show", "bookshop/zzzzzzzz")
    assert result.exit_code == 2
    assert "Available tools:" in result.stderr and "search_books" in result.stderr


def test_show_unknown_tool_json_error_on_stdout(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "--json", "show", "bookshop/nope1234")
    assert result.exit_code == 2
    err = json.loads(result.stdout)["error"]
    assert err["kind"] == "not_found" and err["server"] == "bookshop"


def test_show_missing_tool_forces_one_refresh_when_cache_was_used(
    ws: Path, invoke: Invoke, mcpc_home: Path
) -> None:
    assert invoke(ws, "refresh", "bookshop").exit_code == 0
    cache_file = next((mcpc_home / "cache").glob("bookshop-*.json"))
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    data["tools"] = [t for t in data["tools"] if t["name"] != "lookup_book"]
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    result = invoke(ws, "show", "bookshop/lookup_book")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("bookshop/lookup_book")


def test_show_unknown_server(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "show", "bookshp")
    assert result.exit_code == 2
    assert "unknown server 'bookshp'" in result.stderr and "bookshop" in result.stderr


def test_show_rejects_malformed_target(ws: Path, invoke: Invoke) -> None:
    assert invoke(ws, "show", "bookshop/").exit_code == 2


def test_show_connection_failure_without_cache(write_config: WriteConfig, invoke: Invoke) -> None:
    ws = write_config(global_servers={"broken": {"command": BAD_COMMAND}})
    result = invoke(ws, "show", "broken")
    assert result.exit_code == 3
    assert result.stderr.startswith("mcpc: broken:")


def test_show_falls_back_to_stale_cache_with_warning(
    write_config: WriteConfig,
    invoke: Invoke,
    bookshop_stdio_cmd: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The command comes from the environment: the raw spec (cache key) stays the same while it breaks.
    ws = write_config(
        global_servers={"bookshop": {"command": "${env:MCPC_W7_CMD}", "args": bookshop_stdio_cmd[1:]}}
    )
    monkeypatch.setenv("MCPC_W7_CMD", bookshop_stdio_cmd[0])
    assert invoke(ws, "refresh").exit_code == 0
    monkeypatch.setenv("MCPC_W7_CMD", BAD_COMMAND)
    result = invoke(ws, "--refresh", "show", "bookshop")
    assert result.exit_code == 0, result.stderr
    assert "refresh failed" in result.stderr and "showing the cached catalog" in result.stderr
    assert "search_books" in result.stdout
    assert "last refresh failed" in result.stdout


def test_show_untrusted_without_cache_exits_5(
    write_config: WriteConfig, bookshop_stdio_cmd: list[str], invoke: Invoke
) -> None:
    ws = write_config(global_servers={}, workspace_servers={"docs": stdio(bookshop_stdio_cmd)})
    result = invoke(ws, "show", "docs")
    assert result.exit_code == 5
    assert 'run "mcpc trust"' in result.stderr


def test_show_untrusted_with_cache_renders_cache_and_warns(
    write_config: WriteConfig,
    bookshop_stdio_cmd: list[str],
    invoke: Invoke,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = write_config(global_servers={}, workspace_servers={"docs": stdio(bookshop_stdio_cmd)})
    monkeypatch.setenv("MCPC_TRUST_ALL", "1")
    assert invoke(ws, "refresh").exit_code == 0
    monkeypatch.delenv("MCPC_TRUST_ALL")
    result = invoke(ws, "show", "docs")
    assert result.exit_code == 0, result.stderr
    assert "search_books" in result.stdout
    assert "not trusted" in result.stderr
    as_json = json.loads(invoke(ws, "--json", "show", "docs").stdout)
    assert as_json["status"] == "untrusted"


# ---- refresh / check -------------------------------------------------------------------------------


def test_refresh_all_ok(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "refresh")
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert re.fullmatch(r"bookshop  ok  5 tools  \d+\.\ds", lines[0])
    assert re.fullmatch(r"weather  ok  2 tools  \d+\.\ds", lines[1])
    assert "bookshop starting" not in result.stdout


def test_refresh_named_server_only(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "refresh", "weather")
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [result.stdout.strip()]
    assert result.stdout.startswith("weather  ok")


def test_refresh_failure_exit_code_is_first_failing_server(
    write_config: WriteConfig, bookshop_stdio_cmd: list[str], invoke: Invoke
) -> None:
    ws = write_config(
        global_servers={
            "bookshop": stdio(bookshop_stdio_cmd),
            "broken": {"command": BAD_COMMAND},
            "disabled": {"command": "x", "disabled": True},
        }
    )
    result = invoke(ws, "refresh")
    assert result.exit_code == 3
    assert result.stdout.splitlines()[0].startswith("bookshop  ok  5 tools")
    assert result.stdout.splitlines()[1].startswith("broken  error  ")
    # An explicitly named disabled server is a usage error (exit 2) that comes first in config order.
    named = invoke(ws, "refresh", "broken", "disabled")
    assert named.exit_code == 3
    assert invoke(ws, "refresh", "disabled").exit_code == 2


def test_refresh_json_shape(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "--json", "refresh")
    assert result.exit_code == 0, result.stderr
    results = json.loads(result.stdout)["results"]
    assert [r["name"] for r in results] == ["bookshop", "weather"]
    assert all(set(r) == {"name", "ok", "tools", "elapsed", "error"} for r in results)
    assert results[0]["ok"] is True and results[0]["tools"] == 5 and results[0]["error"] is None


def test_refresh_json_failure_still_prints_results(write_config: WriteConfig, invoke: Invoke) -> None:
    ws = write_config(global_servers={"broken": {"command": BAD_COMMAND}})
    result = invoke(ws, "--json", "refresh")
    assert result.exit_code == 3
    record = json.loads(result.stdout)["results"][0]
    assert record["ok"] is False and record["tools"] is None and record["error"]


def test_refresh_untrusted_is_listed_and_fails_with_5(
    write_config: WriteConfig, bookshop_stdio_cmd: list[str], invoke: Invoke
) -> None:
    ws = write_config(global_servers={}, workspace_servers={"docs": stdio(bookshop_stdio_cmd)})
    result = invoke(ws, "refresh")
    assert result.exit_code == 5
    assert result.stdout.strip() == 'docs  untrusted  run "mcpc trust"'


def test_refresh_unknown_server(ws: Path, invoke: Invoke) -> None:
    assert invoke(ws, "refresh", "nope").exit_code == 2


def test_check_reports_protocol_server_latency_capabilities(ws: Path, invoke: Invoke) -> None:
    result = invoke(ws, "check", "bookshop")
    assert result.exit_code == 0, result.stderr
    assert re.fullmatch(
        r"bookshop  ok  \S+  Bookshop  \d+\.\d\ds  (\w+,)*tools(,\w+)*", result.stdout.strip()
    ), result.stdout


def test_check_failure_exit_code_and_json(
    write_config: WriteConfig, bookshop_stdio_cmd: list[str], invoke: Invoke
) -> None:
    ws = write_config(
        global_servers={"bookshop": stdio(bookshop_stdio_cmd), "broken": {"command": BAD_COMMAND}}
    )
    text = invoke(ws, "check")
    assert text.exit_code == 3
    assert "broken  error  " in text.stdout
    data = invoke(ws, "--json", "check")
    assert data.exit_code == 3
    results = {r["name"]: r for r in json.loads(data.stdout)["results"]}
    assert results["bookshop"]["ok"] is True
    assert results["bookshop"]["server_name"] == "Bookshop"
    assert "tools" in results["bookshop"]["capabilities"]
    assert results["bookshop"]["protocol_version"]
    assert results["broken"]["ok"] is False


def test_check_saves_snapshot(ws: Path, invoke: Invoke, mcpc_home: Path) -> None:
    assert invoke(ws, "check", "weather").exit_code == 0
    assert list((mcpc_home / "cache").glob("weather-*.json"))


# ---- config ----------------------------------------------------------------------------------------


@pytest.fixture
def merged_ws(write_config: WriteConfig, bookshop_stdio_cmd: list[str]) -> Path:
    return write_config(
        global_servers={
            "api": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer s3cret"}},
            "shared": {"command": "global-cmd"},
            "bad name!": {"command": "x"},
        },
        workspace_servers={
            "shared": {"command": "ws-cmd", "args": ["--flag"], "env": {"KEY": "value-to-hide"}},
            "docs": stdio(bookshop_stdio_cmd),
        },
    )


def test_config_text(merged_ws: Path, invoke: Invoke) -> None:
    result = invoke(merged_ws, "config")
    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert re.search(r"^api  global  .*mcp\.json  streamable-http  https://example\.com/mcp$", out, re.M)
    assert re.search(r"^shared  workspace  .*mcp\.json  stdio  ws-cmd --flag$", out, re.M)
    assert re.search(r"^docs  workspace  ", out, re.M)
    assert "warnings:" in out and "bad name!" in out
    assert "s3cret" not in out and "value-to-hide" not in out


def test_config_json(merged_ws: Path, invoke: Invoke) -> None:
    result = invoke(merged_ws, "--json", "config")
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert set(data) == {"workspace_root", "global_path", "workspace_path", "servers", "warnings"}
    assert data["workspace_root"] == str(merged_ws.resolve())
    servers = {s["name"]: s for s in data["servers"]}
    assert servers["shared"]["scope"] == "workspace" and servers["shared"]["overrides_global"] is True
    assert servers["api"]["spec"]["headers"] == {"Authorization": "***"}
    assert servers["shared"]["spec"]["env"] == {"KEY": "***"}
    assert servers["api"]["disabled"] is False
    assert any("bad name!" in w for w in data["warnings"])
    assert "s3cret" not in result.stdout and "value-to-hide" not in result.stdout


def test_config_path_text_and_json(merged_ws: Path, invoke: Invoke, mcpc_home: Path) -> None:
    result = invoke(merged_ws, "config", "path")
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == f"global: {mcpc_home / 'mcp.json'} (exists)"
    assert lines[1] == f"workspace: {merged_ws.resolve() / '.mcpc' / 'mcp.json'} (exists)"
    assert lines[2] == f"workspace root: {merged_ws.resolve()}"
    data = json.loads(invoke(merged_ws, "--json", "config", "path").stdout)
    assert data["global_exists"] is True and data["workspace_exists"] is True


def test_config_path_reports_missing_files(tmp_path: Path, invoke: Invoke) -> None:
    empty = tmp_path / "empty-ws"
    empty.mkdir()
    result = invoke(empty, "config", "path")
    assert result.exit_code == 0
    assert "global: " in result.stdout and "(missing)" in result.stdout
    assert result.stdout.count("(missing)") == 2


# ---- trust / untrust -------------------------------------------------------------------------------


@pytest.fixture
def untrusted_ws(write_config: WriteConfig, bookshop_stdio_cmd: list[str]) -> Path:
    return write_config(global_servers={}, workspace_servers={"docs": stdio(bookshop_stdio_cmd)})


def test_trust_refuses_without_tty_even_with_yes(untrusted_ws: Path, invoke: Invoke) -> None:
    for args in (["trust"], ["trust", "--yes"], ["trust", "-y"]):
        result = invoke(untrusted_ws, *args, input="y\n")
        assert result.exit_code == 2
        assert "mcpc trust needs an interactive terminal" in result.stderr
        assert str(untrusted_ws.resolve()) in result.stderr
        assert "trusted" not in result.stdout
    still = json.loads(invoke(untrusted_ws, "--json", "list").stdout)["servers"][0]
    assert still["status"] == "untrusted"


def test_trust_nothing_to_trust(write_config: WriteConfig, invoke: Invoke) -> None:
    ws = write_config(global_servers={"g": {"command": "x"}})
    result = invoke(ws, "trust")  # no TTY needed when there is nothing to approve
    assert result.exit_code == 0
    assert result.stdout.strip() == f"nothing to trust in {ws.resolve()}"


def test_trust_with_tty_and_yes_answer(untrusted_ws: Path, invoke: Invoke, tty: None) -> None:
    result = invoke(untrusted_ws, "trust", input="y\n")
    assert result.exit_code == 0, result.stderr
    root = str(untrusted_ws.resolve())
    assert (
        f"The workspace {root} defines MCP servers that run commands or send your environment variables:"
        in (result.stdout)
    )
    assert re.search(r"^  docs: \S+ \S+bookshop_server\.py$", result.stdout, re.M)
    assert "Trust this workspace configuration? [y/N]" in result.stdout
    assert f"trusted {root}" in result.stdout
    status = json.loads(invoke(untrusted_ws, "--json", "list").stdout)["servers"][0]["status"]
    assert status == "not cached"


def test_trust_then_commands_connect(untrusted_ws: Path, invoke: Invoke, tty: None) -> None:
    assert invoke(untrusted_ws, "refresh").exit_code == 5
    assert invoke(untrusted_ws, "trust", input="y\n").exit_code == 0
    result = invoke(untrusted_ws, "refresh")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("docs  ok  5 tools")


def test_trust_declined(untrusted_ws: Path, invoke: Invoke, tty: None) -> None:
    for answer in ("n\n", "\n", ""):
        result = invoke(untrusted_ws, "trust", input=answer)
        assert result.exit_code == 1
        assert f"trusted {untrusted_ws.resolve()}" not in result.stdout.replace("not trusted", "")
    assert invoke(untrusted_ws, "refresh").exit_code == 5


def test_trust_yes_skips_prompt_in_tty(untrusted_ws: Path, invoke: Invoke, tty: None) -> None:
    result = invoke(untrusted_ws, "trust", "--yes")
    assert result.exit_code == 0, result.stderr
    assert "Trust this workspace configuration?" not in result.stdout
    assert f"trusted {untrusted_ws.resolve()}" in result.stdout


def test_trust_json_keeps_stdout_parseable(untrusted_ws: Path, invoke: Invoke, tty: None) -> None:
    result = invoke(untrusted_ws, "--json", "trust", input="y\n")
    assert result.exit_code == 0, result.stderr
    # CliRunner echoes the typed answer to stdout; a real terminal shows it on the tty, not in the pipe.
    payload = result.stdout[result.stdout.index("{") :]
    assert "Trust this workspace" not in result.stdout
    assert json.loads(payload) == {
        "workspace_root": str(untrusted_ws.resolve()),
        "trusted": True,
        "changed": True,
    }


def test_trust_is_revoked_when_config_changes(untrusted_ws: Path, invoke: Invoke, tty: None) -> None:
    assert invoke(untrusted_ws, "trust", "--yes").exit_code == 0
    config_file = untrusted_ws / ".mcpc" / "mcp.json"
    data = json.loads(config_file.read_text(encoding="utf-8"))
    data["mcpServers"]["extra"] = {"command": "evil"}
    config_file.write_text(json.dumps(data), encoding="utf-8")
    statuses = {
        r["name"]: r["status"] for r in json.loads(invoke(untrusted_ws, "--json", "list").stdout)["servers"]
    }
    assert statuses == {"docs": "untrusted", "extra": "untrusted"}


def test_untrust(untrusted_ws: Path, invoke: Invoke, tty: None) -> None:
    root = str(untrusted_ws.resolve())
    assert invoke(untrusted_ws, "untrust").stdout.strip() == f"{root} was not trusted"
    assert invoke(untrusted_ws, "trust", "--yes").exit_code == 0
    result = invoke(untrusted_ws, "untrust")
    assert result.exit_code == 0
    assert result.stdout.strip() == f"untrusted {root}"
    status = json.loads(invoke(untrusted_ws, "--json", "list").stdout)["servers"][0]["status"]
    assert status == "untrusted"


def test_untrust_json(untrusted_ws: Path, invoke: Invoke) -> None:
    data = json.loads(invoke(untrusted_ws, "--json", "untrust").stdout)
    assert data == {"workspace_root": str(untrusted_ws.resolve()), "trusted": False, "changed": False}
