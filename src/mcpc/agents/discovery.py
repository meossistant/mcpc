"""Unified discovery engine across coding agents (Claude Code, Antigravity CLI)."""

from __future__ import annotations

from pathlib import Path

from mcpc.agents.antigravity import (
    detect_antigravity,
    discover_antigravity_plugins,
    discover_antigravity_skills,
    load_antigravity_servers,
)
from mcpc.agents.claude import (
    detect_claude,
    discover_claude_plugins,
    discover_claude_skills,
    load_claude_servers,
)
from mcpc.agents.models import AgentInfo, AgentMode, AgentPlugin, AgentSkill, AgentType
from mcpc.config.models import HttpServer, ServerConfig, StdioServer


def resolve_agent_mode(mode: str | None) -> list[AgentType]:
    """Map mode alias strings to normalized list of AgentType."""
    if not mode or mode.strip().lower() in ("none", "false", "0"):
        return []
    m = mode.strip().lower()
    if m in ("all", "auto", "both", "true", "1"):
        return ["claude-code", "antigravity"]
    if m in ("claude", "claude-code", "claudecode"):
        return ["claude-code"]
    if m in ("antigravity", "agy", "gemini"):
        return ["antigravity"]
    return []


def _get_server_endpoint_key(sc: ServerConfig) -> str:
    """Create a canonical key identifying the server's remote target or process command."""
    if isinstance(sc.spec, HttpServer):
        return f"http:{sc.spec.url.strip().rstrip('/')}"
    if isinstance(sc.spec, StdioServer):
        cmd = sc.spec.command.strip()
        args = " ".join(sc.spec.args)
        return f"stdio:{cmd}:{args}".strip()
    return sc.spec_hash()


def deduplicate_servers(servers: list[ServerConfig]) -> list[ServerConfig]:
    """Deduplicate servers by endpoint target and name, merging tags."""
    by_name: dict[str, ServerConfig] = {}
    by_endpoint: dict[str, str] = {}

    for s in servers:
        endpoint_key = _get_server_endpoint_key(s)
        if endpoint_key in by_endpoint:
            primary_name = by_endpoint[endpoint_key]
            existing = by_name[primary_name]
            merged_tags = list(dict.fromkeys(existing.tags + s.tags))
            by_name[primary_name] = existing.model_copy(update={"tags": merged_tags})
            continue

        if s.name not in by_name:
            by_name[s.name] = s
            by_endpoint[endpoint_key] = s.name
        else:
            agent_tag = (
                s.tags[0].split(":")[-1] if (s.tags and s.tags[0].startswith("agent:")) else "agent"
            )
            prefixed = f"{agent_tag}-{s.name}"
            if prefixed not in by_name:
                disambiguated = s.model_copy(update={"name": prefixed})
                by_name[prefixed] = disambiguated
                by_endpoint[endpoint_key] = prefixed

    return list(by_name.values())


def deduplicate_skills(skills: list[AgentSkill]) -> list[AgentSkill]:
    """Deduplicate skills by normalized name while merging supporting agents and source paths."""
    by_name: dict[str, AgentSkill] = {}
    for s in skills:
        key = s.name.lower().strip()
        agents_list = list(s.agents) if s.agents else [s.agent]
        sources_list = list(s.sources) if s.sources else [s.path]

        if key not in by_name:
            by_name[key] = s.model_copy(
                update={"agents": agents_list, "sources": sources_list}
            )
        else:
            existing = by_name[key]
            merged_agents = list(
                dict.fromkeys(existing.agents + [a for a in agents_list if a not in existing.agents])
            )
            merged_sources = list(dict.fromkeys(existing.sources + sources_list))
            plugin_name = existing.plugin_name or s.plugin_name
            desc = (
                existing.description
                if len(existing.description) >= len(s.description)
                else s.description
            )
            by_name[key] = existing.model_copy(
                update={
                    "agents": merged_agents,
                    "sources": merged_sources,
                    "plugin_name": plugin_name,
                    "description": desc,
                }
            )
    return list(by_name.values())


