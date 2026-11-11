"""Unit tests for agent discovery functions."""

from __future__ import annotations

import json
from pathlib import Path

from mcpc.agents.claude import parse_skill_markdown
from mcpc.agents.discovery import (
    discover_all_skills,
    get_agents_overview,
    load_all_agent_servers,
    resolve_agent_mode,
)


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
