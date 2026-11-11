"""Server-management commands: list, show, refresh, check, config, trust, untrust (plan 3.6, 4.1, 6)."""

from __future__ import annotations

import difflib
import sys
from dataclasses import dataclass
from typing import Any, Literal

import typer

from mcpc import trust as trust_mod
from mcpc.catalog import refresh as catalog_refresh
from mcpc.catalog import store
from mcpc.catalog.models import ServerSnapshot, server_description
from mcpc.cli.context import Ctx, warn
from mcpc.config import interpolate
from mcpc.config.models import HttpServer, ServerConfig, StdioServer
from mcpc.errors import McpcError, NotFoundError, UntrustedWorkspaceError, UsageError
from mcpc.executor import ResolvedServer
from mcpc.output import json as out_json
from mcpc.output import text as out_text
from mcpc.output.schemas import ServerListRow, Status

_REFRESH_CONCURRENCY = 8


# ---- small helpers --------------------------------------------------------------------------------


def _transport(spec: StdioServer | HttpServer) -> Literal["stdio", "streamable-http", "sse"]:
    return "stdio" if isinstance(spec, StdioServer) else spec.transport


def _target(spec: StdioServer | HttpServer) -> str:
    """Redacted command line or URL for display."""
    red = interpolate.redact_spec(spec)
    if isinstance(spec, StdioServer):
        return " ".join([red["command"], *red["args"]])
    return str(red["url"])


def _has_data(snap: ServerSnapshot | None) -> bool:
    """True when the snapshot holds a fetched catalog (not just a recorded error)."""
    if snap is None:
        return False
    return bool(
        snap.protocol_version is not None
        or snap.server_info is not None
        or snap.tools
        or snap.resources
        or snap.resource_templates
        or snap.prompts
    )


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0].strip() if lines else ""


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _config_order(c: Ctx) -> dict[str, int]:
    return {name: i for i, name in enumerate(c.config().servers)}


def _prepare(c: Ctx, cfg: ServerConfig) -> ResolvedServer | McpcError:
    """Trust check + interpolation; a failure is returned (not raised) so it can be reported per server."""
    try:
        return c.resolve(cfg)
    except McpcError as err:
        return err


# ---- list -----------------------------------------------------------------------------------------


def _build_row(
    c: Ctx, cfg: ServerConfig, snap: ServerSnapshot | None, refresh_error: str | None
) -> ServerListRow:
    error = refresh_error or (snap.last_error if snap is not None else None)
    status: Status
    if cfg.disabled:
        status, error = "disabled", None
    elif c.is_untrusted(cfg):
        status, error = "untrusted", None
    elif error:
        status = "error"
    elif snap is not None:
        status = "ok"
    else:
        status = "not cached"
    row = ServerListRow(
        name=cfg.name,
        description=server_description(cfg.description, snap),
        scope=cfg.scope,
        source=str(cfg.source),
        transport=_transport(cfg.spec),
        target=_target(cfg.spec),
        status=status,
        overrides_global=cfg.overrides_global,
        error=error,
    )
    if snap is not None and _has_data(snap):
        row.protocol_version = snap.protocol_version
        row.tools = len(snap.tools)
        row.resources = len(snap.resources)
        row.resource_templates = len(snap.resource_templates)
        row.prompts = len(snap.prompts)
        row.cache_age_hours = round(snap.age_hours(), 2)
    return row


async def _refresh_for_list(c: Ctx, cfgs: list[ServerConfig]) -> dict[str, catalog_refresh.RefreshResult]:
    """Force-refresh every enabled, trusted server. Resolve errors become per-row errors."""
    resolved: list[ResolvedServer] = []
    failures: dict[str, catalog_refresh.RefreshResult] = {}
    for cfg in cfgs:
        if cfg.disabled or c.is_untrusted(cfg):
            continue
        prepared = _prepare(c, cfg)
        if isinstance(prepared, McpcError):
            failures[cfg.name] = catalog_refresh.RefreshResult(
                cfg.name, store.load(cfg), prepared, False, 0.0
            )
        else:
            resolved.append(prepared)
    results = dict(failures)
    if resolved:
        executor = await c.executor()
        refreshed = await catalog_refresh.refresh_servers(
            resolved,
            executor,
            force=True,
            ttl_hours=c.config().settings.cache_ttl_hours,
            concurrency=_REFRESH_CONCURRENCY,
        )
        results.update({r.name: r for r in refreshed})
    return results


