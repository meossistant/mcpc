"""Tests for the on-disk catalog cache."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp.types import Tool

from mcpc import paths
from mcpc.catalog import store
from mcpc.catalog.models import ServerSnapshot
from mcpc.config.models import ServerConfig, StdioServer


def make_cfg(name: str = "books", command: str = "python", args: list[str] | None = None) -> ServerConfig:
    return ServerConfig(
        name=name,
        spec=StdioServer(command=command, args=args or []),
        scope="global",
        source=Path("mcp.json"),
    )


def make_snap(cfg: ServerConfig, tools: tuple[str, ...] = ("search",), **kw: object) -> ServerSnapshot:
    return ServerSnapshot(
        server=cfg.name,
        spec_hash=cfg.spec_hash(),
        tools=[Tool(name=t, input_schema={"type": "object"}) for t in tools],
        **kw,  # type: ignore[arg-type]
    )


def test_snapshot_path_uses_name_and_hash_prefix() -> None:
    cfg = make_cfg()
    p = store.snapshot_path(cfg)
    assert p.parent == paths.cache_dir()
    assert p.name == f"books-{cfg.spec_hash()[:12]}.json"


def test_save_and_load_roundtrip() -> None:
    cfg = make_cfg()
    snap = make_snap(cfg, instructions="hello")
    path = store.save(snap, cfg)
    assert path.exists()
    loaded = store.load(cfg)
    assert loaded is not None
    assert loaded.instructions == "hello"
    assert [t.name for t in loaded.tools] == ["search"]
    assert loaded.tools[0].input_schema == {"type": "object"}
    assert loaded.fetched_at == snap.fetched_at


def test_saved_json_is_compact_and_aliased() -> None:
    cfg = make_cfg()
    path = store.save(make_snap(cfg), cfg)
    text = path.read_text(encoding="utf-8")
    assert "\n" not in text
    assert '"inputSchema"' in text


def test_load_missing_returns_none() -> None:
    assert store.load(make_cfg()) is None


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"server": "books"}'])
def test_load_corrupt_returns_none(content: str) -> None:
    cfg = make_cfg()
    store.snapshot_path(cfg).write_text(content, encoding="utf-8")
    assert store.load(cfg) is None


def test_load_unreadable_returns_none() -> None:
    cfg = make_cfg()
    # A directory where the file should be makes read_text raise OSError.
    store.snapshot_path(cfg).mkdir()
    assert store.load(cfg) is None


def test_load_rejects_mismatched_spec_hash_inside_file() -> None:
    cfg = make_cfg()
    snap = make_snap(cfg).model_copy(update={"spec_hash": "0" * 64})
    store.save(snap, cfg)
    assert store.load(cfg) is None


def test_spec_change_invalidates_and_save_removes_old_file() -> None:
    old_cfg = make_cfg(args=["a"])
    new_cfg = make_cfg(args=["b"])
    old_path = store.save(make_snap(old_cfg), old_cfg)
    assert store.load(new_cfg) is None

    new_path = store.save(make_snap(new_cfg), new_cfg)
    assert new_path != old_path
    assert not old_path.exists()
    assert store.load(new_cfg) is not None


def test_save_keeps_files_of_other_servers_with_prefix_names() -> None:
    a, ab = make_cfg("a"), make_cfg("a-b")
    ab_path = store.save(make_snap(ab), ab)
    store.save(make_snap(a), a)
    assert ab_path.exists()
    assert store.clear("a") == 1
    assert ab_path.exists()


def test_save_leaves_no_tmp_files() -> None:
    cfg = make_cfg()
    for i in range(5):
        store.save(make_snap(cfg, tools=(f"t{i}",)), cfg)
    names = sorted(p.name for p in paths.cache_dir().iterdir())
    assert names == [store.snapshot_path(cfg).name]


def test_save_failure_cleans_up_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_cfg()

    def boom(src: object, dst: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(store, "_REPLACE_DELAY", 0)
    with pytest.raises(PermissionError):
        store.save(make_snap(cfg), cfg)
    assert list(paths.cache_dir().iterdir()) == []


def test_replace_retries_on_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_cfg()
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src: str, dst: str) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(store, "_REPLACE_DELAY", 0)
    store.save(make_snap(cfg), cfg)
    assert calls["n"] == 3
    assert store.load(cfg) is not None


def test_record_error_without_existing_creates_empty_snapshot() -> None:
    cfg = make_cfg()
    snap = store.record_error(cfg, "boom")
    assert snap.last_error == "boom"
    assert snap.tools == []
    loaded = store.load(cfg)
    assert loaded is not None
    assert loaded.last_error == "boom"


def test_record_error_keeps_old_data_and_fetched_at() -> None:
    cfg = make_cfg()
    old_time = datetime.now(UTC) - timedelta(hours=30)
    store.save(make_snap(cfg, tools=("keep_me",), fetched_at=old_time), cfg)
    snap = store.record_error(cfg, "connection refused")
    assert snap.fetched_at == old_time
    assert [t.name for t in snap.tools] == ["keep_me"]
    loaded = store.load(cfg)
    assert loaded is not None
    assert loaded.last_error == "connection refused"
    assert loaded.fetched_at == old_time


def test_is_fresh() -> None:
    cfg = make_cfg()
    now = datetime.now(UTC)
    fresh = make_snap(cfg, fetched_at=now - timedelta(hours=1))
    old = make_snap(cfg, fetched_at=now - timedelta(hours=25))
    errored = fresh.model_copy(update={"last_error": "x"})

    assert store.is_fresh(fresh, 24, now)
    assert not store.is_fresh(old, 24, now)
    assert not store.is_fresh(errored, 24, now)
    assert not store.is_fresh(None, 24, now)
    assert not store.is_fresh(fresh, 0, now)
    assert store.is_fresh(fresh, 24)  # default now


def test_clear_one_and_all() -> None:
    a, b = make_cfg("a"), make_cfg("b")
    store.save(make_snap(a), a)
    store.save(make_snap(b), b)
    assert store.clear("a") == 1
    assert store.load(a) is None
    assert store.load(b) is not None
    assert store.clear() == 1
    assert store.clear() == 0
    assert store.clear("missing") == 0
