"""Google Antigravity CLI integration: read MCP servers, plugins, and skills."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcpc.agents.claude import parse_skill_markdown
from mcpc.agents.models import AgentInfo, AgentPlugin, AgentSkill
from mcpc.config.models import HttpServer, ServerConfig, StdioServer


def _antigravity_config_dir() -> Path:
    env = os.environ.get("GEMINI_CONFIG_DIR") or os.environ.get("ANTIGRAVITY_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".gemini" / "config"


def _antigravity_plugins_dir() -> Path:
    return _antigravity_config_dir() / "plugins"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def detect_antigravity(workspace: Path | None = None) -> AgentInfo:
    """Check if Antigravity CLI is installed or configured on the system."""
    config_dir = _antigravity_config_dir()
    plugins_dir = _antigravity_plugins_dir()

    detected = config_dir.is_dir()
    if workspace and (workspace / ".gemini").is_dir():
        detected = True

    servers = load_antigravity_servers(workspace)
    plugins = discover_antigravity_plugins(workspace)
    skills = discover_antigravity_skills(workspace)

    return AgentInfo(
        agent="antigravity",
        name="Antigravity CLI",
        detected=detected,
        config_path=str(config_dir / "config.json") if (config_dir / "config.json").is_file() else None,
        plugins_dir=str(plugins_dir) if plugins_dir.is_dir() else None,
        skills_count=len(skills),
        plugins_count=len(plugins),
        servers_count=len(servers),
    )


def load_antigravity_servers(
    workspace: Path | None = None, warnings: list[str] | None = None
) -> list[ServerConfig]:
    """Load MCP servers configured in Antigravity global config, plugins, and workspace."""
    servers: list[ServerConfig] = []
    seen_names: set[str] = set()

    # 1. Global config: ~/.gemini/config/mcp_config.json & mcp.json
    config_dir = _antigravity_config_dir()
    for name_candidate in ("mcp_config.json", "mcp.json"):
        global_path = config_dir / name_candidate
        data = _read_json(global_path)
        if data and "mcpServers" in data and isinstance(data["mcpServers"], dict):
            for sname, raw in data["mcpServers"].items():
                sc = _parse_antigravity_server_entry(sname, raw, global_path, "global")
                if sc and sc.name not in seen_names:
                    servers.append(sc)
                    seen_names.add(sc.name)

    # 2. Plugins: ~/.gemini/config/plugins/<plugin>/mcp_config.json (if enabled)
    enabled_map = _get_plugin_enabled_map(config_dir)
    plugins_dir = _antigravity_plugins_dir()
    if plugins_dir.is_dir():
        for pdir in plugins_dir.iterdir():
            if not pdir.is_dir():
                continue
            pname = pdir.name
            is_enabled = enabled_map.get(pname, True)
            if not is_enabled:
                continue

            for mcp_candidate in ("mcp_config.json", "mcp.json"):
                mcp_file = pdir / mcp_candidate
                if mcp_file.is_file():
                    data = _read_json(mcp_file)
                    if data and "mcpServers" in data and isinstance(data["mcpServers"], dict):
                        for sname, raw in data["mcpServers"].items():
                            sc = _parse_antigravity_server_entry(
                                sname, raw, mcp_file, "global", plugin_name=pname
                            )
                            if sc and sc.name not in seen_names:
                                servers.append(sc)
                                seen_names.add(sc.name)

    # 3. Workspace: <workspace>/.gemini/mcp_config.json
    if workspace:
        for ws_name in ("mcp_config.json", "mcp.json"):
            ws_path = workspace / ".gemini" / ws_name
            data = _read_json(ws_path)
            if data and "mcpServers" in data and isinstance(data["mcpServers"], dict):
                for sname, raw in data["mcpServers"].items():
                    sc = _parse_antigravity_server_entry(sname, raw, ws_path, "workspace")
                    if sc and sc.name not in seen_names:
                        servers.append(sc)
                        seen_names.add(sc.name)

    return servers


def _get_plugin_enabled_map(config_dir: Path) -> dict[str, bool]:
    cfg = _read_json(config_dir / "config.json")
    if not cfg:
        return {}
    plugins_cfg = cfg.get("plugins")
    if not isinstance(plugins_cfg, dict):
        return {}
    res: dict[str, bool] = {}
    for k, v in plugins_cfg.items():
        if isinstance(v, dict) and "enabled" in v:
            res[k] = bool(v["enabled"])
        elif isinstance(v, bool):
            res[k] = v
    return res


def _parse_antigravity_server_entry(
    name: str, raw: Any, source: Path, scope: str, plugin_name: str | None = None
) -> ServerConfig | None:
    if not isinstance(raw, dict):
        return None

    safe_name = "".join(c if (c.isalnum() or c in "_.-") else "_" for c in name)
    if not safe_name:
        safe_name = "antigravity-server"

    url = raw.get("url") or raw.get("serverUrl")
    command = raw.get("command")

    tags = ["agent:antigravity"]
    if plugin_name:
        tags.append(f"plugin:{plugin_name}")

    desc = raw.get("description")
    if not desc:
        if plugin_name:
            desc = f"MCP server '{name}' from Antigravity plugin {plugin_name}"
        else:
            desc = f"MCP server '{name}' from Antigravity"

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


def discover_antigravity_plugins(workspace: Path | None = None) -> list[AgentPlugin]:
    """Discover all Antigravity CLI plugins installed on the system and in the workspace."""
    plugins: list[AgentPlugin] = []
    seen_dirs: set[Path] = set()

    config_dir = _antigravity_config_dir()
    enabled_map = _get_plugin_enabled_map(config_dir)

    search_dirs = [_antigravity_plugins_dir()]
    if workspace:
        search_dirs.append(workspace / ".gemini" / "plugins")

    for base in search_dirs:
        if not base.is_dir():
            continue
        for pdir in base.iterdir():
            if not pdir.is_dir() or pdir in seen_dirs:
                continue
            seen_dirs.add(pdir)

            meta = _read_json(pdir / "plugin.json") or {}
            name = meta.get("name", pdir.name)
            desc = meta.get("description")
            ver = meta.get("version")
            author = meta.get("author")
            if isinstance(author, dict):
                author = author.get("name")

            skills = [s.name for s in _scan_skills_in_dir(pdir, "antigravity", plugin_name=name)]
            mcp_servers: list[str] = []
            for mfile in ("mcp_config.json", "mcp.json"):
                mf = pdir / mfile
                if mf.is_file():
                    mdata = _read_json(mf) or {}
                    mcp_servers = list(mdata.get("mcpServers", {}).keys())
                    break

            is_enabled = enabled_map.get(pdir.name, True)

            plugins.append(
                AgentPlugin(
                    name=name,
                    agent="antigravity",
                    version=ver,
                    description=desc,
                    author=str(author) if author else None,
                    homepage=meta.get("homepage"),
                    enabled=is_enabled,
                    source_dir=str(pdir),
                    skills=skills,
                    mcp_servers=mcp_servers,
                )
            )

    return plugins


def discover_antigravity_skills(workspace: Path | None = None) -> list[AgentSkill]:
    """Discover all skills registered with Antigravity CLI."""
    skills: list[AgentSkill] = []
    seen_paths: set[str] = set()

    # 1. Plugin skills
    plugins = discover_antigravity_plugins(workspace)
    for p in plugins:
        pdir = Path(p.source_dir)
        for s in _scan_skills_in_dir(pdir, "antigravity", plugin_name=p.name):
            if s.path not in seen_paths:
                skills.append(s)
                seen_paths.add(s.path)

    # 2. Workspace skills: <workspace>/.gemini/skills
    if workspace:
        ws_skills = workspace / ".gemini" / "skills"
        for s in _scan_skills_in_dir(ws_skills, "antigravity"):
            if s.path not in seen_paths:
                skills.append(s)
                seen_paths.add(s.path)

    return skills


def _scan_skills_in_dir(
    root: Path, agent_type: Any, plugin_name: str | None = None
) -> list[AgentSkill]:
    found: list[AgentSkill] = []
    if not root.is_dir():
        return found
    for item in root.rglob("SKILL.md"):
        parsed = parse_skill_markdown(item)
        if parsed:
            parsed = parsed.model_copy(update={"agent": "antigravity", "plugin_name": plugin_name})
            found.append(parsed)
    return found