async def _list_body(c: Ctx, full: bool, include_all: bool) -> None:
    cfgs = c.servers(include_disabled=include_all)
    results = await _refresh_for_list(c, cfgs) if c.refresh else {}
    rows: list[ServerListRow] = []
    for cfg in cfgs:
        result = results.get(cfg.name)
        snap = result.snapshot if result is not None else store.load(cfg)
        message = result.error.message if result is not None and result.error is not None else None
        rows.append(_build_row(c, cfg, snap, message))
    c.emit(text=out_text.render_server_list(rows, full=full), data=out_json.server_list_json(rows))


# ---- show -----------------------------------------------------------------------------------------


@dataclass
class _Shown:
    snap: ServerSnapshot
    from_cache: bool  # served from a fresh cache entry, so a refresh could still change the answer
    untrusted: bool = False


async def _load_for_show(c: Ctx, cfg: ServerConfig, *, force: bool | None = None) -> _Shown:
    """Snapshot for `show`: cached if fresh, else refreshed; falls back to a stale cache with a warning."""
    cached = c.cached(cfg)
    if not cfg.disabled and c.is_untrusted(cfg) and cached is not None and _has_data(cached):
        warn(
            f'{cfg.name}: workspace is not trusted; showing the cached catalog (run "mcpc trust" to refresh)'
        )
        return _Shown(cached, from_cache=False, untrusted=True)
    fresh = (
        force is not True and not c.refresh and store.is_fresh(cached, c.config().settings.cache_ttl_hours)
    )
    try:
        snap = await c.snapshot(cfg, force=force)
    except McpcError as err:
        if cached is None or not _has_data(cached):
            raise
        warn(f"{cfg.name}: refresh failed ({_first_line(err.message)}); showing the cached catalog")
        return _Shown(cached.model_copy(update={"last_error": err.message}), from_cache=False)
    return _Shown(snap, from_cache=fresh)


def _unknown_tool(cfg: ServerConfig, name: str, snap: ServerSnapshot) -> NotFoundError:
    names = [t.name for t in snap.tools]
    close = difflib.get_close_matches(name, names, n=3)
    if close:
        hint = f" Did you mean: {', '.join(close)}?"
    elif names:
        hint = f" Available tools: {', '.join(names)}."
    else:
        hint = " The server reports no tools."
    return NotFoundError(f"unknown tool '{name}'.{hint}", server=cfg.name)


async def _show_body(c: Ctx, target: str, full: bool) -> None:
    server, sep, tool_name = target.partition("/")
    if not server or (sep and not tool_name):
        raise UsageError(f"expected 'server' or 'server/tool', got '{target}'")
    cfg = c.server(server)
    shown = await _load_for_show(c, cfg)
    if not sep:
        status: Status | None = "untrusted" if shown.untrusted else None
        c.emit(
            text=out_text.render_server(cfg, shown.snap, full=full),
            data=out_json.server_json(cfg, shown.snap, full, status=status),
        )
        return
    tool = shown.snap.tool(tool_name)
    if tool is None and shown.from_cache:
        shown = await _load_for_show(c, cfg, force=True)
        tool = shown.snap.tool(tool_name)
    if tool is None:
        raise _unknown_tool(cfg, tool_name, shown.snap)
    c.emit(text=out_text.render_tool(cfg.name, tool), data=out_json.tool_json(cfg.name, tool))


# ---- refresh / check ------------------------------------------------------------------------------


