"""In-memory BM25 index over the cached catalog (plan §7)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from mcp_types import Tool

from mcpc.agents.models import AgentPlugin, AgentSkill
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
class SkillHit:
    name: str
    score: float
    summary: str
    agents: list[str] = field(default_factory=list)
    plugin: str | None = None
    path: str = ""


@dataclass(frozen=True)
class PluginHit:
    name: str
    score: float
    summary: str
    agents: list[str] = field(default_factory=list)
    version: str | None = None
    skills_count: int = 0
    servers_count: int = 0
    source_dir: str = ""


@dataclass(frozen=True)
class UnifiedHit:
    kind: str  # "tool", "server", "skill", "plugin"
    target: str
    score: float
    summary: str
    server: str | None = None
    tool: str | None = None
    agents: list[str] = field(default_factory=list)
    plugin: str | None = None
    version: str | None = None
    path: str | None = None
    top_tools: list[ToolHit] = field(default_factory=list)


@dataclass(frozen=True)
class _ToolEntry:
    server: str
    tool: Tool


@dataclass(frozen=True)
class _IndexEntry:
    kind: str  # "tool", "server", "skill", "plugin"
    target: str
    name: str
    summary: str
    server: str | None = None
    tool: str | None = None
    agents: list[str] = field(default_factory=list)
    plugin: str | None = None
    version: str | None = None
    path: str | None = None
    top_tools: list[ToolHit] = field(default_factory=list)


class CatalogIndex:
    """BM25 search over tools, servers, skills, and plugins.

    Tool scores come from one corpus that spans all servers, so IDF is the same whether or not a
    ``server`` filter is applied.
    """

    def __init__(
        self,
        entries: Sequence[tuple[ServerConfig, ServerSnapshot | None]],
        skills: Sequence[AgentSkill] = (),
        plugins: Sequence[AgentPlugin] = (),
    ) -> None:
        self._servers: list[tuple[str, str]] = []  # (name, description)
        self._server_docs: list[list[str]] = []
        self._tools: list[_ToolEntry] = []
        self._tool_docs: list[list[str]] = []

        self._unified_entries: list[_IndexEntry] = []
        self._unified_docs: list[list[str]] = []

        for cfg, snap in entries:
            srv_desc = server_description(cfg.description, snap)
            self._servers.append((cfg.name, srv_desc))
            srv_doc = _server_doc(cfg, snap)
            self._server_docs.append(srv_doc)

            self._unified_entries.append(
                _IndexEntry(
                    kind="server",
                    target=cfg.name,
                    name=cfg.name,
                    summary=srv_desc,
                    server=cfg.name,
                )
            )
            self._unified_docs.append(srv_doc)

            for tool in snap.tools if snap is not None else []:
                self._tools.append(_ToolEntry(cfg.name, tool))
                t_doc = _tool_doc(cfg, tool)
                self._tool_docs.append(t_doc)

                self._unified_entries.append(
                    _IndexEntry(
                        kind="tool",
                        target=f"{cfg.name}/{tool.name}",
                        name=tool.name,
                        summary=tool_summary(tool),
                        server=cfg.name,
                        tool=tool.name,
                    )
                )
                self._unified_docs.append(t_doc)

        self._tool_bm25 = BM25(self._tool_docs)
        self._server_bm25 = BM25(self._server_docs)

        self._skills = list(skills)
        self._skill_docs = [_skill_doc(s) for s in self._skills]
        self._skill_bm25 = BM25(self._skill_docs)

        for s, s_doc in zip(self._skills, self._skill_docs, strict=True):
            self._unified_entries.append(
                _IndexEntry(
                    kind="skill",
                    target=s.name,
                    name=s.name,
                    summary=s.description or "",
                    agents=list(s.agents) if s.agents else ([s.agent] if s.agent else []),
                    plugin=s.plugin_name,
                    path=s.path,
                )
            )
            self._unified_docs.append(s_doc)

        self._plugins = list(plugins)
        self._plugin_docs = [_plugin_doc(p) for p in self._plugins]
        self._plugin_bm25 = BM25(self._plugin_docs)

        for p, p_doc in zip(self._plugins, self._plugin_docs, strict=True):
            self._unified_entries.append(
                _IndexEntry(
                    kind="plugin",
                    target=p.name,
                    name=p.name,
                    summary=p.description or "",
                    agents=list(p.agents) if p.agents else ([p.agent] if p.agent else []),
                    version=p.version,
                    path=p.source_dir,
                )
            )
            self._unified_docs.append(p_doc)

        self._unified_bm25 = BM25(self._unified_docs)

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

    def search_skills(self, query: str, limit: int = 10) -> list[SkillHit]:
        """Rank skills across coding agents."""
        hits = self._score_skills(query)
        hits.sort(key=lambda h: (-h.score, h.name))
        return hits[: max(limit, 0)]

    def search_plugins(self, query: str, limit: int = 10) -> list[PluginHit]:
        """Rank plugins across coding agents."""
        hits = self._score_plugins(query)
        hits.sort(key=lambda h: (-h.score, h.name))
        return hits[: max(limit, 0)]

    def search_mcp(self, query: str, limit: int = 10, server: str | None = None) -> list[UnifiedHit]:
        """Rank MCP tools and servers together."""
        return [
            h
            for h in self.search_all(query, limit=len(self._unified_entries), server=server)
            if h.kind in ("tool", "server")
        ][: max(limit, 0)]

    def search_all(self, query: str, limit: int = 10, server: str | None = None) -> list[UnifiedHit]:
        """Rank tools, servers, skills, and plugins in a unified hit list."""
        tokens = tokenize(query)
        scores = self._unified_bm25.score(tokens)
        results: list[UnifiedHit] = []

        for entry, score in zip(self._unified_entries, scores, strict=True):
            if score <= 0:
                continue
            if server is not None and (entry.kind != "tool" or entry.server != server):
                continue

            results.append(
                UnifiedHit(
                    kind=entry.kind,
                    target=entry.target,
                    score=score,
                    summary=entry.summary,
                    server=entry.server,
                    tool=entry.tool,
                    agents=entry.agents,
                    plugin=entry.plugin,
                    version=entry.version,
                    path=entry.path,
                    top_tools=entry.top_tools,
                )
            )

        # Tie-breaker priority: tool (0) > skill (1) > plugin (2) > server (3)
        kind_prio = {"tool": 0, "skill": 1, "plugin": 2, "server": 3}
        results.sort(
            key=lambda h: (-h.score, kind_prio.get(h.kind, 9), h.target)
        )
        return results[: max(limit, 0)]

    def _score_tools(self, query: str) -> list[ToolHit]:
        scores = self._tool_bm25.score(tokenize(query))
        return [
            ToolHit(e.server, e.tool.name, score, tool_summary(e.tool))
            for e, score in zip(self._tools, scores, strict=True)
            if score > 0
        ]

    def _score_skills(self, query: str) -> list[SkillHit]:
        scores = self._skill_bm25.score(tokenize(query))
        return [
            SkillHit(
                name=s.name,
                score=score,
                summary=s.description or "",
                agents=list(s.agents) if s.agents else ([s.agent] if s.agent else []),
                plugin=s.plugin_name,
                path=s.path,
            )
            for s, score in zip(self._skills, scores, strict=True)
            if score > 0
        ]

    def _score_plugins(self, query: str) -> list[PluginHit]:
        scores = self._plugin_bm25.score(tokenize(query))
        return [
            PluginHit(
                name=p.name,
                score=score,
                summary=p.description or "",
                agents=list(p.agents) if p.agents else ([p.agent] if p.agent else []),
                version=p.version,
                skills_count=len(p.skills),
                servers_count=len(p.mcp_servers),
                source_dir=p.source_dir,
            )
            for p, score in zip(self._plugins, scores, strict=True)
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


def _skill_doc(skill: AgentSkill) -> list[str]:
    """Tokens for one skill: name x3, description x2, plugin, agents, content terms."""
    doc = tokenize(skill.name) * NAME_WEIGHT
    doc += tokenize(skill.description or "") * TITLE_WEIGHT
    if skill.plugin_name:
        doc += tokenize(skill.plugin_name)
    for a in skill.agents:
        doc += tokenize(a)
    if skill.content:
        doc += tokenize(skill.content[:500])
    return doc


def _plugin_doc(plugin: AgentPlugin) -> list[str]:
    """Tokens for one plugin: name x3, description x2, skills, servers, agents."""
    doc = tokenize(plugin.name) * NAME_WEIGHT
    doc += tokenize(plugin.description or "") * TITLE_WEIGHT
    for a in plugin.agents:
        doc += tokenize(a)
    for s in plugin.skills:
        doc += tokenize(s)
    for srv in plugin.mcp_servers:
        doc += tokenize(srv)
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
