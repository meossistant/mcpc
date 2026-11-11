"""CLI tests for call, resources, read, prompts and prompt against the real fixture servers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mcpc import paths
from mcpc.cli.app import app
from mcpc.client.direct import DirectExecutor
from mcpc.output.schemas import CallOutput

runner = CliRunner()

PNG_BASE64_PREFIX = "iVBORw0KGgo"


def stdio(cmd: list[str]) -> dict:
    return {"command": cmd[0], "args": cmd[1:]}


def run(ws: Path, *args: str, input: str | None = None):
    return runner.invoke(app, ["--no-daemon", "-w", str(ws), *args], input=input)


@pytest.fixture
def ws(write_config, bookshop_stdio_cmd, weather_stdio_cmd) -> Path:
    return write_config(
        global_servers={"bookshop": stdio(bookshop_stdio_cmd), "weather": stdio(weather_stdio_cmd)}
    )


@pytest.fixture
def sent_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Record every tool call that actually reaches the executor."""
    calls: list[tuple[str, dict]] = []
    original = DirectExecutor.call_tool

    async def spy(self, server, tool, arguments, prior=None):  # type: ignore[no-untyped-def]
        calls.append((tool, dict(arguments)))
        return await original(self, server, tool, arguments, prior)

    monkeypatch.setattr(DirectExecutor, "call_tool", spy)
    return calls


# ---- call: success paths ----------------------------------------------------------------------


def test_call_with_arg_pairs(ws: Path) -> None:
    result = run(ws, "call", "bookshop/search_books", "-a", "query=dune")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "Found 3 books matching 'dune' (showing up to 10)."
    assert "bookshop starting" not in result.stdout


def test_call_populates_cache(ws: Path) -> None:
    assert run(ws, "call", "bookshop/search_books", "-a", "query=dune").exit_code == 0
    assert list(paths.cache_dir().glob("bookshop-*.json"))


def test_call_with_args_json(ws: Path) -> None:
    result = run(ws, "call", "bookshop/search_books", "--args", '{"query":"dune","limit":2}')
    assert result.exit_code == 0, result.stderr
    assert "showing up to 2" in result.stdout


def test_call_with_short_json_flag_and_pairs_override(ws: Path) -> None:
    result = run(ws, "call", "bookshop/search_books", "-j", '{"query":"dune","limit":2}', "-a", "limit=5")
    assert result.exit_code == 0, result.stderr
    assert "showing up to 5" in result.stdout


def test_call_with_args_file(ws: Path, tmp_path: Path) -> None:
    f = tmp_path / "args.json"
    f.write_text('{"query": "emma", "limit": 4}', encoding="utf-8")
    result = run(ws, "call", "bookshop/search_books", "--args", f"@{f}")
    assert result.exit_code == 0, result.stderr
    assert "'emma'" in result.stdout and "up to 4" in result.stdout


def test_call_with_args_from_stdin(ws: Path) -> None:
    result = run(ws, "call", "bookshop/search_books", "--args", "-", input='{"query":"piped"}')
    assert result.exit_code == 0, result.stderr
    assert "'piped'" in result.stdout


def test_call_two_positional_form(ws: Path) -> None:
    result = run(ws, "call", "bookshop", "search_books", "-a", "query=dune")
    assert result.exit_code == 0, result.stderr
    assert "'dune'" in result.stdout


def test_call_both_forms_is_usage_error(ws: Path) -> None:
    result = run(ws, "call", "bookshop/search_books", "search_books", "-a", "query=x")
    assert result.exit_code == 2


def test_call_missing_tool_is_usage_error(ws: Path) -> None:
    result = run(ws, "call", "bookshop")
    assert result.exit_code == 2


def test_string_argument_stays_a_string(ws: Path, sent_calls: list) -> None:
    result = run(ws, "call", "bookshop/lookup_book", "-a", "title=1965")
    # Tool-level failure (no such title), not a validation failure, and the value was sent as text.
    assert result.exit_code == 1, result.stderr
    assert "1965" in result.stdout
    assert sent_calls == [("lookup_book", {"title": "1965"})]


def test_int_argument_is_parsed(ws: Path, sent_calls: list) -> None:
    assert run(ws, "call", "bookshop/search_books", "-a", "query=dune", "-a", "limit=3").exit_code == 0
    assert sent_calls == [("search_books", {"query": "dune", "limit": 3})]


def test_structured_output_when_no_text_block(ws: Path) -> None:
    result = run(ws, "call", "bookshop/lookup_book", "-a", "title=Dune")
    assert result.exit_code == 0, result.stderr
    assert "Frank Herbert" in result.stdout


# ---- call: failures ---------------------------------------------------------------------------


def test_tool_error_exits_1_with_text_on_stdout(ws: Path) -> None:
    result = run(ws, "call", "bookshop/lookup_book", "-a", "title=X")
    assert result.exit_code == 1
    assert "No book titled 'X'" in result.stdout
    assert result.stdout.startswith("error:")


