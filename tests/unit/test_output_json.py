"""--json builders: every output validates against its model in output/schemas.py and carries no base64."""

from __future__ import annotations

import base64
import json
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
from mcpc.errors import ConnectionFailedError, ProtocolFailedError, UsageError
from mcpc.output import json as out_json
from mcpc.output import schemas as s

PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


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
        scope="workspace",
        source=Path("w.json"),
    )


@pytest.fixture
def out(tmp_path: Path) -> Path:
    """Empty output dir (tmp_path itself also holds the MCPC_HOME dir)."""
    path = tmp_path / "out"
    path.mkdir()
    return path


def roundtrip(data: dict[str, Any]) -> dict[str, Any]:
    """The final string must be valid JSON equal to the builder's dict."""
    parsed = json.loads(out_json.to_json(data))
    assert parsed == data
    return parsed


class Hit:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


# --- to_json -------------------------------------------------------------------------------------


def test_to_json_format() -> None:
    rendered = out_json.to_json({"b": 1, "a": "é"})
    assert rendered == '{\n  "b": 1,\n  "a": "é"\n}'  # indent=2, unsorted keys, no ASCII escaping


def test_to_json_model_excludes_none() -> None:
    model = s.ErrorOutput(error=s.ErrorBody(code=3, kind="connection", message="m"))
    assert json.loads(out_json.to_json(model)) == {"error": {"code": 3, "kind": "connection", "message": "m"}}


def test_to_json_keeps_none_inside_dict_values() -> None:
    model = s.ToolOutput(server="s", name="n", input_schema={"properties": {"a": {"default": None}}})
    assert json.loads(out_json.to_json(model))["input_schema"]["properties"]["a"] == {"default": None}


# --- list / show ---------------------------------------------------------------------------------


def test_server_list(snap: ServerSnapshot) -> None:
    rows = [
        s.ServerListRow(name="a", description="A", status="ok", tools=2, cache_age_hours=0.5),
        s.ServerListRow(name="b", status="untrusted", transport="sse", error="run mcpc trust"),
    ]
    data = roundtrip(out_json.server_list_json(rows))
    parsed = s.ServerListOutput.model_validate(data)
    assert [r.name for r in parsed.servers] == ["a", "b"]
    assert "protocol_version" not in data["servers"][0]  # None fields are omitted
    assert data["servers"][1]["transport"] == "sse"
    assert out_json.server_list_json([]) == {"servers": []}


def test_server_compact(cfg: ServerConfig, snap: ServerSnapshot) -> None:
    data = roundtrip(out_json.server_json(cfg, snap, False))
    parsed = s.ServerOutput.model_validate(data)
    assert parsed.status == "ok" and parsed.scope == "workspace" and parsed.transport == "stdio"
    assert parsed.description == "Search the catalog before recommending a book."
    assert data["tools"][0] == {"name": "search_books", "summary": "Search the catalog by title or author."}
    assert all(set(t) == {"name", "summary"} for t in data["tools"])
    assert "resources" not in data and "prompts" not in data
    assert data["server_info"] == {"name": "Bookshop", "version": "1.0.0"}


def test_server_full(cfg: ServerConfig, snap: ServerSnapshot) -> None:
    data = roundtrip(out_json.server_json(cfg, snap, True))
    parsed = s.ServerOutput.model_validate(data)
    assert parsed.instructions and parsed.protocol_version == "2026-07-28"
    assert [type(t) for t in parsed.tools or []] == [s.ToolOutput] * 5
    search = data["tools"][0]
    assert search["server"] == "bookshop"
    assert search["input_schema"]["required"] == ["query"]
    assert data["resources"][0]["uri"] == "catalog://genres"
    assert data["resource_templates"][0]["uri_template"] == "catalog://genres/{genre}"
    assert data["prompts"][0] == {
        "name": "recommend",
        "title": "Recommend a book",
        "description": "Ask for a recommendation in a genre.",
        "arguments": [{"name": "genre", "required": True}],
    }


def test_server_without_snapshot_or_disabled(cfg: ServerConfig) -> None:
    data = out_json.server_json(cfg, None, False)
    parsed = s.ServerOutput.model_validate(data)
    assert parsed.status == "not cached" and parsed.error == "not cached" and parsed.tools is None
    disabled = out_json.server_json(cfg.model_copy(update={"disabled": True}), None, False)
    assert disabled["status"] == "disabled"
    forced = out_json.server_json(cfg, None, False, status="untrusted")
    assert forced["status"] == "untrusted"