@dataclass
class _Outcome:
    cfg: ServerConfig
    result: catalog_refresh.RefreshResult | None = None
    error: McpcError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def untrusted(self) -> bool:
        return isinstance(self.error, UntrustedWorkspaceError)

    @property
    def snap(self) -> ServerSnapshot | None:
        return self.result.snapshot if self.result is not None else None

    @property
    def elapsed(self) -> float:
        return self.result.elapsed if self.result is not None else 0.0


def _select(c: Ctx, names: list[str] | None) -> list[ServerConfig]:
    """Named servers (config order, duplicates dropped) or every enabled server in scope."""
    if not names:
        return c.servers()
    order = _config_order(c)
    unique = {cfg.name: cfg for cfg in (c.server(n) for n in names)}
    return sorted(unique.values(), key=lambda s: order.get(s.name, len(order)))


async def _force_refresh(c: Ctx, cfgs: list[ServerConfig]) -> list[_Outcome]:
    """Connect to every server in parallel and save snapshots. Outcomes keep the order of `cfgs`."""
    outcomes: dict[str, _Outcome] = {}
    resolved: list[ResolvedServer] = []
    for cfg in cfgs:
        prepared = _prepare(c, cfg)
        if isinstance(prepared, McpcError):
            outcomes[cfg.name] = _Outcome(cfg, error=prepared)
        else:
            resolved.append(prepared)
    if resolved:
        executor = await c.executor()
        results = await catalog_refresh.refresh_servers(
            resolved,
            executor,
            force=True,
            ttl_hours=c.config().settings.cache_ttl_hours,
            concurrency=_REFRESH_CONCURRENCY,
        )
        by_cfg = {cfg.name: cfg for cfg in cfgs}
        for r in results:
            outcomes[r.name] = _Outcome(by_cfg[r.name], result=r, error=r.error)
    return [outcomes[cfg.name] for cfg in cfgs if cfg.name in outcomes]


def _exit_for(outcomes: list[_Outcome]) -> None:
    """Exit with the code of the first failing server (config order); return normally when all are ok."""
    for o in outcomes:
        if o.error is not None:
            raise typer.Exit(int(o.error.exit_code))


def _refresh_line(o: _Outcome) -> str:
    name = o.cfg.name
    if o.untrusted:
        return f'{name}  untrusted  run "mcpc trust"'
    if o.error is not None:
        return f"{name}  error  {_first_line(o.error.message)}"
    tools = len(o.snap.tools) if o.snap is not None else 0
    return f"{name}  ok  {_plural(tools, 'tool')}  {o.elapsed:.1f}s"


def _refresh_record(o: _Outcome) -> dict[str, Any]:
    return {
        "name": o.cfg.name,
        "ok": o.ok,
        "tools": len(o.snap.tools) if o.ok and o.snap is not None else None,
        "elapsed": round(o.elapsed, 3),
        "error": o.error.message if o.error is not None else None,
    }


def _capabilities(snap: ServerSnapshot) -> list[str]:
    return [k for k, v in (snap.capabilities or {}).items() if v is not None]


def _check_line(o: _Outcome) -> str:
    if not o.ok or o.snap is None:
        return _refresh_line(o)
    snap = o.snap
    info = snap.server_info
    server = info.name if info is not None else "-"
    caps = ",".join(_capabilities(snap)) or "-"
    return f"{o.cfg.name}  ok  {snap.protocol_version or '-'}  {server}  {o.elapsed:.2f}s  {caps}"


def _check_record(o: _Outcome) -> dict[str, Any]:
    record = _refresh_record(o)
    snap = o.snap if o.ok else None
    info = snap.server_info if snap is not None else None
    record.update(
        {
            "protocol_version": snap.protocol_version if snap is not None else None,
            "server_name": info.name if info is not None else None,
            "server_version": info.version if info is not None else None,
            "capabilities": _capabilities(snap) if snap is not None else None,
        }
    )
    return record


async def _refresh_body(c: Ctx, names: list[str] | None, *, check: bool) -> None:
    outcomes = await _force_refresh(c, _select(c, names))
    line = _check_line if check else _refresh_line
    record = _check_record if check else _refresh_record
    c.emit(
        text="\n".join(line(o) for o in outcomes) if outcomes else "No MCP servers to refresh.",
        data={"results": [record(o) for o in outcomes]},
    )
    _exit_for(outcomes)