def test_tool_error_json_exits_1(ws: Path) -> None:
    result = run(ws, "--json", "call", "bookshop/lookup_book", "-a", "title=X")
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["is_error"] is True
    assert "No book titled" in data["content"][0]["text"]


def test_missing_required_argument_sends_nothing(ws: Path, sent_calls: list) -> None:
    result = run(ws, "call", "bookshop/search_books")
    assert result.exit_code == 2
    assert "query" in result.stderr
    assert result.stdout == ""
    assert sent_calls == []


def test_wrong_type_sends_nothing(ws: Path, sent_calls: list) -> None:
    result = run(ws, "call", "bookshop/search_books", "-a", "query=dune", "-a", "limit=many")
    assert result.exit_code == 2
    assert sent_calls == []


def test_no_validate_sends_anyway(ws: Path, sent_calls: list) -> None:
    result = run(ws, "call", "bookshop/search_books", "--no-validate")
    assert result.exit_code == 1  # the server rejects the call and reports is_error
    assert sent_calls == [("search_books", {})]
    assert result.stdout.strip()


def test_unknown_tool_suggests_close_match(ws: Path, sent_calls: list) -> None:
    result = run(ws, "call", "bookshop/search_book", "-a", "query=x")
    assert result.exit_code == 2
    assert "unknown tool 'search_book'" in result.stderr
    assert "search_books" in result.stderr
    assert sent_calls == []


def test_unknown_server(ws: Path) -> None:
    result = run(ws, "call", "bookshp/search_books", "-a", "query=x")
    assert result.exit_code == 2
    assert "unknown server 'bookshp'" in result.stderr
    assert "bookshop" in result.stderr


def test_unknown_server_json_error_on_stdout(ws: Path) -> None:
    result = run(ws, "--json", "call", "nope/x")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["kind"] == "not_found"


def test_invalid_args_json(ws: Path) -> None:
    result = run(ws, "call", "bookshop/search_books", "--args", "{nope")
    assert result.exit_code == 2


def test_bad_command_exits_3(write_config) -> None:
    ws = write_config(global_servers={"ghost": {"command": "definitely-not-a-real-command-xyz"}})
    result = run(ws, "call", "ghost/anything")
    assert result.exit_code == 3
    assert "ghost" in result.stderr


def test_untrusted_workspace_server_exits_5(write_config, bookshop_stdio_cmd) -> None:
    ws = write_config(workspace_servers={"wsbooks": stdio(bookshop_stdio_cmd)})
    result = run(ws, "call", "wsbooks/search_books", "-a", "query=x")
    assert result.exit_code == 5
    assert "mcpc trust" in result.stderr
    assert list(paths.cache_dir().glob("wsbooks-*.json")) == []


# ---- call: output -----------------------------------------------------------------------------


def test_image_is_saved_to_out_dir(ws: Path, tmp_path: Path) -> None:
    out = tmp_path / "images" / "nested"
    result = run(ws, "call", "bookshop/cover_image", "--out-dir", str(out))
    assert result.exit_code == 0, result.stderr
    saved = list(out.glob("*.png"))
    assert len(saved) == 1
    assert str(saved[0]) in result.stdout
    assert PNG_BASE64_PREFIX not in result.stdout


def test_image_defaults_to_tmp_dir(ws: Path) -> None:
    result = run(ws, "call", "bookshop/cover_image")
    assert result.exit_code == 0, result.stderr
    assert list(paths.tmp_dir().glob("*.png"))


def test_json_shape(ws: Path) -> None:
    result = run(ws, "--json", "call", "bookshop/search_books", "-a", "query=dune")
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["is_error"] is False
    assert data["content"][0]["type"] == "text"
    assert "dune" in data["content"][0]["text"]
    parsed = CallOutput.model_validate(data)
    assert (parsed.server, parsed.tool) == ("bookshop", "search_books")


def test_json_image_has_path_not_base64(ws: Path, tmp_path: Path) -> None:
    out = tmp_path / "img"
    result = run(ws, "--json", "call", "bookshop/cover_image", "--out-dir", str(out))
    assert result.exit_code == 0, result.stderr
    block = json.loads(result.stdout)["content"][0]
    assert block["type"] == "image"
    assert Path(block["path"]).exists()
    assert PNG_BASE64_PREFIX not in result.stdout


def test_json_structured_content(ws: Path) -> None:
    data = json.loads(run(ws, "--json", "call", "bookshop/lookup_book", "-a", "title=Dune").stdout)
    assert data["structured_content"]["author"] == "Frank Herbert"


def test_max_output_truncates(ws: Path) -> None:
    result = run(ws, "call", "bookshop/search_books", "-a", "query=dune", "--max-output", "10")
    assert result.exit_code == 0, result.stderr
    assert "[output truncated: 10 of" in result.stdout
    assert result.stdout.startswith("Found 3 bo\n")


def test_call_over_http(write_config, bookshop_http_url) -> None:
    ws = write_config(global_servers={"web": {"url": bookshop_http_url}})
    result = run(ws, "call", "web/search_books", "-a", "query=dune")
    assert result.exit_code == 0, result.stderr
    assert "'dune'" in result.stdout


