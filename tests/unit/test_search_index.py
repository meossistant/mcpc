"""CatalogIndex ranking over the bookshop and weather fixture catalogs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import Tool

from mcpc.catalog.models import ServerSnapshot
from mcpc.config.models import ServerConfig, StdioServer
from mcpc.search.index import CatalogIndex, ServerHit, ToolHit


def _cfg(name: str, description: str | None = None, tags: list[str] | None = None) -> ServerConfig:
    return ServerConfig(
        name=name,
        spec=StdioServer(command="x"),
        description=description,
        tags=tags or [],
        scope="global",
        source=Path("g"),
    )


def _snap(name: str, tools: list[Tool], instructions: str | None = None) -> ServerSnapshot:
    return ServerSnapshot(server=name, spec_hash="h", tools=tools, instructions=instructions)


def _tool(name: str, description: str, properties: dict[str, Any] | None = None, **kw: Any) -> Tool:
    schema = {"type": "object", "properties": properties or {}}
    return Tool(name=name, description=description, input_schema=schema, **kw)


async def _fixture_tools(server: Any) -> tuple[list[Tool], str | None]:
    async with Client(server) as client:
        return (await client.list_tools()).tools, client.instructions


@pytest.fixture
async def fixture_entries() -> list[tuple[ServerConfig, ServerSnapshot | None]]:
    from bookshop_server import mcp as bookshop
    from weather_server import mcp as weather

    book_tools, book_instr = await _fixture_tools(bookshop)
    weather_tools, weather_instr = await _fixture_tools(weather)
    return [
        (_cfg("weather", "Forecasts and weather stations"), _snap("weather", weather_tools, weather_instr)),
        (_cfg("bookshop", None, ["books"]), _snap("bookshop", book_tools, book_instr)),
    ]


@pytest.mark.anyio
async def test_find_a_book_by_author_ranks_search_books_first(fixture_entries: Any) -> None:
    index = CatalogIndex(fixture_entries)

    hits = index.search_tools("find a book by author")

    assert (hits[0].server, hits[0].tool) == ("bookshop", "search_books")
    assert hits[0].summary == "Search the catalog by title or author."
    assert all(h.score > 0 for h in hits)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


@pytest.mark.anyio
async def test_weather_query_ranks_forecast_first(fixture_entries: Any) -> None:
    hits = CatalogIndex(fixture_entries).search_tools("weather forecast for a city")
    assert (hits[0].server, hits[0].tool) == ("weather", "forecast")


@pytest.mark.anyio
async def test_parameter_names_are_searchable(fixture_entries: Any) -> None:
    hits = CatalogIndex(fixture_entries).search_tools("rating")
    assert [(h.server, h.tool) for h in hits] == [("bookshop", "add_review")]


@pytest.mark.anyio
async def test_camel_case_query_matches_snake_case_tool(fixture_entries: Any) -> None:
    hits = CatalogIndex(fixture_entries).search_tools("searchBooks")
    assert (hits[0].server, hits[0].tool) == ("bookshop", "search_books")


@pytest.mark.anyio
async def test_server_filter_and_limit(fixture_entries: Any) -> None:
    index = CatalogIndex(fixture_entries)

    only_weather = index.search_tools("search", server="weather")
    assert {h.server for h in only_weather} == {"weather"}
    assert [h.tool for h in only_weather][0] == "search"

    assert len(index.search_tools("search", limit=1)) == 1
    assert index.search_tools("search", limit=0) == []
    assert index.search_tools("search", server="nope") == []


@pytest.mark.anyio
async def test_no_match_and_empty_query(fixture_entries: Any) -> None:
    index = CatalogIndex(fixture_entries)
    assert index.search_tools("zzzzqqqq") == []
    assert index.search_tools("") == []
    assert index.search_tools("the a of") == []
    assert index.search_servers("zzzzqqqq") == []


@pytest.mark.anyio
async def test_server_ranking_and_top_tools(fixture_entries: Any) -> None:
    hits = CatalogIndex(fixture_entries).search_servers("book author")

    assert hits[0].server == "bookshop"
    assert all(isinstance(h, ServerHit) for h in hits)
    top = hits[0].top_tools
    assert 1 <= len(top) <= 3
    assert top[0].tool == "search_books"
    assert all(isinstance(t, ToolHit) and t.score > 0 for t in top)
    assert [t.score for t in top] == sorted((t.score for t in top), reverse=True)
    assert hits[0].score >= top[0].score


@pytest.mark.anyio
async def test_server_hit_description_uses_fallbacks(fixture_entries: Any) -> None:
    hits = {h.server: h for h in CatalogIndex(fixture_entries).search_servers("search")}
    assert hits["weather"].description == "Forecasts and weather stations"
    # bookshop has no config description: first sentence of its instructions.
    assert hits["bookshop"].description == "Search the catalog before recommending a book."


def test_server_without_snapshot_is_still_a_server_hit() -> None:
    index = CatalogIndex(
        [
            (_cfg("ghost", "Haunted ledger of spirits", ["paranormal"]), None),
            (_cfg("plain", "Something else"), _snap("plain", [_tool("noop", "does nothing")])),
        ]
    )

    by_description = index.search_servers("spirits")
    assert [(h.server, h.top_tools) for h in by_description] == [("ghost", [])]
    assert by_description[0].description == "Haunted ledger of spirits"
    assert [h.server for h in index.search_servers("paranormal")] == ["ghost"]
    assert index.search_tools("spirits") == []


def test_server_without_snapshot_or_description_uses_placeholder() -> None:
    hits = CatalogIndex([(_cfg("lonely"), None)]).search_servers("lonely")
    assert hits[0].description == "(no description)"


def test_empty_index() -> None:
    index = CatalogIndex([])
    assert index.search_tools("anything") == []
    assert index.search_servers("anything") == []


def test_exact_ties_are_broken_alphabetically() -> None:
    def entry(server: str) -> tuple[ServerConfig, ServerSnapshot]:
        return _cfg(server), _snap(server, [_tool("same", "identical description")])

    # Identical documents except the server name, which the query does not mention.
    index = CatalogIndex([entry("beta"), entry("alpha"), entry("gamma")])
    hits = index.search_tools("identical description")
    assert [h.server for h in hits] == ["alpha", "beta", "gamma"]
    assert len({h.score for h in hits}) == 1

    shuffled = CatalogIndex([entry("gamma"), entry("alpha"), entry("beta")])
    assert shuffled.search_tools("identical description") == hits


def test_results_are_deterministic_across_builds() -> None:
    entries = [(_cfg("s"), _snap("s", [_tool("one", "red apple"), _tool("two", "red pear")]))]
    first = CatalogIndex(entries).search_tools("red")
    assert CatalogIndex(entries).search_tools("red") == first


def test_title_and_tags_contribute() -> None:
    index = CatalogIndex(
        [
            (_cfg("a", tags=["finance"]), _snap("a", [_tool("t1", "x")])),
            (_cfg("b"), _snap("b", [_tool("t2", "x", title="Quarterly ledger")])),
        ]
    )
    assert [h.server for h in index.search_tools("finance")] == ["a"]
    assert [h.server for h in index.search_tools("ledger")] == ["b"]


def test_nested_parameter_names_and_descriptions_are_indexed() -> None:
    nested = {
        "filter": {
            "type": "object",
            "description": "Narrow results",
            "properties": {"assignee": {"type": "string", "description": "Person responsible"}},
        }
    }
    index = CatalogIndex([(_cfg("s"), _snap("s", [_tool("t", "do it", nested), _tool("u", "other")]))])
    assert [h.tool for h in index.search_tools("assignee")] == ["t"]
    assert [h.tool for h in index.search_tools("responsible")] == ["t"]
    assert [h.tool for h in index.search_tools("narrow")] == ["t"]


def test_malformed_input_schema_does_not_break_indexing() -> None:
    tool = Tool(name="odd", description="odd tool", input_schema={"type": "object", "properties": []})
    index = CatalogIndex([(_cfg("s"), _snap("s", [tool]))])
    assert [h.tool for h in index.search_tools("odd")] == ["odd"]


def test_search_servers_uses_best_tool_score_when_higher() -> None:
    index = CatalogIndex(
        [
            (_cfg("a", "generic"), _snap("a", [_tool("compile_report", "build a quarterly report")])),
            (_cfg("b", "generic"), _snap("b", [_tool("other", "unrelated")])),
        ]
    )
    tool_hits = index.search_tools("compile quarterly report")
    server_hits = index.search_servers("compile quarterly report")
    assert [h.server for h in server_hits] == ["a"]
    assert server_hits[0].score >= tool_hits[0].score


def test_top_tools_capped_at_three() -> None:
    tools = [_tool(f"fetch_{i}", "fetch things") for i in range(6)]
    hits = CatalogIndex([(_cfg("s"), _snap("s", tools))]).search_servers("fetch")
    assert len(hits[0].top_tools) == 3
    assert [t.tool for t in hits[0].top_tools] == ["fetch_0", "fetch_1", "fetch_2"]
