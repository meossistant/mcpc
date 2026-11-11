"""Coding agent integration package for mcpc (Claude Code & Antigravity CLI)."""

from __future__ import annotations

from mcpc.agents.discovery import (
    discover_all_plugins,
    discover_all_skills,
    get_agents_overview,
    load_all_agent_servers,
    resolve_agent_mode,
)
from mcpc.agents.models import AgentInfo, AgentMode, AgentPlugin, AgentSkill, AgentType

__all__ = [
    "AgentInfo",
    "AgentMode",
    "AgentPlugin",
    "AgentSkill",
    "AgentType",
    "discover_all_plugins",
    "discover_all_skills",
    "get_agents_overview",
    "load_all_agent_servers",
    "resolve_agent_mode",
]