def test_cached_snapshot_is_reused(ws: Path) -> None:
    assert run(ws, "call", "bookshop/search_books", "-a", "query=a").exit_code == 0
    cache = next(paths.cache_dir().glob("bookshop-*.json"))
    before = cache.stat().st_mtime_ns
    assert run(ws, "call", "bookshop/search_books", "-a", "query=b").exit_code == 0
    assert cache.stat().st_mtime_ns == before


def test_snapshot_failure_warns_and_still_calls(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcpc.errors import ConnectionFailedError

    async def boom(self, server, prior=None):  # type: ignore[no-untyped-def]
        raise ConnectionFailedError("catalog unavailable", server=server.name)

    monkeypatch.setattr(DirectExecutor, "snapshot", boom)
    result = run(ws, "call", "bookshop/search_books", "-a", "query=dune")
    assert result.exit_code == 0, result.stderr
    assert "'dune'" in result.stdout
    assert "warning" in result.stderr and "catalog unavailable" in result.stderr


# ---- resources, read ---------------------------------------------------------------------------


def test_resources_text(ws: Path) -> None:
    result = run(ws, "resources", "bookshop")
    assert result.exit_code == 0, result.stderr
    assert "catalog://genres" in result.stdout
    assert "catalog://genres/{genre}" in result.stdout


def test_resources_json(ws: Path) -> None:
    data = json.loads(run(ws, "--json", "resources", "bookshop").stdout)
    assert data["server"] == "bookshop"
    assert [r["uri"] for r in data["resources"]] == ["catalog://genres"]
    assert data["resource_templates"][0]["uri_template"] == "catalog://genres/{genre}"


def test_resources_empty(ws: Path) -> None:
    result = run(ws, "resources", "weather")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "no resources"


def test_resources_unknown_server(ws: Path) -> None:
    assert run(ws, "resources", "nope").exit_code == 2


def test_read_resource(ws: Path) -> None:
    result = run(ws, "read", "bookshop", "catalog://genres")
    assert result.exit_code == 0, result.stderr
    assert "poetry" in result.stdout


def test_read_templated_resource_json(ws: Path) -> None:
    result = run(ws, "--json", "read", "bookshop", "catalog://genres/poetry")
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["uri"] == "catalog://genres/poetry"
    assert "3 books filed under poetry" in data["contents"][0]["text"]


def test_read_max_output(ws: Path) -> None:
    result = run(ws, "read", "bookshop", "catalog://genres/poetry", "--max-output", "5")
    assert result.exit_code == 0, result.stderr
    assert "[output truncated" in result.stdout


def test_read_unknown_resource_is_protocol_error(ws: Path) -> None:
    result = run(ws, "read", "bookshop", "catalog://nothing")
    assert result.exit_code != 0
    assert result.stdout == ""


# ---- prompts, prompt ---------------------------------------------------------------------------


def test_prompts_text(ws: Path) -> None:
    result = run(ws, "prompts", "bookshop")
    assert result.exit_code == 0, result.stderr
    assert "recommend" in result.stdout
    assert "genre" in result.stdout and "required" in result.stdout


def test_prompts_json(ws: Path) -> None:
    data = json.loads(run(ws, "--json", "prompts", "bookshop").stdout)
    assert data["prompts"][0]["name"] == "recommend"
    assert data["prompts"][0]["arguments"][0] == {"name": "genre", "required": True}


def test_prompts_empty(ws: Path) -> None:
    result = run(ws, "prompts", "weather")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "no prompts"


def test_prompt_renders(ws: Path) -> None:
    result = run(ws, "prompt", "bookshop/recommend", "-a", "genre=poetry")
    assert result.exit_code == 0, result.stderr
    assert "[user]" in result.stdout
    assert "Recommend one poetry book" in result.stdout


def test_prompt_two_positional_json(ws: Path) -> None:
    result = run(ws, "--json", "prompt", "bookshop", "recommend", "-a", "genre=fiction")
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert (data["server"], data["name"]) == ("bookshop", "recommend")
    assert "fiction" in data["messages"][0]["content"]["text"]


def test_prompt_missing_required_argument(ws: Path) -> None:
    result = run(ws, "prompt", "bookshop/recommend")
    assert result.exit_code == 2
    assert "genre" in result.stderr
    assert result.stdout == ""


def test_prompt_invalid_pair(ws: Path) -> None:
    assert run(ws, "prompt", "bookshop/recommend", "-a", "genre").exit_code == 2


# ---- stderr hygiene ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["call", "bookshop/search_books", "-a", "query=x"],
        ["resources", "bookshop"],
        ["read", "bookshop", "catalog://genres"],
        ["prompts", "bookshop"],
        ["prompt", "bookshop/recommend", "-a", "genre=fiction"],
        ["--json", "call", "bookshop/search_books", "-a", "query=x"],
    ],
)
def test_server_stderr_never_reaches_stdout(ws: Path, args: list[str]) -> None:
    result = run(ws, *args)
    assert result.exit_code == 0, result.stderr
    assert "bookshop starting" not in result.stdout
    assert "bookshop starting" not in result.stderr