def deduplicate_plugins(plugins: list[AgentPlugin]) -> list[AgentPlugin]:
    """Deduplicate plugins by normalized name while merging skills, servers, and agents."""
    by_name: dict[str, AgentPlugin] = {}
    for p in plugins:
        key = p.name.lower().strip()
        agents_list = list(p.agents) if p.agents else [p.agent]
        sources_list = list(p.sources) if p.sources else [p.source_dir]

        if key not in by_name:
            by_name[key] = p.model_copy(
                update={"agents": agents_list, "sources": sources_list}
            )
        else:
            existing = by_name[key]
            merged_agents = list(
                dict.fromkeys(existing.agents + [a for a in agents_list if a not in existing.agents])
            )
            merged_sources = list(dict.fromkeys(existing.sources + sources_list))
            merged_skills = list(dict.fromkeys(existing.skills + p.skills))
            merged_servers = list(dict.fromkeys(existing.mcp_servers + p.mcp_servers))
            version = existing.version or p.version
            desc = existing.description or p.description
            author = existing.author or p.author
            homepage = existing.homepage or p.homepage
            enabled = existing.enabled or p.enabled
            by_name[key] = existing.model_copy(
                update={
                    "agents": merged_agents,
                    "sources": merged_sources,
                    "skills": merged_skills,
                    "mcp_servers": merged_servers,
                    "version": version,
                    "description": desc,
                    "author": author,
                    "homepage": homepage,
                    "enabled": enabled,
                }
            )
    return list(by_name.values())


def load_all_agent_servers(
    mode: AgentMode | str | None,
    workspace: Path | None = None,
    warnings: list[str] | None = None,
    deduplicate: bool = True,
) -> list[ServerConfig]:
    """Load MCP servers for all agents active under the requested mode."""
    active_agents = resolve_agent_mode(str(mode) if mode else None)
    if not active_agents:
        return []

    servers: list[ServerConfig] = []
    seen_names: set[str] = set()

    if "claude-code" in active_agents:
        for sc in load_claude_servers(workspace, warnings):
            if sc.name not in seen_names:
                servers.append(sc)
                seen_names.add(sc.name)

    if "antigravity" in active_agents:
        for sc in load_antigravity_servers(workspace, warnings):
            if sc.name not in seen_names:
                servers.append(sc)
                seen_names.add(sc.name)
            else:
                prefixed_name = f"agy-{sc.name}"
                if prefixed_name not in seen_names:
                    sc = sc.model_copy(update={"name": prefixed_name})
                    servers.append(sc)
                    seen_names.add(prefixed_name)

    return deduplicate_servers(servers) if deduplicate else servers


def discover_all_plugins(
    mode: AgentMode | str | None = "all",
    workspace: Path | None = None,
    deduplicate: bool = True,
) -> list[AgentPlugin]:
    """Discover plugins across active coding agents."""
    active_agents = resolve_agent_mode(str(mode) if mode else "all")
    plugins: list[AgentPlugin] = []

    if "claude-code" in active_agents:
        plugins.extend(discover_claude_plugins(workspace))

    if "antigravity" in active_agents:
        plugins.extend(discover_antigravity_plugins(workspace))

    return deduplicate_plugins(plugins) if deduplicate else plugins


def discover_all_skills(
    mode: AgentMode | str | None = "all",
    workspace: Path | None = None,
    deduplicate: bool = True,
) -> list[AgentSkill]:
    """Discover skills across active coding agents."""
    active_agents = resolve_agent_mode(str(mode) if mode else "all")
    skills: list[AgentSkill] = []
    seen_paths: set[str] = set()

    if "claude-code" in active_agents:
        for s in discover_claude_skills(workspace):
            if s.path not in seen_paths:
                skills.append(s)
                seen_paths.add(s.path)

    if "antigravity" in active_agents:
        for s in discover_antigravity_skills(workspace):
            if s.path not in seen_paths:
                skills.append(s)
                seen_paths.add(s.path)

    return deduplicate_skills(skills) if deduplicate else skills


def get_agents_overview(workspace: Path | None = None) -> list[AgentInfo]:
    """Report status of supported coding agents."""
    return [
        detect_claude(workspace),
        detect_antigravity(workspace),
    ]
