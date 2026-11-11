"""Tests for parallel catalog refresh, using a fake executor (no real servers)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from mcp.types import Tool

from mcpc.catalog import store
from mcpc.catalog.models import ServerSnapshot
from mcpc.catalog.refresh import RefreshResult, get_snapshot, refresh_servers
from mcpc.config.models import ServerConfig, StdioServer
from mcpc.errors import ConnectionFailedError, McpcError, ProtocolFailedError
from mcpc.executor import ResolvedServer

pytestmark = pytest.mark.anyio


def make_server(name: str, args: list[str] | None = None) -> ResolvedServer:
    spec = StdioServer(command="python", args=args or [])
    cfg = ServerConfig(name=name, spec=spec, scope="global", source=Path("mcp.json"))
    return ResolvedServer(config=cfg, spec=spec)


class FakeExecutor:
    """Returns canned snapshots; can fail per server, sleep, and records calls and concurrency."""

    def __init__(
        self,
        *,
        delay: float = 0.0,
        fail: dict[str, BaseException] | None = None,
        tool: str = "fresh_tool",
    ) -> None:
        self.delay = delay
        self.fail = fail or {}
        self.tool = tool
        self.calls: list[str] = []
        self.priors: dict[str, ServerSnapshot | None] = {}
        self.running = 0
        self.max_running = 0

    async def snapshot(self, server: ResolvedServer, prior: ServerSnapshot | None = None) -> ServerSnapshot:
        self.calls.append(server.name)
        self.priors[server.name] = prior
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if self.delay:
                await anyio.sleep(self.delay)
            if server.name in self.fail:
                raise self.fail[server.name]
            return ServerSnapshot(
                server=server.name,
                spec_hash=server.config.spec_hash(),
                tools=[Tool(name=self.tool, input_schema={"type": "object"})],
            )
        finally:
            self.running -= 1

    async def aclose(self) -> None:  # pragma: no cover - protocol completeness
        return None


def seed(server: ResolvedServer, *, age_hours: float = 1, tool: str = "cached_tool") -> None:
    snap = ServerSnapshot(
        server=server.name,
        spec_hash=server.config.spec_hash(),
        fetched_at=datetime.now(UTC) - timedelta(hours=age_hours),
        tools=[Tool(name=tool, input_schema={"type": "object"})],
    )
    store.save(snap, server.config)


def tool_names(snap: ServerSnapshot | None) -> list[str]:
    assert snap is not None
    return [t.name for t in snap.tools]


async def test_missing_cache_fetches_and_saves() -> None:
    srv = make_server("a")
    ex = FakeExecutor()
    [res] = await refresh_servers([srv], ex)  # type: ignore[arg-type]
    assert isinstance(res, RefreshResult)
    assert res.name == "a"
    assert res.error is None
    assert res.from_cache is False
    assert res.elapsed >= 0
    assert tool_names(res.snapshot) == ["fresh_tool"]
    assert ex.priors["a"] is None
    loaded = store.load(srv.config)
    assert tool_names(loaded) == ["fresh_tool"]


async def test_fresh_cache_skips_executor() -> None:
    srv = make_server("a")
    seed(srv, age_hours=1)
    ex = FakeExecutor()
    [res] = await refresh_servers([srv], ex, ttl_hours=24)  # type: ignore[arg-type]
    assert ex.calls == []
    assert res.from_cache is True
    assert res.error is None
    assert tool_names(res.snapshot) == ["cached_tool"]


async def test_stale_cache_refreshes_and_passes_prior() -> None:
    srv = make_server("a")
    seed(srv, age_hours=30)
    ex = FakeExecutor()
    [res] = await refresh_servers([srv], ex, ttl_hours=24)  # type: ignore[arg-type]
    assert ex.calls == ["a"]
    assert res.from_cache is False
    assert tool_names(res.snapshot) == ["fresh_tool"]
    assert tool_names(ex.priors["a"]) == ["cached_tool"]
    assert tool_names(store.load(srv.config)) == ["fresh_tool"]


async def test_force_refreshes_fresh_cache() -> None:
    srv = make_server("a")
    seed(srv, age_hours=0.1)
    ex = FakeExecutor()
    [res] = await refresh_servers([srv], ex, force=True)  # type: ignore[arg-type]
    assert ex.calls == ["a"]
    assert res.from_cache is False
    assert tool_names(res.snapshot) == ["fresh_tool"]


async def test_failure_records_error_and_keeps_old_tools() -> None:
    srv = make_server("a")
    seed(srv, age_hours=30)
    ex = FakeExecutor(fail={"a": ConnectionFailedError("refused")})
    [res] = await refresh_servers([srv], ex, ttl_hours=24)  # type: ignore[arg-type]
    assert isinstance(res.error, ConnectionFailedError)
    assert res.error.server == "a"
    assert res.from_cache is False
    assert res.snapshot is not None
    assert res.snapshot.last_error == "refused"
    assert tool_names(res.snapshot) == ["cached_tool"]

    loaded = store.load(srv.config)
    assert loaded is not None
    assert loaded.last_error == "refused"
    assert tool_names(loaded) == ["cached_tool"]

    # A cache entry with last_error is never considered fresh, so the next run retries.
    ex2 = FakeExecutor()
    [res2] = await refresh_servers([srv], ex2, ttl_hours=24)  # type: ignore[arg-type]
    assert ex2.calls == ["a"]
    assert res2.error is None
    loaded2 = store.load(srv.config)
    assert loaded2 is not None
    assert loaded2.last_error is None


async def test_failure_without_prior_cache_records_error_entry() -> None:
    srv = make_server("a")
    ex = FakeExecutor(fail={"a": ConnectionFailedError("refused")})
    [res] = await refresh_servers([srv], ex)  # type: ignore[arg-type]
    assert res.error is not None
    assert res.snapshot is None
    loaded = store.load(srv.config)
    assert loaded is not None
    assert loaded.last_error == "refused"
    assert loaded.tools == []


async def test_generic_exception_is_translated() -> None:
    srv = make_server("a")
    ex = FakeExecutor(fail={"a": ValueError("weird")})
    [res] = await refresh_servers([srv], ex)  # type: ignore[arg-type]
    assert isinstance(res.error, McpcError)
    assert isinstance(res.error, ProtocolFailedError)
    assert res.error.server == "a"
    assert "weird" in res.error.message


async def test_exception_group_is_translated() -> None:
    srv = make_server("a")
    group = ExceptionGroup("tg", [ConnectionFailedError("closed")])
    ex = FakeExecutor(fail={"a": group})
    [res] = await refresh_servers([srv], ex)  # type: ignore[arg-type]
    assert isinstance(res.error, ConnectionFailedError)


async def test_one_failure_does_not_cancel_others_and_order_is_kept() -> None:
    servers = [make_server(n) for n in ("s0", "s1", "s2", "s3")]
    ex = FakeExecutor(delay=0.05, fail={"s1": ConnectionFailedError("down")})
    results = await refresh_servers(servers, ex)  # type: ignore[arg-type]
    assert [r.name for r in results] == ["s0", "s1", "s2", "s3"]
    assert [r.error is None for r in results] == [True, False, True, True]
    assert tool_names(store.load(servers[3].config)) == ["fresh_tool"]


async def test_results_keep_input_order_with_mixed_cache_and_fetch() -> None:
    servers = [make_server(n) for n in ("x", "y", "z")]
    seed(servers[1])
    ex = FakeExecutor()
    results = await refresh_servers(servers, ex, ttl_hours=24)  # type: ignore[arg-type]
    assert [r.name for r in results] == ["x", "y", "z"]
    assert [r.from_cache for r in results] == [False, True, False]
    assert sorted(ex.calls) == ["x", "z"]


async def test_servers_refresh_in_parallel() -> None:
    servers = [make_server(f"p{i}") for i in range(4)]
    ex = FakeExecutor(delay=0.2)
    start = time.perf_counter()
    results = await refresh_servers(servers, ex, concurrency=8)  # type: ignore[arg-type]
    elapsed = time.perf_counter() - start
    assert elapsed < 0.6
    assert ex.max_running == 4
    assert all(r.error is None for r in results)


async def test_concurrency_limit_is_respected() -> None:
    servers = [make_server(f"c{i}") for i in range(6)]
    ex = FakeExecutor(delay=0.05)
    await refresh_servers(servers, ex, concurrency=2)  # type: ignore[arg-type]
    assert ex.max_running == 2


async def test_empty_server_list() -> None:
    assert await refresh_servers([], FakeExecutor()) == []  # type: ignore[arg-type]


async def test_spec_change_invalidates_cache() -> None:
    old = make_server("a", args=["old"])
    seed(old, age_hours=0.1)
    new = make_server("a", args=["new"])
    ex = FakeExecutor()
    [res] = await refresh_servers([new], ex, ttl_hours=24)  # type: ignore[arg-type]
    assert ex.calls == ["a"]
    assert ex.priors["a"] is None
    assert res.from_cache is False
    assert not store.snapshot_path(old.config).exists()
    assert store.snapshot_path(new.config).exists()


async def test_snapshot_from_executor_is_rekeyed_to_config() -> None:
    srv = make_server("a")

    class WrongHash(FakeExecutor):
        async def snapshot(
            self, server: ResolvedServer, prior: ServerSnapshot | None = None
        ) -> ServerSnapshot:
            snap = await super().snapshot(server, prior)
            return snap.model_copy(update={"spec_hash": "bogus", "last_error": "old"})

    [res] = await refresh_servers([srv], WrongHash())  # type: ignore[arg-type]
    assert res.snapshot is not None
    assert res.snapshot.spec_hash == srv.config.spec_hash()
    assert res.snapshot.last_error is None
    assert store.load(srv.config) is not None


async def test_cache_write_failure_does_not_fail_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", boom)
    [res] = await refresh_servers([make_server("a")], FakeExecutor())  # type: ignore[arg-type]
    assert res.error is None
    assert tool_names(res.snapshot) == ["fresh_tool"]


async def test_get_snapshot_returns_cached_when_fresh() -> None:
    srv = make_server("a")
    seed(srv)
    ex = FakeExecutor()
    snap = await get_snapshot(srv, ex, ttl_hours=24)  # type: ignore[arg-type]
    assert ex.calls == []
    assert tool_names(snap) == ["cached_tool"]


async def test_get_snapshot_force_and_raises_on_failure() -> None:
    srv = make_server("a")
    seed(srv)
    snap = await get_snapshot(srv, FakeExecutor(), ttl_hours=24, force=True)  # type: ignore[arg-type]
    assert tool_names(snap) == ["fresh_tool"]

    failing = FakeExecutor(fail={"a": ConnectionFailedError("nope")})
    with pytest.raises(ConnectionFailedError, match="nope"):
        await get_snapshot(srv, failing, ttl_hours=24, force=True)  # type: ignore[arg-type]
