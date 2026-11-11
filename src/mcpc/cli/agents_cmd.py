"""CLI implementation for coding agent inspection (mcpc plugins, mcpc skills, mcpc agents)."""

from __future__ import annotations

from mcpc.agents.discovery import (
    discover_all_plugins,
    discover_all_skills,
    get_agents_overview,
)
from mcpc.cli.context import Ctx
from mcpc.errors import NotFoundError


async def _agents_body(c: Ctx) -> None:
    """Show status of coding agents on the system."""
    overview = get_agents_overview(c.workspace)

    def _render_text() -> str:
        lines = []
        for info in overview:
            status = "detected" if info.detected else "not found"
            lines.append(f"{info.name} ({info.agent}): {status}")
            if info.config_path:
                lines.append(f"  Config:  {info.config_path}")
            if info.plugins_dir:
                lines.append(f"  Plugins: {info.plugins_dir}")
            asset_str = (
                f"  Assets:  {info.servers_count} servers, "
                f"{info.plugins_count} plugins, {info.skills_count} skills"
            )
            lines.append(asset_str)
        return "\n".join(lines)

    data = {"agents": [a.model_dump(mode="json") for a in overview]}
    c.emit(text=_render_text, data=data)


async def _plugins_body(
    c: Ctx,
    name: str | None = None,
    agent: str | None = None,
    enabled_only: bool = False,
    raw: bool = False,
) -> None:
    """List or inspect coding agent plugins."""
    mode = agent if agent else ("all" if c.agent == "none" else c.agent)
    plugins = discover_all_plugins(mode, c.workspace, deduplicate=not raw)

    if enabled_only:
        plugins = [p for p in plugins if p.enabled]

    if name:
        target = next((p for p in plugins if p.name.lower() == name.lower()), None)
        if not target:
            raise NotFoundError(f"plugin '{name}' not found under agent mode '{mode}'")

        def _render_plugin_text() -> str:
            agents_str = ", ".join(target.agents) if target.agents else target.agent
            lines = [
                f"Plugin:      {target.name}",
                f"Agent(s):    {agents_str}",
                f"Version:     {target.version or '-'}",
                f"Status:      {'enabled' if target.enabled else 'disabled'}",
                f"Author:      {target.author or '-'}",
                f"Homepage:    {target.homepage or '-'}",
                f"Directory:   {target.source_dir}",
            ]
            if len(target.sources) > 1:
                lines.append(f"All Sources: {', '.join(target.sources)}")
            lines.extend(
                [
                    f"Description: {target.description or '(no description)'}",
                    f"Servers:     {', '.join(target.mcp_servers) if target.mcp_servers else '(none)'}",
                    f"Skills:      {', '.join(target.skills) if target.skills else '(none)'}",
                ]
            )
            return "\n".join(lines)

        c.emit(text=_render_plugin_text, data=target.model_dump(mode="json"))
        return

    def _render_list_text() -> str:
        if not plugins:
            return "No plugins found."
        col_name = max(len("NAME"), max(len(p.name) for p in plugins))
        agent_strs = [", ".join(p.agents) if p.agents else p.agent for p in plugins]
        col_agent = max(len("AGENT"), max(len(a) for a in agent_strs))
        col_ver = max(len("VER"), max(len(p.version or "-") for p in plugins))
        header = (
            f"{'NAME':<{col_name}}  {'AGENT':<{col_agent}}  "
            f"{'VER':<{col_ver}}  STATUS    SERVERS  SKILLS  DESCRIPTION"
        )
        rows = [header, "-" * len(header)]
        for p, ag_str in zip(plugins, agent_strs, strict=False):
            status = "enabled" if p.enabled else "disabled"
            desc = (p.description or "").replace("\n", " ").strip()
            if len(desc) > 50:
                desc = desc[:47] + "..."
            ver = p.version or "-"
            row = (
                f"{p.name:<{col_name}}  {ag_str:<{col_agent}}  {ver:<{col_ver}}  "
                f"{status:<8}  {len(p.mcp_servers):<7}  {len(p.skills):<6}  {desc}"
            )
            rows.append(row)
        return "\n".join(rows)

    data = {"plugins": [p.model_dump(mode="json") for p in plugins]}
    c.emit(text=_render_list_text, data=data)


async def _skills_body(
    c: Ctx,
    name: str | None = None,
    agent: str | None = None,
    plugin: str | None = None,
    full: bool = False,
    raw: bool = False,
) -> None:
    """List or show coding agent skills."""
    mode = agent if agent else ("all" if c.agent == "none" else c.agent)
    skills = discover_all_skills(mode, c.workspace, deduplicate=not raw)

    if plugin:
        skills = [s for s in skills if s.plugin_name and s.plugin_name.lower() == plugin.lower()]

    if name:
        target = next((s for s in skills if s.name.lower() == name.lower()), None)
        if not target:
            raise NotFoundError(f"skill '{name}' not found under agent mode '{mode}'")

        def _render_skill_text() -> str:
            agents_str = ", ".join(target.agents) if target.agents else target.agent
            lines = [
                f"Skill:       {target.name}",
                f"Agent(s):    {agents_str}",
                f"Plugin:      {target.plugin_name or '-'}",
                f"Path:        {target.path}",
            ]
            if len(target.sources) > 1:
                lines.append(f"All Sources: {', '.join(target.sources)}")
            lines.extend(
                [
                    f"Description: {target.description or '(no description)'}",
                    "",
                    "--- Content ---",
                    target.content,
                ]
            )
            return "\n".join(lines)

        c.emit(text=_render_skill_text, data=target.model_dump(mode="json"))
        return

    def _render_skills_list_text() -> str:
        if not skills:
            return "No skills found."
        col_name = max(len("NAME"), max(len(s.name) for s in skills))
        agent_strs = [", ".join(s.agents) if s.agents else s.agent for s in skills]
        col_agent = max(len("AGENT"), max(len(a) for a in agent_strs))
        col_plugin = max(len("PLUGIN"), max(len(s.plugin_name or "-") for s in skills))
        header = f"{'NAME':<{col_name}}  {'AGENT':<{col_agent}}  {'PLUGIN':<{col_plugin}}  DESCRIPTION"
        rows = [header, "-" * len(header)]
        for s, ag_str in zip(skills, agent_strs, strict=False):
            desc = (s.description or "").replace("\n", " ").strip()
            if not full and len(desc) > 60:
                desc = desc[:57] + "..."
            plugin_str = s.plugin_name or "-"
            rows.append(f"{s.name:<{col_name}}  {ag_str:<{col_agent}}  {plugin_str:<{col_plugin}}  {desc}")
        return "\n".join(rows)

    data = {"skills": [s.model_dump(mode="json") for s in skills]}
    c.emit(text=_render_skills_list_text, data=data)
