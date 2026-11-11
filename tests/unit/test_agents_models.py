"""Tests for coding agent data models."""

from __future__ import annotations

from mcpc.agents.models import AgentInfo, AgentPlugin, AgentSkill


def test_agent_plugin_model() -> None:
    plugin = AgentPlugin(
        name="test-plugin",
        agent="antigravity",
        version="1.2.3",
        description="A test plugin",
        source_dir="/tmp/test",
        skills=["s1", "s2"],
        mcp_servers=["m1"],
    )
    assert plugin.name == "test-plugin"
    assert plugin.agent == "antigravity"
    assert plugin.version == "1.2.3"
    assert plugin.enabled is True
    assert len(plugin.skills) == 2
    assert len(plugin.mcp_servers) == 1


def test_agent_skill_model() -> None:
    skill = AgentSkill(
        name="test-skill",
        agent="claude-code",
        plugin_name="some-plugin",
        description="Skill description",
        path="/path/to/SKILL.md",
        content="Instructions here",
    )
    assert skill.name == "test-skill"
    assert skill.agent == "claude-code"
    assert skill.content == "Instructions here"


def test_agent_info_model() -> None:
    info = AgentInfo(
        agent="claude-code",
        name="Claude Code",
        detected=True,
        config_path="/path/to/.claude.json",
        skills_count=10,
        plugins_count=2,
        servers_count=3,
    )
    assert info.detected is True
    assert info.servers_count == 3
