"""Claude Code integration: read MCP servers, plugins, and skills from Claude configurations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcpc.agents.models import AgentInfo, AgentPlugin, AgentSkill
from mcpc.config.models import HttpServer, ServerConfig, StdioServer


def parse_skill_markdown(path: Path) -> AgentSkill | None:
    """Read a SKILL.md and extract its metadata and content."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    name = path.parent.name
    description = ""
    content = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            raw_meta = parts[1]
            content = parts[2].lstrip("\r\n")
            for line in raw_meta.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k == "name" and v:
                        name = v
                    elif k == "description" and v:
                        description = v

    return AgentSkill(
        name=name,
        agent="claude-code",
        description=description,
        path=str(path),
        content=content,
    )


def _claude_home() -> Path:
    env = os.environ.get("CLAUDE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def _claude_user_config() -> Path:
    env = os.environ.get("CLAUDE_CONFIG")
    return Path(env).expanduser() if env else Path.home() / ".claude.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def detect_claude(workspace: Path | None = None) -> AgentInfo:
    """Check if Claude Code is configured on the machine or in the workspace."""
    user_config = _claude_user_config()
    home_dir = _claude_home()
    plugins_dir = home_dir / "plugins"

    detected = user_config.is_file() or home_dir.is_dir()
    if workspace and ((workspace / ".mcp.json").is_file() or (workspace / ".claude").is_dir()):
        detected = True

    servers = load_claude_servers(workspace)
    plugins = discover_claude_plugins(workspace)
    skills = discover_claude_skills(workspace)

    return AgentInfo(
        agent="claude-code",
        name="Claude Code",
        detected=detected,
        config_path=str(user_config) if user_config.is_file() else None,
        plugins_dir=str(plugins_dir) if plugins_dir.is_dir() else None,
        skills_count=len(skills),
        plugins_count=len(plugins),
        servers_count=len(servers),
    )


def load_claude_servers(
    workspace: Path | None = None, warnings: list[str] | None = None
) -> list[ServerConfig]:
    """Load MCP servers configured in Claude Code (user config and workspace .mcp.json)."""
    servers: list[ServerConfig] = []
    seen_names: set[str] = set()

    # 1. Global user config: ~/.claude.json
    user_config_path = _claude_user_config()
    data = _read_json(user_config_path)
    if data and "mcpServers" in data and isinstance(data["mcpServers"], dict):
        for name, raw in data["mcpServers"].items():
            sc = _parse_claude_server_entry(name, raw, user_config_path, "global")
            if sc and sc.name not in seen_names:
                servers.append(sc)
                seen_names.add(sc.name)

    # 2. Global directory: ~/.claude/mcp.json
    mcp_json_path = _claude_home() / "mcp.json"
    mcp_data = _read_json(mcp_json_path)
    if mcp_data and "mcpServers" in mcp_data and isinstance(mcp_data["mcpServers"], dict):
        for name, raw in mcp_data["mcpServers"].items():
            sc = _parse_claude_server_entry(name, raw, mcp_json_path, "global")
            if sc and sc.name not in seen_names:
                servers.append(sc)
                seen_names.add(sc.name)

    # 3. Workspace config: <workspace>/.mcp.json (Standard Anthropic MCP workspace config)
    if workspace:
        ws_mcp = workspace / ".mcp.json"
        ws_data = _read_json(ws_mcp)
        if ws_data:
            dict_servers = ws_data.get("mcpServers") if "mcpServers" in ws_data else ws_data
            if isinstance(dict_servers, dict):
                for name, raw in dict_servers.items():
                    sc = _parse_claude_server_entry(name, raw, ws_mcp, "workspace")
                    if sc and sc.name not in seen_names:
                        servers.append(sc)
                        seen_names.add(sc.name)

        # <workspace>/.claude/mcp.json
        ws_claude_mcp = workspace / ".claude" / "mcp.json"
        ws_cm_data = _read_json(ws_claude_mcp)
        if ws_cm_data and "mcpServers" in ws_cm_data and isinstance(ws_cm_data["mcpServers"], dict):
            for name, raw in ws_cm_data["mcpServers"].items():
                sc = _parse_claude_server_entry(name, raw, ws_claude_mcp, "workspace")
                if sc and sc.name not in seen_names:
                    servers.append(sc)
                    seen_names.add(sc.name)

    return servers


def _parse_claude_server_entry(
    name: str, raw: Any, source: Path, scope: str
) -> ServerConfig | None:
    if not isinstance(raw, dict):
        return None

    # Sanitize name
    safe_name = "".join(c if (c.isalnum() or c in "_.-") else "_" for c in name)
    if not safe_name:
        safe_name = "claude-server"

    url = raw.get("url") or raw.get("serverUrl")
    command = raw.get("command")

    tags = ["agent:claude-code"]
    desc = raw.get("description") or f"MCP server '{name}' from Claude Code"

    if url:
        transport_hint = raw.get("transport") or raw.get("type", "streamable-http")
        transport = "sse" if "sse" in str(transport_hint).lower() else "streamable-http"
        headers = raw.get("headers", {})
        spec = HttpServer(url=url, transport=transport, headers=headers)
        return ServerConfig(
            name=safe_name,
            spec=spec,
            description=desc,
            tags=tags,
            scope=scope,  # type: ignore[arg-type]
            source=source,
        )

    if command:
        args = [str(a) for a in raw.get("args", [])]
        env = {k: str(v) for k, v in raw.get("env", {}).items()}
        cwd = raw.get("cwd")
        spec = StdioServer(command=command, args=args, env=env, cwd=cwd)
        return ServerConfig(
            name=safe_name,
            spec=spec,
            description=desc,
            tags=tags,
            scope=scope,  # type: ignore[arg-type]
            source=source,
        )

    return None


def discover_claude_plugins(workspace: Path | None = None) -> list[AgentPlugin]:
    """Discover plugins registered with Claude Code."""
    plugins: list[AgentPlugin] = []
    seen_dirs: set[Path] = set()

    search_dirs: list[Path] = [_claude_home() / "plugins"]
    if workspace:
        search_dirs.append(workspace / ".claude" / "plugins")

    for base in search_dirs:
        if not base.is_dir():
            continue
        for pdir in base.iterdir():
            if not pdir.is_dir() or pdir in seen_dirs:
                continue
            seen_dirs.add(pdir)

            meta = _read_json(pdir / "plugin.json") or _read_json(pdir / "marketplace.json") or {}
            name = meta.get("name", pdir.name)
            desc = meta.get("description")
            ver = meta.get("version")
            author = meta.get("author")
            if isinstance(author, dict):
                author = author.get("name")

            skills = [s.name for s in _scan_skills_in_dir(pdir, "claude-code")]
            mcp_servers = []
            mcp_file = pdir / "mcp.json" if (pdir / "mcp.json").is_file() else pdir / "mcp_config.json"
            if mcp_file.is_file():
                mdata = _read_json(mcp_file) or {}
                mcp_servers = list(mdata.get("mcpServers", {}).keys())

            plugins.append(
                AgentPlugin(
                    name=name,
                    agent="claude-code",
                    version=ver,
                    description=desc,
                    author=str(author) if author else None,
                    homepage=meta.get("homepage"),
                    enabled=True,
                    source_dir=str(pdir),
                    skills=skills,
                    mcp_servers=mcp_servers,
                )
            )

    return plugins


def discover_claude_skills(workspace: Path | None = None) -> list[AgentSkill]:
    """Discover skills available to Claude Code (including shared ~/.agents/skills)."""
    skills: list[AgentSkill] = []
    seen_paths: set[str] = set()

    search_roots = [
        _claude_home() / "skills",
        Path.home() / ".agents" / "skills",
    ]
    if workspace:
        search_roots.extend([
            workspace / ".claude" / "skills",
            workspace / ".agents" / "skills",
        ])

    for root in search_roots:
        if not root.is_dir():
            continue
        for skill in _scan_skills_in_dir(root, "claude-code"):
            if skill.path not in seen_paths:
                skills.append(skill)
                seen_paths.add(skill.path)

    return skills


def _scan_skills_in_dir(root: Path, agent_type: Any) -> list[AgentSkill]:
    found: list[AgentSkill] = []
    if not root.is_dir():
        return found
    for item in root.rglob("SKILL.md"):
        parsed = parse_skill_markdown(item)
        if parsed:
            found.append(parsed)
    return found
