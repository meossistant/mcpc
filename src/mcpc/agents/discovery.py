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
from mcpc.config.models import ServerConfig


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


def load_all_agent_servers(
    mode: AgentMode | str | None,
    workspace: Path | None = None,
    warnings: list[str] | None = None,
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
                # Disambiguate name collision by prefixing agent
                prefixed_name = f"agy-{sc.name}"
                if prefixed_name not in seen_names:
                    sc = sc.model_copy(update={"name": prefixed_name})
                    servers.append(sc)
                    seen_names.add(prefixed_name)

    return servers


def discover_all_plugins(
    mode: AgentMode | str | None = "all",
    workspace: Path | None = None,
) -> list[AgentPlugin]:
    """Discover plugins across active coding agents."""
    active_agents = resolve_agent_mode(str(mode) if mode else "all")
    plugins: list[AgentPlugin] = []

    if "claude-code" in active_agents:
        plugins.extend(discover_claude_plugins(workspace))

    if "antigravity" in active_agents:
        plugins.extend(discover_antigravity_plugins(workspace))

    return plugins


def discover_all_skills(
    mode: AgentMode | str | None = "all",
    workspace: Path | None = None,
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

    return skills


def get_agents_overview(workspace: Path | None = None) -> list[AgentInfo]:
    """Report status of supported coding agents."""
    return [
        detect_claude(workspace),
        detect_antigravity(workspace),
    ]
