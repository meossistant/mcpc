"""Data models for coding agents, plugins, and skills discovery."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

AgentType = Literal["claude-code", "antigravity"]
AgentMode = Literal["none", "all", "claude-code", "antigravity"]


class AgentPlugin(BaseModel):
    """A plugin or extension discovered from a coding agent."""

    model_config = ConfigDict(extra="ignore")
    name: str
    agent: AgentType
    version: str | None = None
    description: str | None = None
    author: str | None = None
    homepage: str | None = None
    enabled: bool = True
    source_dir: str
    skills: list[str] = []
    mcp_servers: list[str] = []
    agents: list[AgentType] = []
    sources: list[str] = []


class AgentSkill(BaseModel):
    """An agent skill (SKILL.md) discovered from a plugin, coding agent, or workspace."""

    model_config = ConfigDict(extra="ignore")
    name: str
    agent: AgentType
    plugin_name: str | None = None
    description: str = ""
    path: str
    content: str = ""
    agents: list[AgentType] = []
    sources: list[str] = []


class AgentInfo(BaseModel):
    """Summary of a coding agent's presence and assets on the local system."""

    model_config = ConfigDict(extra="ignore")
    agent: AgentType
    name: str
    detected: bool
    config_path: str | None = None
    plugins_dir: str | None = None
    skills_count: int = 0
    plugins_count: int = 0
    servers_count: int = 0
