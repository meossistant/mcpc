"""`mcpc search`: BM25 search over the cached catalog (plan §4.1, §7)."""

from __future__ import annotations

from mcpc.catalog import refresh as catalog_refresh
from mcpc.catalog import store
from mcpc.catalog.models import ServerSnapshot
from mcpc.cli.context import Ctx, warn
from mcpc.config.models import ServerConfig
from mcpc.errors import McpcError, UsageError
from mcpc.executor import ResolvedServer
from mcpc.output import json as out_json
from mcpc.output import text as out_text
from mcpc.search.index import CatalogIndex

_TYPES = ("tool", "server")


async def _search(c: Ctx, query: str, limit: int, server: str | None, type_: str) -> None:
    if type_ not in _TYPES:
        raise UsageError(f"--type must be one of: {', '.join(_TYPES)} (got '{type_}')")
    only = c.server(server) if server is not None else None
    configs = c.servers()
    snapshots = await _collect_snapshots(c, configs, only)
    index = CatalogIndex([(cfg, snapshots.get(cfg.name)) for cfg in configs])
    if type_ == "server":
        _emit_servers(c, query, index, limit, server)
    else:
        _emit_tools(c, query, index, limit, server)


def _emit_tools(c: Ctx, query: str, index: CatalogIndex, limit: int, server: str | None) -> None:
    hits = index.search_tools(query, limit=limit, server=server)
    text = out_text.render_tool_hits(hits) if hits else _no_matches(query)
    c.emit(text=text, data=out_json.tool_hits_json(query, hits))


def _emit_servers(c: Ctx, query: str, index: CatalogIndex, limit: int, server: str | None) -> None:
    if server is None:
        hits = index.search_servers(query, limit=limit)
    else:
        everything = index.search_servers(query, limit=len(c.servers()))
        hits = [h for h in everything if h.server == server][: max(limit, 0)]
    text = out_text.render_server_hits(hits) if hits else _no_matches(query)
    c.emit(text=text, data=out_json.server_hits_json(query, hits))


def _no_matches(query: str) -> str:
    return f'no matches for "{query}"'


async def _collect_snapshots(
    c: Ctx, configs: list[ServerConfig], only: ServerConfig | None
) -> dict[str, ServerSnapshot | None]:
    """Cached snapshot per server, refreshing stale or missing ones for trusted servers.

    With `--server`, only that server is refreshed. Failures warn and fall back to the cached data.
    """
    ttl = c.config().settings.cache_ttl_hours
    snapshots: dict[str, ServerSnapshot | None] = {cfg.name: store.load(cfg) for cfg in configs}
    to_refresh = [
        cfg
        for cfg in configs
        if (only is None or cfg.name == only.name)
        and not c.is_untrusted(cfg)
        and (c.refresh or not store.is_fresh(snapshots[cfg.name], ttl))
    ]
    resolved = _resolve_all(c, to_refresh)
    if not resolved:
        return snapshots
    results = await catalog_refresh.refresh_servers(
        resolved, await c.executor(), force=c.refresh, ttl_hours=ttl
    )
    for result in results:
        if result.error is not None:
            warn(f"{result.name}: {result.error.message}")
        snapshots[result.name] = result.snapshot
    return snapshots


def _resolve_all(c: Ctx, configs: list[ServerConfig]) -> list[ResolvedServer]:
    resolved: list[ResolvedServer] = []
    for cfg in configs:
        try:
            resolved.append(c.resolve(cfg))
        except McpcError as err:
            warn(f"{cfg.name}: {err.message}")
    return resolved
