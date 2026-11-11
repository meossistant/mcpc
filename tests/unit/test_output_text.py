"""Text renderers. List/show/tool formats are pinned by golden files in tests/unit/golden/.

Regenerate goldens with `UPDATE_GOLDEN=1 pytest tests/unit/test_output_text.py` and review the diff.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.types import (
    AudioContent,
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    GetPromptResult,
    ImageContent,
    Implementation,
    PromptMessage,
    ReadResourceResult,
    ResourceLink,
    TextContent,
    TextResourceContents,
    Tool,
    ToolAnnotations,
)

from mcpc.catalog.models import ServerSnapshot
from mcpc.config.models import HttpServer, ServerConfig, StdioServer
from mcpc.errors import ConnectionFailedError, UsageError
from mcpc.output import text
from mcpc.output.schemas import ServerListRow

GOLDEN = Path(__file__).parent / "golden"
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
PNG_BYTES = 70


def assert_golden(name: str, actual: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(actual + "\n", encoding="utf-8", newline="\n")
    assert path.exists(), f"missing golden file {path}; run with UPDATE_GOLDEN=1"
    assert actual + "\n" == path.read_text(encoding="utf-8")


async def _bookshop_snapshot() -> ServerSnapshot:
    from bookshop_server import mcp as bookshop  # pyright: ignore[reportMissingImports]

    async with Client(bookshop) as client:
        return ServerSnapshot(
            server="bookshop",
            spec_hash="x",
            protocol_version="2026-07-28",
            server_info=Implementation(name="Bookshop", version="1.0.0"),
            instructions=client.instructions,
            tools=(await client.list_tools()).tools,
            resources=(await client.list_resources()).resources,
            resource_templates=(await client.list_resource_templates()).resource_templates,
            prompts=(await client.list_prompts()).prompts,
        )


@pytest.fixture(scope="module")
def snap() -> ServerSnapshot:
    return anyio.run(_bookshop_snapshot)


@pytest.fixture
def cfg() -> ServerConfig:
    return ServerConfig(
        name="bookshop",
        spec=StdioServer(command="python", args=["bookshop_server.py"]),
        scope="global",
        source=Path("g"),
    )


@pytest.fixture
def out(tmp_path: Path) -> Path:
    """Empty output dir (tmp_path itself also holds the MCPC_HOME dir)."""
    path = tmp_path / "out"
    path.mkdir()
    return path


def tool(snap: ServerSnapshot, name: str) -> Tool:
    found = snap.tool(name)
    assert found is not None
    return found


# --- list ----------------------------------------------------------------------------------------

ROWS = [
    ServerListRow(
        name="bookshop",
        description="Search the catalog before recommending a book.",
        scope="workspace",
        source=r"C:\x\.mcpc\mcp.json",
        transport="stdio",
        target="python bookshop_server.py",
        status="ok",
        protocol_version="2026-07-28",
        tools=5,
        resources=1,
        resource_templates=1,
        prompts=1,
        cache_age_hours=0.2,
        overrides_global=True,
    ),
    ServerListRow(
        name="gh",
        description="GitHub issues and pull requests",
        scope="global",
        source=r"C:\Users\me\.mcpc\mcp.json",
        transport="streamable-http",
        target="https://api.example.com/mcp",
        status="error",
        error="connection refused\nsecond line",
        live=False,
    ),
    ServerListRow(
        name="local-docs",
        description="Docs from the repo",
        scope="workspace",
        source=r"C:\x\.mcpc\mcp.json",
        transport="stdio",
        target="npx -y docs-server",
        status="untrusted",
        error='run "mcpc trust" in C:\\x to allow its MCP servers',
    ),
    ServerListRow(name="weather", description="", status="not cached", source="g", target="python w.py"),
    ServerListRow(
        name="old", description="Disabled one", status="disabled", transport="sse", target="http://h/sse"
    ),
]


def test_list_compact_golden() -> None:
    assert_golden("list_compact.txt", text.render_server_list(ROWS, full=False))


def test_list_full_golden() -> None:
    assert_golden("list_full.txt", text.render_server_list(ROWS, full=True))


def test_list_full_live_and_counts() -> None:
    row = ServerListRow(name="a", status="ok", tools=0, resources=2, prompts=3, live=True, source="g")
    out = text.render_server_list([row], full=True)
    assert "status:      ok (live in daemon)" in out
    assert "provides:    0 tools, 2 resources, 3 prompts" in out
    assert "description:" not in out  # lines without data are omitted


def test_list_empty() -> None:
    expected = "No MCP servers configured. Add them to ~/.mcpc/mcp.json or .mcpc/mcp.json."
    assert text.render_server_list([], full=False) == expected
    assert text.render_server_list([], full=True) == expected


# --- show ----------------------------------------------------------------------------------------


def test_show_compact_golden(cfg: ServerConfig, snap: ServerSnapshot) -> None:
    assert_golden("show_compact.txt", text.render_server(cfg, snap, full=False))


def test_show_full_golden(cfg: ServerConfig, snap: ServerSnapshot) -> None:
    assert_golden("show_full.txt", text.render_server(cfg, snap, full=True))


def test_show_without_snapshot(cfg: ServerConfig) -> None:
    for full in (False, True):
        assert text.render_server(cfg, None, full=full) == (
            "bookshop: (no description)\n(catalog not available: not cached)"
        )


def test_show_failed_refresh_without_catalog(cfg: ServerConfig) -> None:
    broken = ServerSnapshot(server="bookshop", spec_hash="x", last_error="spawn failed\ntraceback")
    out = text.render_server(cfg.model_copy(update={"description": "Books"}), broken, full=False)
    assert out == "bookshop: Books\n(catalog not available: spawn failed)"


def test_show_stale_catalog_notes_error(cfg: ServerConfig, snap: ServerSnapshot) -> None:
    stale = snap.model_copy(update={"last_error": "timed out"})
    out = text.render_server(cfg, stale, full=False)
    assert "(last refresh failed: timed out)" in out
    assert "search_books" in out


def test_show_full_skips_empty_sections(cfg: ServerConfig, snap: ServerSnapshot) -> None:
    bare = snap.model_copy(update={"resources": [], "resource_templates": [], "prompts": []})
    out = text.render_server(cfg, bare, full=True)
    assert "resources" not in out and "prompts" not in out
    assert "tools (5):" in out


def test_resources_and_prompts_listings(snap: ServerSnapshot) -> None:
    res = text.render_resources(snap)
    assert "catalog://genres" in res and "catalog://genres/{genre}" in res
    assert "resource templates (1):" in res and "prompts" not in res
    prompts = text.render_prompts(snap)
    assert "recommend  (Recommend a book)" in prompts
    assert "genre  required" in prompts
    empty = ServerSnapshot(server="x", spec_hash="x")
    assert text.render_resources(empty) == "No resources."
    assert text.render_prompts(empty) == "No prompts."


# --- tool ----------------------------------------------------------------------------------------


def test_render_tool_golden(snap: ServerSnapshot) -> None:
    assert_golden("tool_search_books.txt", text.render_tool("bookshop", tool(snap, "search_books")))
    assert_golden("tool_add_review.txt", text.render_tool("bookshop", tool(snap, "add_review")))


def test_render_tool_no_params(snap: ServerSnapshot) -> None:
    out = text.render_tool("bookshop", tool(snap, "cover_image"))
    assert "parameters: none" in out
    assert out.endswith("example: mcpc call bookshop/cover_image")


def test_render_tool_annotations_enum_and_nested() -> None:
    t = Tool(
        name="act",
        description="Do it.",
        input_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["a", "b"], "default": "a", "description": "Run mode"},
                "filter": {
                    "type": "object",
                    "properties": {"status": {"type": "string"}},
                    "required": ["status"],
                },
            },
            "required": ["filter"],
        },
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    out = text.render_tool("s", t)
    assert "  mode  string  default: a  enum: a, b - Run mode" in out
    assert "  filter  object  required" in out
    assert "  filter.status  string  required" in out
    assert "annotations: read-only, non-destructive, idempotent, open-world" in out
    assert out.endswith("example: mcpc call s/act -a filter=<object>")  # nested names are skipped


def test_render_tool_only_set_hints() -> None:
    t = Tool(
        name="rm",
        input_schema={"type": "object"},
        annotations=ToolAnnotations(destructive_hint=True, title="Remove"),
    )
    out = text.render_tool("s", t)
    assert out.splitlines()[0] == "s/rm  (Remove)"
    assert "annotations: destructive" in out
    assert "output:" not in out


# --- hits ----------------------------------------------------------------------------------------


class Hit:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def test_tool_hits() -> None:
    hits = [
        Hit(server="bookshop", tool="search_books", score=2.0, summary="Search the catalog."),
        Hit(server="weather", tool="forecast", score=1.0, summary=""),
    ]
    assert text.render_tool_hits(hits) == "bookshop/search_books  Search the catalog.\nweather/forecast"
    assert text.render_tool_hits([]) == "No matching tools."


def test_server_hits() -> None:
    hits = [
        Hit(
            server="bookshop",
            score=2.0,
            description="Books",
            top_tools=[Hit(tool="search_books"), Hit(tool="lookup_book")],
        ),
        Hit(server="w", score=1.0, description="Weather", top_tools=[]),
    ]
    assert text.render_server_hits(hits) == (
        "bookshop  Books\n  tools: search_books, lookup_book\nw         Weather"
    )
    assert text.render_server_hits([]) == "No matching servers."


# --- call results --------------------------------------------------------------------------------


def test_call_text_blocks_verbatim(out: Path) -> None:
    result = CallToolResult(content=[TextContent(text="line one\n  indented"), TextContent(text="two")])
    assert text.render_call_result(result, out_dir=out) == "line one\n  indented\ntwo"
    assert list(out.iterdir()) == []


def test_call_structured_only(out: Path) -> None:
    result = CallToolResult(content=[], structured_content={"title": "Dune", "n": "é"})
    assert text.render_call_result(result, out_dir=out) == '{\n  "title": "Dune",\n  "n": "é"\n}'


def test_call_text_wins_over_structured(out: Path) -> None:
    result = CallToolResult(content=[TextContent(text="hi")], structured_content={"a": 1})
    assert text.render_call_result(result, out_dir=out) == "hi"


def test_call_empty(out: Path) -> None:
    assert text.render_call_result(CallToolResult(content=[]), out_dir=out) == "(no output)"


def test_call_image_saved_to_out_dir(out: Path) -> None:
    out_dir = out / "nested"
    result = CallToolResult(content=[ImageContent(data=PNG_B64, mime_type="image/png")])
    rendered = text.render_call_result(result, out_dir=out_dir)
    [saved] = list(out_dir.iterdir())
    assert saved.suffix == ".png"
    assert saved.read_bytes() == base64.b64decode(PNG_B64)
    assert rendered == f"[image saved: {saved} (image/png, {PNG_BYTES} bytes)]"


def test_call_audio_saved(out: Path) -> None:
    data = base64.b64encode(b"RIFFxxxx").decode()
    result = CallToolResult(content=[AudioContent(data=data, mime_type="audio/wav")])
    rendered = text.render_call_result(result, out_dir=out)
    [saved] = list(out.iterdir())
    assert saved.suffix == ".wav" and saved.read_bytes() == b"RIFFxxxx"
    assert rendered == f"[audio saved: {saved} (audio/wav, 8 bytes)]"


def test_call_unknown_mime_falls_back_to_bin(out: Path) -> None:
    data = base64.b64encode(b"abc").decode()
    result = CallToolResult(content=[ImageContent(data=data, mime_type="image/x-weird")])
    text.render_call_result(result, out_dir=out)
    [saved] = list(out.iterdir())
    assert saved.suffix == ".bin"


def test_call_invalid_base64_does_not_crash(out: Path) -> None:
    result = CallToolResult(content=[ImageContent(data="@@@", mime_type="image/png")])
    rendered = text.render_call_result(result, out_dir=out)
    assert "not saved" in rendered or "saved:" in rendered


def test_call_resource_link_and_embedded(out: Path) -> None:
    result = CallToolResult(
        content=[
            ResourceLink(name="genres", uri="catalog://genres"),
            EmbeddedResource(resource=TextResourceContents(uri="catalog://genres", text="fiction\npoetry")),
            EmbeddedResource(
                resource=BlobResourceContents(uri="file:///a.png", mime_type="image/png", blob=PNG_B64)
            ),
        ]
    )
    lines = text.render_call_result(result, out_dir=out).splitlines()
    [saved] = list(out.iterdir())
    assert lines == [
        "[resource link: catalog://genres (genres)]",
        "[resource: catalog://genres]",
        "fiction",
        "poetry",
        "[resource: file:///a.png]",
        f"[blob saved: {saved} (image/png, {PNG_BYTES} bytes)]",
    ]


def test_call_is_error_prefix(out: Path) -> None:
    result = CallToolResult(content=[TextContent(text="No book\nsecond")], is_error=True)
    assert text.render_call_result(result, out_dir=out) == "error: No book\nsecond"


def test_call_max_output(out: Path) -> None:
    result = CallToolResult(content=[TextContent(text="abcdefghij")])
    assert text.render_call_result(result, out_dir=out, max_output=4) == (
        "abcd\n[output truncated: 4 of 10 characters shown]"
    )
    assert text.render_call_result(result, out_dir=out, max_output=10) == "abcdefghij"
    assert text.render_call_result(result, out_dir=out, max_output=None) == "abcdefghij"


# --- read / prompt / error -----------------------------------------------------------------------


def test_read_result(out: Path) -> None:
    single = ReadResourceResult(contents=[TextResourceContents(uri="u://a", text="hello")])
    assert text.render_read_result(single, out_dir=out) == "hello"
    blob = ReadResourceResult(
        contents=[BlobResourceContents(uri="u://b", mime_type="image/png", blob=PNG_B64)]
    )
    rendered = text.render_read_result(blob, out_dir=out)
    [saved] = list(out.iterdir())
    assert rendered == f"[blob saved: {saved} (image/png, {PNG_BYTES} bytes)]"
    many = ReadResourceResult(
        contents=[TextResourceContents(uri="u://a", text="A"), TextResourceContents(uri="u://b", text="B")]
    )
    assert text.render_read_result(many, out_dir=out) == "[resource: u://a]\nA\n[resource: u://b]\nB"
    assert text.render_read_result(many, out_dir=out, max_output=3).endswith(
        "[output truncated: 3 of 39 characters shown]"
    )


def test_prompt_result() -> None:
    result = GetPromptResult(
        description="d",
        messages=[
            PromptMessage(role="user", content=TextContent(text="Recommend a book.")),
            PromptMessage(role="assistant", content=ImageContent(data=PNG_B64, mime_type="image/png")),
            PromptMessage(role="user", content=ResourceLink(name="n", uri="u://x")),
        ],
    )
    assert text.render_prompt_result(result) == (
        f"[user]\nRecommend a book.\n\n[assistant]\n[image: image/png, {PNG_BYTES} bytes]\n\n"
        "[user]\n[resource link: u://x (n)]"
    )
    assert text.render_prompt_result(GetPromptResult(messages=[])) == "(no messages)"


def test_render_error() -> None:
    assert text.render_error(ConnectionFailedError("refused", server="gh")) == "mcpc: gh: refused"
    assert text.render_error(UsageError("unknown server 'x'")) == "mcpc: unknown server 'x'"


def test_http_config_not_needed_for_rendering(snap: ServerSnapshot) -> None:
    http_cfg = ServerConfig(
        name="web", spec=HttpServer(url="http://h/mcp"), scope="global", source=Path("g"), description="Web"
    )
    assert text.render_server(http_cfg, snap, full=False).startswith("web: Web\ntools (5):")