# ---- config ---------------------------------------------------------------------------------------


def _config_text(c: Ctx) -> str:
    loaded = c.config()
    cfgs = c.servers(include_disabled=True)
    lines = [
        f"{s.name}  {s.scope}  {s.source}  {_transport(s.spec)}  {_target(s.spec)}"
        + ("  [disabled]" if s.disabled else "")
        for s in cfgs
    ]
    if not lines:
        lines = [out_text.EMPTY_SERVER_LIST]
    if loaded.warnings:
        lines.append("")
        lines.append("warnings:")
        lines.extend(f"  {w}" for w in loaded.warnings)
    return "\n".join(lines)


def _config_data(c: Ctx) -> dict[str, Any]:
    loaded = c.config()
    return {
        "workspace_root": str(loaded.workspace_root),
        "global_path": str(loaded.global_path),
        "workspace_path": str(loaded.workspace_path),
        "servers": [
            {
                "name": s.name,
                "scope": s.scope,
                "source": str(s.source),
                "disabled": s.disabled,
                "overrides_global": s.overrides_global,
                "spec": interpolate.redact_spec(s.spec),
            }
            for s in c.servers(include_disabled=True)
        ],
        "warnings": list(loaded.warnings),
    }


def _config_path_output(c: Ctx) -> None:
    loaded = c.config()
    g_exists = loaded.global_path.is_file()
    w_exists = loaded.workspace_path.is_file()
    text = "\n".join(
        [
            f"global: {loaded.global_path} ({'exists' if g_exists else 'missing'})",
            f"workspace: {loaded.workspace_path} ({'exists' if w_exists else 'missing'})",
            f"workspace root: {loaded.workspace_root}",
        ]
    )
    data = {
        "global_path": str(loaded.global_path),
        "global_exists": g_exists,
        "workspace_path": str(loaded.workspace_path),
        "workspace_exists": w_exists,
        "workspace_root": str(loaded.workspace_root),
    }
    c.emit(text=text, data=data)


# ---- trust ----------------------------------------------------------------------------------------


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def _confirm_trust(c: Ctx, root: str, lines: list[str], yes: bool) -> bool:
    """Show what would be trusted and ask. With --json the prose goes to stderr to keep stdout parseable."""
    err = c.json
    typer.echo(
        f"The workspace {root} defines MCP servers that run commands or send your environment variables:",
        err=err,
    )
    for line in lines:
        typer.echo(f"  {line}", err=err)
    if yes:
        return True
    try:
        return typer.confirm("Trust this workspace configuration?", default=False, err=err)
    except typer.Abort:
        return False


async def _trust_body(c: Ctx, yes: bool) -> None:
    loaded = c.config()
    root = str(loaded.workspace_root)
    lines = trust_mod.describe_untrusted(loaded)
    if not lines:
        c.emit(
            text=f"nothing to trust in {root}",
            data={"workspace_root": root, "trusted": True, "changed": False},
        )
        return
    # Security boundary: an agent driving mcpc non-interactively must not be able to approve a workspace.
    if not _stdin_is_tty():
        raise UsageError(f"mcpc trust needs an interactive terminal; run it yourself in {root}")
    if not _confirm_trust(c, root, lines, yes):
        c.emit(text=f"not trusted {root}", data={"workspace_root": root, "trusted": False, "changed": False})
        raise typer.Exit(1)
    trust_mod.trust_workspace(loaded)
    c.emit(text=f"trusted {root}", data={"workspace_root": root, "trusted": True, "changed": True})


async def _untrust_body(c: Ctx) -> None:
    loaded = c.config()
    root = str(loaded.workspace_root)
    if trust_mod.untrust_workspace(loaded):
        c.emit(text=f"untrusted {root}", data={"workspace_root": root, "trusted": False, "changed": True})
    else:
        c.emit(
            text=f"{root} was not trusted", data={"workspace_root": root, "trusted": False, "changed": False}
        )
