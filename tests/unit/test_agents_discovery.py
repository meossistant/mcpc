"""Unit tests for agent discovery functions."""

from __future__ import annotations

import json
from pathlib import Path

from mcpc.agents.claude import parse_skill_markdown
from mcpc.agents.discovery import (
    deduplicate_plugins,
    deduplicate_servers,
    deduplicate_skills,
    discover_all_skills,
    get_agents_overview,
    load_all_agent_servers,
    resolve_agent_mode,
)
from mcpc.agents.models import AgentPlugin, AgentSkill
from mcpc.config.models import HttpServer, ServerConfig, StdioServer


def test_resolve_agent_mode() -> None:
    assert resolve_agent_mode(None) == []
    assert resolve_agent_mode("none") == []
    assert resolve_agent_mode("all") == ["claude-code", "antigravity"]
    assert resolve_agent_mode("auto") == ["claude-code", "antigravity"]
    assert resolve_agent_mode("claude") == ["claude-code"]
    assert resolve_agent_mode("claude-code") == ["claude-code"]
    assert resolve_agent_mode("antigravity") == ["antigravity"]
    assert resolve_agent_mode("agy") == ["antigravity"]


def test_parse_skill_markdown(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: custom-name\ndescription: Custom skill description\n---\n# Guide\nUse this tool.\n",
        encoding="utf-8",
    )
    skill = parse_skill_markdown(skill_file)
    assert skill is not None
    assert skill.name == "custom-name"
    assert skill.description == "Custom skill description"
    assert "# Guide" in skill.content


def test_claude_workspace_discovery(tmp_path: Path) -> None:
    # Create workspace with .mcp.json and .agents/skills
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(
        json.dumps({
            "mcpServers": {
                "ws-server": {
                    "command": "python",
                    "args": ["server.py"],
                    "description": "Workspace MCP Server",
                }
            }
        }),
        encoding="utf-8",
    )
    skill_dir = tmp_path / ".agents" / "skills" / "ws-skill"
    skill_dir.mkdir(parents=True)
    skill_content = "---\nname: ws-skill\ndescription: Workspace skill\n---\nHello"
    (skill_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")

    servers = load_all_agent_servers("claude-code", workspace=tmp_path)
    assert any(s.name == "ws-server" for s in servers)

    skills = discover_all_skills("claude-code", workspace=tmp_path)
    assert any(s.name == "ws-skill" for s in skills)


def test_antigravity_workspace_discovery(tmp_path: Path) -> None:
    gemini_dir = tmp_path / ".gemini"
    gemini_dir.mkdir(parents=True)
    mcp_json = gemini_dir / "mcp_config.json"
    mcp_json.write_text(
        json.dumps({
            "mcpServers": {
                "agy-server": {
                    "url": "http://localhost:8000/sse",
                    "transport": "sse",
                }
            }
        }),
        encoding="utf-8",
    )
    servers = load_all_agent_servers("antigravity", workspace=tmp_path)
    assert any(s.name == "agy-server" for s in servers)


def test_get_agents_overview(tmp_path: Path) -> None:
    overview = get_agents_overview(tmp_path)
    assert len(overview) == 2
    names = {o.agent for o in overview}
    assert "claude-code" in names
    assert "antigravity" in names


def test_deduplicate_skills() -> None:
    s1 = AgentSkill(
        name="test-skill",
        agent="claude-code",
        agents=["claude-code"],
        path="/path/claude/SKILL.md",
        sources=["/path/claude/SKILL.md"],
        description="Short description",
        content="Content 1",
    )
    s2 = AgentSkill(
        name="test-skill",
        agent="antigravity",
        agents=["antigravity"],
        path="/path/antigravity/SKILL.md",
        sources=["/path/antigravity/SKILL.md"],
        description="Much longer comprehensive description",
        plugin_name="cool-plugin",
        content="Content 2",
    )
    s3 = AgentSkill(
        name="unique-skill",
        agent="antigravity",
        agents=["antigravity"],
        path="/path/unique/SKILL.md",
        sources=["/path/unique/SKILL.md"],
        description="Unique",
        content="Content 3",
    )

    deduped = deduplicate_skills([s1, s2, s3])
    assert len(deduped) == 2
    merged = next(s for s in deduped if s.name == "test-skill")
    assert "claude-code" in merged.agents
    assert "antigravity" in merged.agents
    assert "/path/claude/SKILL.md" in merged.sources
    assert "/path/antigravity/SKILL.md" in merged.sources
    assert merged.plugin_name == "cool-plugin"
    assert merged.description == "Much longer comprehensive description"


def test_deduplicate_plugins() -> None:
    p1 = AgentPlugin(
        name="my-plugin",
        agent="claude-code",
        agents=["claude-code"],
        source_dir="/path/claude/my-plugin",
        sources=["/path/claude/my-plugin"],
        skills=["skill1"],
        mcp_servers=["srv1"],
        enabled=True,
    )
    p2 = AgentPlugin(
        name="my-plugin",
        agent="antigravity",
        agents=["antigravity"],
        source_dir="/path/agy/my-plugin",
        sources=["/path/agy/my-plugin"],
        version="1.2.0",
        skills=["skill2"],
        mcp_servers=["srv2"],
        enabled=True,
    )
    deduped = deduplicate_plugins([p1, p2])
    assert len(deduped) == 1
    merged = deduped[0]
    assert "claude-code" in merged.agents
    assert "antigravity" in merged.agents
    assert merged.version == "1.2.0"
    assert set(merged.skills) == {"skill1", "skill2"}
    assert set(merged.mcp_servers) == {"srv1", "srv2"}
    assert len(merged.sources) == 2


def test_deduplicate_servers() -> None:
    s1 = ServerConfig(
        name="gemini",
        spec=HttpServer(url="https://api.gemini.example.com/sse"),
        tags=["agent:claude-code"],
        scope="global",
        source=Path("/tmp/claude.json"),
    )
    # Server with same URL should merge tags
    s2 = ServerConfig(
        name="gemini-alt",
        spec=HttpServer(url="https://api.gemini.example.com/sse/"),
        tags=["agent:antigravity"],
        scope="global",
        source=Path("/tmp/agy.json"),
    )
    # Server with same name but different command should be disambiguated
    s3 = ServerConfig(
        name="gemini",
        spec=StdioServer(command="different-cmd", args=[]),
        tags=["agent:antigravity"],
        scope="global",
        source=Path("/tmp/agy.json"),
    )

    deduped = deduplicate_servers([s1, s2, s3])
    assert len(deduped) == 2
    # Check that HTTP endpoint was merged
    http_srv = next(s for s in deduped if isinstance(s.spec, HttpServer))
    assert "agent:claude-code" in http_srv.tags
    assert "agent:antigravity" in http_srv.tags
    # Disambiguated server name has agent prefix
    diff_srv = next(s for s in deduped if isinstance(s.spec, StdioServer))
    assert diff_srv.name == "antigravity-gemini"