def test_server_failed_refresh_and_http_transport() -> None:
    http = ServerConfig(
        name="web",
        spec=HttpServer(url="http://h/sse", transport="sse"),
        scope="global",
        source=Path("g"),
        description="Web",
    )
    broken = ServerSnapshot(server="web", spec_hash="x", last_error="refused")
    data = out_json.server_json(http, broken, True)
    parsed = s.ServerOutput.model_validate(data)
    assert parsed.status == "error" and parsed.transport == "sse" and parsed.error == "refused"
    assert data["tools"] == []


def test_tool_json_has_raw_schema_and_annotations(snap: ServerSnapshot) -> None:
    data = roundtrip(out_json.tool_json("bookshop", snap.tool("search_books")))  # type: ignore[arg-type]
    parsed = s.ToolOutput.model_validate(data)
    assert parsed.title == "Search the catalog"
    assert parsed.input_schema["properties"]["limit"]["default"] == 10
    assert parsed.output_schema is not None
    assert "annotations" not in data
    tool = Tool(
        name="rm",
        input_schema={"type": "object"},
        annotations=ToolAnnotations(destructive_hint=True, read_only_hint=False),
    )
    ann = out_json.tool_json("s", tool)["annotations"]
    assert ann == {"readOnlyHint": False, "destructiveHint": True}


def test_resources_and_prompts_json(snap: ServerSnapshot) -> None:
    res = s.ResourcesOutput.model_validate(out_json.resources_json("bookshop", snap))
    assert [r.uri for r in res.resources] == ["catalog://genres"]
    assert [t.uri_template for t in res.resource_templates] == ["catalog://genres/{genre}"]
    prompts = s.PromptsOutput.model_validate(out_json.prompts_json("bookshop", snap))
    assert prompts.prompts[0].arguments[0].required is True


# --- search --------------------------------------------------------------------------------------


def test_tool_hits() -> None:
    hits = [Hit(server="bookshop", tool="search_books", score=2.123456, summary="Search.")]
    data = roundtrip(out_json.tool_hits_json("find book", hits))
    parsed = s.SearchOutput.model_validate(data)
    assert parsed.type == "tool" and parsed.query == "find book"
    assert data["results"] == [
        {"server": "bookshop", "tool": "search_books", "score": 2.1235, "summary": "Search."}
    ]
    assert out_json.tool_hits_json("q", [])["results"] == []


def test_server_hits() -> None:
    hits = [
        Hit(
            server="bookshop",
            score=1.5,
            description="Books",
            top_tools=[Hit(server="bookshop", tool="search_books", score=1.5, summary="S")],
        )
    ]
    data = roundtrip(out_json.server_hits_json("books", hits))
    parsed = s.SearchOutput.model_validate(data)
    assert parsed.type == "server"
    assert isinstance(parsed.results[0], s.ServerHitOutput)
    assert data["results"][0]["top_tools"][0]["tool"] == "search_books"


# --- call / read / prompt ------------------------------------------------------------------------


def assert_no_base64(data: Any) -> None:
    assert PNG_B64 not in json.dumps(data)


def test_call_text_and_structured(out: Path) -> None:
    result = CallToolResult(content=[TextContent(text="hi")], structured_content={"a": [1, None]})
    data = roundtrip(out_json.call_json("bookshop", "t", result, out))
    parsed = s.CallOutput.model_validate(data)
    assert not parsed.is_error
    assert data == {
        "server": "bookshop",
        "tool": "t",
        "is_error": False,
        "content": [{"type": "text", "text": "hi"}],
        "structured_content": {"a": [1, None]},
    }


def test_call_without_structured_content_omits_key(out: Path) -> None:
    result = CallToolResult(content=[TextContent(text="x")], is_error=True)
    data = out_json.call_json("s", "t", result, out)
    assert data["is_error"] is True and "structured_content" not in data


