"""Parallel catalog refresh: use the cache when fresh, otherwise ask the executor for a snapshot."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Sequence
from dataclasses import dataclass

import anyio

from mcpc.catalog import store
from mcpc.catalog.models import ServerSnapshot
from mcpc.errors import McpcError, translate
from mcpc.executor import Executor, ResolvedServer


@dataclass
class RefreshResult:
    """Outcome of refreshing one server.

    On success `snapshot` is set and `error` is None. On failure `error` is set and `snapshot` holds
    the last good (stale) snapshot with `last_error` populated, or None if the server was never cached.
    """

    name: str
    snapshot: ServerSnapshot | None
    error: McpcError | None
    from_cache: bool
    elapsed: float


async def refresh_servers(
    servers: Sequence[ResolvedServer],
    executor: Executor,
    *,
    force: bool = False,
    ttl_hours: float = 24,
    concurrency: int = 8,
) -> list[RefreshResult]:
    """Refresh every server's catalog in parallel; results come back in the order of `servers`.

    A fresh cache entry is returned without connecting unless `force` is set. One server failing
    never affects the others; its error is recorded in the cache (`last_error`) and returned.
    """
    results: list[RefreshResult | None] = [None] * len(servers)
    limiter = anyio.CapacityLimiter(max(1, concurrency))

    async def run(index: int, server: ResolvedServer) -> None:
        results[index] = await _refresh_one(server, executor, force, ttl_hours, limiter)

    async with anyio.create_task_group() as tg:
        for i, server in enumerate(servers):
            tg.start_soon(run, i, server)
    return [r for r in results if r is not None]


async def get_snapshot(
    server: ResolvedServer,
    executor: Executor,
    *,
    ttl_hours: float,
    force: bool = False,
) -> ServerSnapshot:
    """Return a fresh-enough snapshot for one server, refreshing if needed. Raises McpcError on failure."""
    result = (await refresh_servers([server], executor, force=force, ttl_hours=ttl_hours, concurrency=1))[0]
    if result.error is not None:
        raise result.error
    assert result.snapshot is not None
    return result.snapshot


async def _refresh_one(
    server: ResolvedServer,
    executor: Executor,
    force: bool,
    ttl_hours: float,
    limiter: anyio.CapacityLimiter,
) -> RefreshResult:
    start = time.perf_counter()
    cfg = server.config
    cached = store.load(cfg)
    if not force and store.is_fresh(cached, ttl_hours):
        return RefreshResult(server.name, cached, None, True, time.perf_counter() - start)
    try:
        async with limiter:
            snap = await executor.snapshot(server, prior=cached)
    except Exception as exc:
        err = translate(exc, server.name)
        stale = _record_failure(server, err, cached)
        return RefreshResult(server.name, stale, err, False, time.perf_counter() - start)
    snap = _normalize(snap, server)
    _save_quietly(snap, server)
    return RefreshResult(server.name, snap, None, False, time.perf_counter() - start)


def _normalize(snap: ServerSnapshot, server: ResolvedServer) -> ServerSnapshot:
    """Make sure the snapshot is keyed to the config it was fetched for and carries no stale error."""
    update: dict[str, object] = {}
    if snap.spec_hash != server.config.spec_hash():
        update["spec_hash"] = server.config.spec_hash()
    if snap.last_error is not None:
        update["last_error"] = None
    return snap.model_copy(update=update) if update else snap


def _save_quietly(snap: ServerSnapshot, server: ResolvedServer) -> None:
    """A cache write failure must not turn a successful fetch into an error."""
    with contextlib.suppress(OSError):
        store.save(snap, server.config)


def _record_failure(
    server: ResolvedServer, err: McpcError, cached: ServerSnapshot | None
) -> ServerSnapshot | None:
    try:
        recorded = store.record_error(server.config, err.message)
    except OSError:
        recorded = cached.model_copy(update={"last_error": err.message}) if cached else None
    # Only expose a snapshot when there is previously fetched data behind it.
    return recorded if cached is not None else None
