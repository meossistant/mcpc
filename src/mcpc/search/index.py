"""In-memory BM25 index over the cached catalog (plan §7)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from mcp_types import Tool

from mcpc.catalog.models import ServerSnapshot, server_description, tool_summary
from mcpc.config.models import ServerConfig
from mcpc.search.bm25 import BM25
from mcpc.search.tokenize import tokenize

NAME_WEIGHT = 3
TITLE_WEIGHT = 2
TOP_TOOLS_PER_SERVER = 3
_SCHEMA_DEPTH = 1  # how many levels of nested objects to read parameter names from


@dataclass(frozen=True)
class ToolHit:
    server: str
    tool: str
    score: float
    summary: str


@dataclass(frozen=True)
class ServerHit:
    server: str
    score: float
    description: str
    top_tools: list[ToolHit]


@dataclass(frozen=True)
class _ToolEntry:
    server: str
    tool: Tool


class CatalogIndex:
    """BM25 search over tools and servers.

    Tool scores come from one corpus that spans all servers, so IDF is the same whether or not a
    ``server`` filter is applied.
    """

    def __init__(self, entries: Sequence[tuple[ServerConfig, ServerSnapshot | None]]) -> None:
        self._servers: list[tuple[str, str]] = []  # (name, description)
        self._server_docs: list[list[str]] = []
        self._tools: list[_ToolEntry] = []
        self._tool_docs: list[list[str]] = []
        for cfg, snap in entries:
            self._servers.append((cfg.name, server_description(cfg.description, snap)))
            self._server_docs.append(_server_doc(cfg, snap))
            for tool in snap.tools if snap is not None else []:
                self._tools.append(_ToolEntry(cfg.name, tool))
                self._tool_docs.append(_tool_doc(cfg, tool))
        self._tool_bm25 = BM25(self._tool_docs)
        self._server_bm25 = BM25(self._server_docs)

    def search_tools(self, query: str, limit: int = 10, server: str | None = None) -> list[ToolHit]:
        """Rank tools for a query. Zero scores are dropped; ties sort by (server, tool)."""
        hits = [h for h in self._score_tools(query) if server is None or h.server == server]
        return _sorted_tool_hits(hits)[: max(limit, 0)]

    def search_servers(self, query: str, limit: int = 10) -> list[ServerHit]:
        """Rank servers by max(server document score, best tool score), with their top 3 tools."""
        tokens = tokenize(query)
        server_scores = self._server_bm25.score(tokens)
        by_server: dict[str, list[ToolHit]] = {}
        for hit in self._score_tools(query):
            by_server.setdefault(hit.server, []).append(hit)
        results: list[ServerHit] = []
        for (name, description), doc_score in zip(self._servers, server_scores, strict=True):
            tool_hits = _sorted_tool_hits(by_server.get(name, []))
            best_tool = tool_hits[0].score if tool_hits else 0.0
            score = max(doc_score, best_tool)
            if score > 0:
                results.append(ServerHit(name, score, description, tool_hits[:TOP_TOOLS_PER_SERVER]))
        results.sort(key=lambda h: (-h.score, h.server))
        return results[: max(limit, 0)]

    def _score_tools(self, query: str) -> list[ToolHit]:
        scores = self._tool_bm25.score(tokenize(query))
        return [
            ToolHit(e.server, e.tool.name, score, tool_summary(e.tool))
            for e, score in zip(self._tools, scores, strict=True)
            if score > 0
        ]


def _sorted_tool_hits(hits: list[ToolHit]) -> list[ToolHit]:
    return sorted(hits, key=lambda h: (-h.score, h.server, h.tool))


def _tool_doc(cfg: ServerConfig, tool: Tool) -> list[str]:
    """Tokens for one tool. Field weights come from repetition: name x3, title x2."""
    doc = tokenize(cfg.name)
    doc += tokenize(tool.name) * NAME_WEIGHT
    doc += tokenize(tool.title or "") * TITLE_WEIGHT
    doc += tokenize(tool.description or "")
    doc += tokenize(" ".join(_schema_terms(tool.input_schema, _SCHEMA_DEPTH)))
    doc += tokenize(" ".join(cfg.tags))
    return doc


def _server_doc(cfg: ServerConfig, snap: ServerSnapshot | None) -> list[str]:
    doc = tokenize(cfg.name)
    doc += tokenize(cfg.description or "")
    doc += tokenize(" ".join(cfg.tags))
    if snap is not None:
        doc += tokenize(snap.instructions or "")
        doc += tokenize(" ".join(t.name for t in snap.tools))
    return doc


def _schema_terms(schema: Any, depth: int) -> list[str]:
    """Parameter names and descriptions from `properties`, descending `depth` levels into nested objects."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return []
    terms: list[str] = []
    for name, sub in props.items():
        terms.append(str(name))
        if not isinstance(sub, dict):
            continue
        description = sub.get("description")
        if isinstance(description, str):
            terms.append(description)
        if depth > 0:
            terms.extend(_schema_terms(sub, depth - 1))
            items = sub.get("items")
            if isinstance(items, dict):
                terms.extend(_schema_terms(items, depth - 1))
    return terms
