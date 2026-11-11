"""Tests for mcpc.config.loader: workspace detection, parsing, merging."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mcpc import paths
from mcpc.config.loader import find_workspace_root, load_config
from mcpc.config.models import HttpServer, StdioServer
from mcpc.errors import ConfigError


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_global(servers: dict[str, Any] | None = None, **extra: Any) -> Path:
    data: dict[str, Any] = {"mcpServers": servers or {}, **extra}
    return write_json(paths.global_config_path(), data)


def write_workspace(root: Path, servers: dict[str, Any] | None = None, **extra: Any) -> Path:
    data: dict[str, Any] = {"mcpServers": servers or {}, **extra}
    return write_json(paths.workspace_config_path(root), data)


# --- workspace detection -------------------------------------------------------------------------


def test_find_root_prefers_mcpc_dir(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / ".mcpc").mkdir(parents=True)
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert find_workspace_root(nested) == root.resolve()


def test_find_root_uses_git_when_no_mcpc(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    assert find_workspace_root(nested) == root.resolve()


def test_find_root_mcpc_beats_git_even_when_deeper_git(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    (outer / ".mcpc").mkdir(parents=True)
    (inner / ".git").mkdir(parents=True)
    assert find_workspace_root(inner) == outer.resolve()


def test_find_root_falls_back_to_start(tmp_path: Path) -> None:
    start = tmp_path / "nothing" / "here"
    start.mkdir(parents=True)
    assert find_workspace_root(start) == start.resolve()


def test_find_root_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "proj"
    (root / ".mcpc").mkdir(parents=True)
    monkeypatch.chdir(root)
    assert find_workspace_root() == root.resolve()


def test_find_root_ignores_global_mcpc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "fakehome"
    mcpc_home = home / ".mcpc"
    mcpc_home.mkdir(parents=True)
    monkeypatch.setenv("MCPC_HOME", str(mcpc_home))
    start = home / "work"
    start.mkdir()
    assert find_workspace_root(start) == start.resolve()


# --- parsing -------------------------------------------------------------------------------------


def test_missing_files_give_empty_config(tmp_path: Path) -> None:
    loaded = load_config(workspace=tmp_path)
    assert loaded.servers == {}
    assert loaded.warnings == []
    assert loaded.workspace_root == tmp_path.resolve()
    assert loaded.global_path == paths.global_config_path()
    assert loaded.workspace_path == paths.workspace_config_path(tmp_path.resolve())


def test_stdio_entry(tmp_path: Path) -> None:
    write_global(
        {
            "fs": {
                "command": "npx",
                "args": ["-y", "server"],
                "env": {"A": "1"},
                "cwd": "/tmp",
                "description": "Files",
                "tags": ["files"],
                "timeout": 60,
                "mode": "legacy",
            }
        }
    )
    cfg = load_config(workspace=tmp_path).servers["fs"]
    assert cfg.spec == StdioServer(command="npx", args=["-y", "server"], env={"A": "1"}, cwd="/tmp")
    assert (cfg.description, cfg.tags, cfg.timeout, cfg.mode) == ("Files", ["files"], 60, "legacy")
    assert cfg.scope == "global"
    assert cfg.source == paths.global_config_path()
    assert not cfg.disabled and not cfg.overrides_global


def test_http_entry_defaults_to_streamable(tmp_path: Path) -> None:
    write_global({"gh": {"url": "https://x.example/mcp", "headers": {"Authorization": "Bearer t"}}})
    spec = load_config(workspace=tmp_path).servers["gh"].spec
    assert spec == HttpServer(url="https://x.example/mcp", headers={"Authorization": "Bearer t"})
    assert isinstance(spec, HttpServer) and spec.transport == "streamable-http"


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"transport": "sse"}, "sse"),
        ({"transport": "streamable-http"}, "streamable-http"),
        ({"type": "sse"}, "sse"),
        ({"type": "http"}, "streamable-http"),
        ({"type": "streamable-http"}, "streamable-http"),
        ({"type": "SSE"}, "sse"),
        ({"type": "http", "transport": "sse"}, "sse"),  # "transport" wins over "type"
    ],
)
def test_http_transport_aliases(tmp_path: Path, extra: dict[str, str], expected: str) -> None:
    write_global({"s": {"url": "http://localhost:9/x", **extra}})
    spec = load_config(workspace=tmp_path).servers["s"].spec
    assert isinstance(spec, HttpServer)
    assert spec.transport == expected


def test_type_stdio_alias_with_command(tmp_path: Path) -> None:
    write_global({"s": {"type": "stdio", "command": "python"}})
    loaded = load_config(workspace=tmp_path)
    assert isinstance(loaded.servers["s"].spec, StdioServer)
    assert loaded.warnings == []


@pytest.mark.parametrize(
    "entry",
    [
        {"description": "neither"},
        {"command": "x", "url": "http://y"},
        {"command": ""},
        {"url": "  "},
        {"command": "x", "type": "sse"},
        {"url": "http://y", "type": "stdio"},
        {"url": "http://y", "type": "websocket"},
        {"command": "x", "args": "not-a-list"},
        {"command": "x", "timeout": -1},
        {"command": "x", "timeout": "soon"},
        {"command": "x", "mode": "bogus"},
        {"command": "x", "disabled": "yes"},
        "just a string",
        ["list"],
    ],
)
def test_bad_entries_become_warnings(tmp_path: Path, entry: Any) -> None:
    write_global({"bad": entry, "good": {"command": "ok"}})
    loaded = load_config(workspace=tmp_path)
    assert list(loaded.servers) == ["good"]
    assert len(loaded.warnings) == 1
    assert "bad" in loaded.warnings[0]
    assert str(paths.global_config_path()) in loaded.warnings[0]


def test_invalid_server_name_warns(tmp_path: Path) -> None:
    write_global({"has space": {"command": "x"}, "ok-name_1.2": {"command": "y"}})
    loaded = load_config(workspace=tmp_path)
    assert list(loaded.servers) == ["ok-name_1.2"]
    assert any("has space" in w and "invalid server name" in w for w in loaded.warnings)


def test_warning_does_not_echo_values(tmp_path: Path) -> None:
    write_global({"bad": {"command": "x", "args": "super-secret-token"}})
    loaded = load_config(workspace=tmp_path)
    assert loaded.warnings
    assert "super-secret-token" not in " ".join(loaded.warnings)


def test_mcp_servers_not_an_object_warns(tmp_path: Path) -> None:
    write_json(paths.global_config_path(), {"mcpServers": ["x"]})
    loaded = load_config(workspace=tmp_path)
    assert loaded.servers == {}
    assert len(loaded.warnings) == 1


def test_bad_json_names_file_and_line(tmp_path: Path) -> None:
    path = paths.global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{\n  "mcpServers": {\n    oops\n  }\n}', encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(workspace=tmp_path)
    assert str(path) in info.value.message
    assert "line 3" in info.value.message


def test_comments_are_not_tolerated(tmp_path: Path) -> None:
    path = paths.global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{ // hi\n "mcpServers": {} }', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(workspace=tmp_path)


def test_top_level_not_object_raises(tmp_path: Path) -> None:
    write_json(paths.global_config_path(), ["a"])
    with pytest.raises(ConfigError, match="top level"):
        load_config(workspace=tmp_path)


def test_bad_workspace_json_raises(tmp_path: Path) -> None:
    path = paths.workspace_config_path(tmp_path.resolve())
    path.parent.mkdir(parents=True)
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(workspace=tmp_path)
    assert str(path) in info.value.message


def test_utf8_bom_is_accepted(tmp_path: Path) -> None:
    path = paths.global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"a": {"command": "x"}}}).encode())
    assert "a" in load_config(workspace=tmp_path).servers


# --- settings ------------------------------------------------------------------------------------


def test_settings_aliases(tmp_path: Path) -> None:
    write_global(
        settings={
            "cacheTtlHours": 2,
            "connectTimeout": 5,
            "requestTimeout": 99,
            "daemon": {"enabled": False, "serverIdleMinutes": 3, "daemonIdleMinutes": 4},
        }
    )
    settings = load_config(workspace=tmp_path).settings
    assert settings.cache_ttl_hours == 2
    assert settings.connect_timeout == 5
    assert settings.request_timeout == 99
    assert settings.daemon.enabled is False
    assert (settings.daemon.server_idle_minutes, settings.daemon.daemon_idle_minutes) == (3, 4)


def test_settings_defaults(tmp_path: Path) -> None:
    settings = load_config(workspace=tmp_path).settings
    assert settings.cache_ttl_hours == 24
    assert settings.daemon.enabled is True


def test_invalid_settings_warn_and_default(tmp_path: Path) -> None:
    write_global(settings={"cacheTtlHours": "lots"})
    loaded = load_config(workspace=tmp_path)
    assert loaded.settings.cache_ttl_hours == 24
    assert any("settings" in w for w in loaded.warnings)


def test_workspace_settings_ignored_with_warning(tmp_path: Path) -> None:
    write_global(settings={"cacheTtlHours": 5})
    write_workspace(tmp_path.resolve(), settings={"cacheTtlHours": 1})
    loaded = load_config(workspace=tmp_path)
    assert loaded.settings.cache_ttl_hours == 5
    assert any("settings" in w and "global" in w for w in loaded.warnings)


# --- merge ---------------------------------------------------------------------------------------


def test_workspace_overrides_global(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    write_global({"a": {"command": "global-a"}, "b": {"command": "global-b"}})
    write_workspace(root, {"a": {"command": "ws-a"}})
    loaded = load_config(workspace=tmp_path)
    a, b = loaded.servers["a"], loaded.servers["b"]
    assert isinstance(a.spec, StdioServer) and a.spec.command == "ws-a"
    assert a.scope == "workspace" and a.overrides_global
    assert a.source == paths.workspace_config_path(root)
    assert b.scope == "global" and not b.overrides_global


def test_server_order_global_then_new_workspace(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    write_global({"g1": {"command": "x"}, "g2": {"command": "x"}, "g3": {"command": "x"}})
    write_workspace(root, {"w1": {"command": "x"}, "g2": {"command": "y"}, "w2": {"command": "x"}})
    assert list(load_config(workspace=tmp_path).servers) == ["g1", "g2", "g3", "w1", "w2"]


def test_disable_only_override_disables_global(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    write_global({"a": {"command": "global-a", "description": "d"}, "b": {"command": "b"}})
    write_workspace(root, {"a": {"disabled": True}})
    loaded = load_config(workspace=tmp_path)
    a = loaded.servers["a"]
    assert a.disabled
    assert a.scope == "global"  # keeps the global spec and scope
    assert not a.overrides_global
    assert isinstance(a.spec, StdioServer) and a.spec.command == "global-a"
    assert a.description == "d"
    assert not loaded.servers["b"].disabled
    assert [s.name for s in loaded.enabled()] == ["b"]
    assert loaded.warnings == []


def test_disable_only_for_unknown_server_warns(tmp_path: Path) -> None:
    write_workspace(tmp_path.resolve(), {"ghost": {"disabled": True}})
    loaded = load_config(workspace=tmp_path)
    assert "ghost" not in loaded.servers
    assert any("ghost" in w for w in loaded.warnings)


def test_disabled_false_only_entry_is_invalid(tmp_path: Path) -> None:
    write_global({"a": {"command": "x"}})
    write_workspace(tmp_path.resolve(), {"a": {"disabled": False}})
    loaded = load_config(workspace=tmp_path)
    assert not loaded.servers["a"].disabled
    assert len(loaded.warnings) == 1


def test_workspace_entry_with_disabled_and_spec_overrides(tmp_path: Path) -> None:
    write_global({"a": {"command": "g"}})
    write_workspace(tmp_path.resolve(), {"a": {"command": "w", "disabled": True}})
    a = load_config(workspace=tmp_path).servers["a"]
    assert a.disabled and a.scope == "workspace" and a.overrides_global


def test_disabled_global_entry_is_kept(tmp_path: Path) -> None:
    write_global({"a": {"command": "x", "disabled": True}})
    loaded = load_config(workspace=tmp_path)
    assert loaded.servers["a"].disabled
    assert loaded.enabled() == []


def test_explicit_config_path_overrides_global(tmp_path: Path) -> None:
    write_global({"default": {"command": "x"}})
    custom = write_json(tmp_path / "custom.json", {"mcpServers": {"custom": {"command": "y"}}})
    loaded = load_config(workspace=tmp_path, config_path=custom)
    assert list(loaded.servers) == ["custom"]
    assert loaded.global_path == custom
    assert loaded.servers["custom"].scope == "global"


def test_missing_explicit_config_path_warns(tmp_path: Path) -> None:
    loaded = load_config(workspace=tmp_path, config_path=tmp_path / "nope.json")
    assert loaded.servers == {}
    assert any("not found" in w for w in loaded.warnings)


def test_workspace_detected_from_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = (tmp_path / "proj").resolve()
    write_workspace(root, {"w": {"command": "x"}})
    nested = root / "deep"
    nested.mkdir()
    monkeypatch.chdir(nested)
    loaded = load_config()
    assert loaded.workspace_root == root
    assert "w" in loaded.servers


def test_workspace_same_file_as_global_not_double_loaded(tmp_path: Path) -> None:
    custom = write_json(
        paths.workspace_config_path(tmp_path.resolve()), {"mcpServers": {"a": {"command": "x"}}}
    )
    loaded = load_config(workspace=tmp_path, config_path=custom)
    assert loaded.servers["a"].scope == "global"
    assert not loaded.servers["a"].overrides_global