def test_call_image_audio_replaced_by_paths(out: Path) -> None:
    audio = base64.b64encode(b"RIFFxxxx").decode()
    result = CallToolResult(
        content=[
            ImageContent(data=PNG_B64, mime_type="image/png"),
            AudioContent(data=audio, mime_type="audio/wav"),
        ]
    )
    data = roundtrip(out_json.call_json("s", "t", result, out))
    s.CallOutput.model_validate(data)
    img, aud = data["content"]
    assert set(img) == {"type", "mimeType", "path", "size"}
    assert img["type"] == "image" and img["mimeType"] == "image/png" and img["size"] == 70
    assert Path(img["path"]).read_bytes() == base64.b64decode(PNG_B64)
    assert aud["type"] == "audio" and Path(aud["path"]).suffix == ".wav"
    assert_no_base64(data)
    assert audio not in json.dumps(data)


def test_call_resources(out: Path) -> None:
    result = CallToolResult(
        content=[
            ResourceLink(name="genres", uri="catalog://genres", mime_type="text/plain"),
            EmbeddedResource(
                resource=TextResourceContents(uri="u://t", mime_type="text/plain", text="hello")
            ),
            EmbeddedResource(resource=BlobResourceContents(uri="u://b", mime_type="image/png", blob=PNG_B64)),
        ]
    )
    data = roundtrip(out_json.call_json("s", "t", result, out))
    link, text_res, blob_res = data["content"]
    assert link == {
        "type": "resource_link",
        "name": "genres",
        "uri": "catalog://genres",
        "mimeType": "text/plain",
    }
    assert text_res["resource"] == {"uri": "u://t", "mimeType": "text/plain", "text": "hello"}
    assert blob_res["type"] == "resource"
    assert "blob" not in blob_res["resource"]
    assert Path(blob_res["resource"]["path"]).suffix == ".png"
    assert_no_base64(data)


def test_call_invalid_base64_reports_error(out: Path) -> None:
    result = CallToolResult(content=[ImageContent(data="@", mime_type="image/png")])
    block = out_json.call_json("s", "t", result, out)["content"][0]
    assert block["type"] == "image" and "data" not in block


def test_read_json(out: Path) -> None:
    result = ReadResourceResult(
        contents=[
            TextResourceContents(uri="u://a", mime_type="text/plain", text="A"),
            BlobResourceContents(uri="u://b", mime_type="image/png", blob=PNG_B64),
        ]
    )
    data = roundtrip(out_json.read_json("s", "u://a", result, out))
    parsed = s.ReadOutput.model_validate(data)
    assert parsed.uri == "u://a"
    assert data["contents"][0] == {"uri": "u://a", "mimeType": "text/plain", "text": "A"}
    blob = data["contents"][1]
    assert blob["uri"] == "u://b" and Path(blob["path"]).exists() and "blob" not in blob
    assert_no_base64(data)


def test_prompt_json(out: Path) -> None:
    result = GetPromptResult(
        description="d",
        messages=[
            PromptMessage(role="user", content=TextContent(text="Recommend.")),
            PromptMessage(role="assistant", content=ImageContent(data=PNG_B64, mime_type="image/png")),
        ],
    )
    data = roundtrip(out_json.prompt_json("s", "recommend", result))
    parsed = s.PromptResultOutput.model_validate(data)
    assert parsed.description == "d" and [m.role for m in parsed.messages] == ["user", "assistant"]
    assert data["messages"][1]["content"] == {"type": "image", "mimeType": "image/png"}
    assert_no_base64(data)
    saved = out_json.prompt_json("s", "recommend", result, out)
    assert Path(saved["messages"][1]["content"]["path"]).exists()


# --- errors --------------------------------------------------------------------------------------


def test_error_json() -> None:
    err = ConnectionFailedError("refused", server="gh")
    data = roundtrip(out_json.error_json(err))
    assert data == {"error": {"code": 3, "kind": "connection", "message": "refused", "server": "gh"}}
    assert s.ErrorOutput.model_validate(data).error.server == "gh"
    bare = out_json.error_json(UsageError("bad"))
    assert bare == {"error": {"code": 2, "kind": "usage", "message": "bad"}}
    detailed = out_json.error_json(ProtocolFailedError("x", detail={"code": -32601}))
    assert detailed["error"]["detail"] == {"code": -32601}


def test_models_forbid_unknown_fields() -> None:
    with pytest.raises(ValueError):
        s.CallOutput.model_validate(
            {"server": "s", "tool": "t", "is_error": False, "content": [], "extra": 1}
        )
