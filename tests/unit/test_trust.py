"""Tests for mcpc.trust: needs_trust matrix, hash-based approval, TRUST_ALL, corrupt state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mcpc import paths
from mcpc.config.loader import load_config
from mcpc.config.models import HttpServer, LoadedConfig, ServerConfig, StdioServer
from mcpc.errors import ConfigError, ExitCode, UntrustedWorkspaceError
from mcpc.trust import (
    describe_untrusted,
    ensure_trusted,
    is_trusted,
    needs_trust,
    trust_workspace,
    untrust_workspace,
    untrusted_servers,
    workspace_fingerprint,
)


def cfg(spec: StdioServer | HttpServer, scope: str = "workspace") -> ServerConfig:
    return ServerConfig(name="srv", spec=spec, scope=scope, source=Path("mcp.json"))  # type: ignore[arg-type]


def write_workspace(root: Path, servers: dict[str, Any]) -> Path:
    path = paths.workspace_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return workspace.resolve()


def loaded_for(root: Path) -> LoadedConfig:
    return load_config(workspace=root)


# --- needs_trust ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "scope", "expected"),
    [
        (StdioServer(command="x"), "workspace", True),
        (StdioServer(command="x"), "global", False),
        (HttpServer(url="https://h/mcp"), "workspace", False),
        (HttpServer(url="https://h/mcp", headers={"A": "static"}), "workspace", False),
        (HttpServer(url="https://h/mcp", headers={"A": "${env:TOKEN}"}), "workspace", True),
        (HttpServer(url="https://h/mcp", headers={"A": "Bearer ${TOKEN}"}), "workspace", True),
        (HttpServer(url="https://${env:HOST}/mcp"), "workspace", True),
        (HttpServer(url="https://h/${NAME:-x}"), "workspace", True),
        (HttpServer(url="https://h/${workspaceFolder}"), "workspace", False),
        (HttpServer(url="https://h/${userHome}", headers={"A": "${workspaceFolder}"}), "workspace", False),
        (HttpServer(url="https://h/$${env:TOKEN}"), "workspace", False),  # escaped, literal text
        (HttpServer(url="https://h/${workspaceFolder}", headers={"A": "${env:T}"}), "workspace", True),
        (HttpServer(url="https://h/${env:TOKEN}", headers={"A": "x"}), "global", False),
        (HttpServer(url="https://h/mcp", transport="sse"), "workspace", False),
    ],
)
def test_needs_trust_matrix(spec: StdioServer | HttpServer, scope: str, expected: bool) -> None:
    assert needs_trust(cfg(spec, scope)) is expected


# --- fingerprint / is_trusted --------------------------------------------------------------------


def test_fingerprint_none_without_file(root: Path) -> None:
    assert workspace_fingerprint(loaded_for(root)) is None


def test_fingerprint_changes_with_content(root: Path) -> None:
    path = write_workspace(root, {"a": {"command": "x"}})
    first = workspace_fingerprint(loaded_for(root))
    assert first is not None and len(first) == 64
    assert workspace_fingerprint(loaded_for(root)) == first
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert workspace_fingerprint(loaded_for(root)) != first


def test_untrusted_by_default(root: Path) -> None:
    write_workspace(root, {"a": {"command": "x"}})
    loaded = loaded_for(root)
    assert not is_trusted(loaded)
    assert untrusted_servers(loaded) == ["a"]


def test_trust_then_trusted(root: Path) -> None:
    write_workspace(root, {"a": {"command": "x"}, "b": {"url": "http://h/mcp"}})
    loaded = loaded_for(root)
    trust_workspace(loaded)
    assert is_trusted(loaded_for(root))
    assert untrusted_servers(loaded_for(root)) == []
    ensure_trusted(loaded_for(root), loaded_for(root).servers["a"])  # does not raise


def test_trust_file_format(root: Path) -> None:
    write_workspace(root, {"a": {"command": "x"}})
    loaded = loaded_for(root)
    trust_workspace(loaded)
    data = json.loads(paths.trust_file().read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert len(data["workspaces"]) == 1
    entry = next(iter(data["workspaces"].values()))
    assert entry["sha256"] == workspace_fingerprint(loaded)
    assert entry["trusted_at"]
    assert not [p for p in paths.mcpc_home().iterdir() if p.name.endswith(".tmp")]


def test_editing_config_revokes_trust(root: Path) -> None:
    write_workspace(root, {"a": {"command": "x"}})
    trust_workspace(loaded_for(root))
    write_workspace(root, {"a": {"command": "x", "args": ["--evil"]}})
    loaded = loaded_for(root)
    assert not is_trusted(loaded)
    assert untrusted_servers(loaded) == ["a"]
    with pytest.raises(UntrustedWorkspaceError):
        ensure_trusted(loaded, loaded.servers["a"])


def test_retrust_after_edit(root: Path) -> None:
    write_workspace(root, {"a": {"command": "x"}})
    trust_workspace(loaded_for(root))
    write_workspace(root, {"a": {"command": "y"}})
    trust_workspace(loaded_for(root))
    assert is_trusted(loaded_for(root))
    data = json.loads(paths.trust_file().read_text(encoding="utf-8"))
    assert len(data["workspaces"]) == 1


def test_trust_is_per_workspace(tmp_path: Path) -> None:
    one, two = (tmp_path / "one").resolve(), (tmp_path / "two").resolve()
    for r in (one, two):
        r.mkdir()
        write_workspace(r, {"a": {"command": "x"}})
    trust_workspace(loaded_for(one))
    assert is_trusted(loaded_for(one))
    assert not is_trusted(loaded_for(two))


def test_trust_workspace_without_file_raises(root: Path) -> None:
    with pytest.raises(ConfigError):
        trust_workspace(loaded_for(root))


def test_ensure_trusted_error_shape(root: Path) -> None:
    write_workspace(root, {"a": {"command": "x"}})
    loaded = loaded_for(root)
    with pytest.raises(UntrustedWorkspaceError) as info:
        ensure_trusted(loaded, loaded.servers["a"])
    err = info.value
    assert err.exit_code == ExitCode.UNTRUSTED
    assert err.server == "a"
    assert err.message == f'run "mcpc trust" in {root} to allow its MCP servers'


def test_ensure_trusted_allows_safe_servers(root: Path) -> None:
    write_workspace(root, {"h": {"url": "http://h/mcp"}})
    loaded = loaded_for(root)
    ensure_trusted(loaded, loaded.servers["h"])  # no env refs: no trust needed


def test_global_servers_never_need_trust(root: Path) -> None:
    paths.global_config_path().write_text(json.dumps({"mcpServers": {"g": {"command": "x"}}}), "utf-8")
    loaded = loaded_for(root)
    assert untrusted_servers(loaded) == []
    ensure_trusted(loaded, loaded.servers["g"])


def test_disabled_servers_not_listed(root: Path) -> None:
    write_workspace(root, {"a": {"command": "x", "disabled": True}, "b": {"command": "y"}})
    assert untrusted_servers(loaded_for(root)) == ["b"]


def test_untrust(root: Path) -> None:
    write_workspace(root, {"a": {"command": "x"}})
    loaded = loaded_for(root)
    assert untrust_workspace(loaded) is False
    trust_workspace(loaded)
    assert untrust_workspace(loaded) is True
    assert not is_trusted(loaded_for(root))
    assert untrust_workspace(loaded) is False


# --- MCPC_TRUST_ALL ------------------------------------------------------------------------------


def test_trust_all_env(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_workspace(root, {"a": {"command": "x"}})
    monkeypatch.setenv("MCPC_TRUST_ALL", "1")
    loaded = loaded_for(root)
    assert is_trusted(loaded)
    assert untrusted_servers(loaded) == []
    ensure_trusted(loaded, loaded.servers["a"])


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_trust_all_only_for_exactly_one(root: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    write_workspace(root, {"a": {"command": "x"}})
    monkeypatch.setenv("MCPC_TRUST_ALL", value)
    assert not is_trusted(loaded_for(root))


# --- corrupt trust.json --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content", ["{not json", "", "[]", '"str"', '{"workspaces": []}', '{"workspaces": 3}']
)
def test_corrupt_trust_file_is_empty(root: Path, content: str) -> None:
    write_workspace(root, {"a": {"command": "x"}})
    paths.trust_file().write_text(content, encoding="utf-8")
    loaded = loaded_for(root)
    assert not is_trusted(loaded)
    assert untrusted_servers(loaded) == ["a"]
    assert untrust_workspace(loaded) is False


def test_corrupt_trust_file_is_replaced_on_trust(root: Path) -> None:
    write_workspace(root, {"a": {"command": "x"}})
    paths.trust_file().write_text("{garbage", encoding="utf-8")
    loaded = loaded_for(root)
    trust_workspace(loaded)
    assert is_trusted(loaded)
    assert json.loads(paths.trust_file().read_text(encoding="utf-8"))["version"] == 1


def test_malformed_entry_is_untrusted(root: Path) -> None:
    write_workspace(root, {"a": {"command": "x"}})
    loaded = loaded_for(root)
    trust_workspace(loaded)
    data = json.loads(paths.trust_file().read_text(encoding="utf-8"))
    data["workspaces"] = {key: "yes" for key in data["workspaces"]}
    paths.trust_file().write_text(json.dumps(data), encoding="utf-8")
    assert not is_trusted(loaded)


# --- describe_untrusted --------------------------------------------------------------------------


def test_describe_untrusted(root: Path) -> None:
    write_workspace(
        root,
        {
            "files": {"command": "npx", "args": ["-y", "pkg", "${workspaceFolder}"]},
            "bare": {"command": "tool"},
            "api": {"url": "https://h/mcp", "headers": {"X-A": "${env:A}", "X-B": "${env:B}"}},
            "api2": {"url": "https://${env:HOST}/mcp"},
            "safe": {"url": "https://h/mcp"},
        },
    )
    assert describe_untrusted(loaded_for(root)) == [
        "files: npx -y pkg ${workspaceFolder}",
        "bare: tool",
        "api: https://h/mcp (headers: X-A, X-B)",
        "api2: https://${env:HOST}/mcp",
    ]


def test_describe_untrusted_empty_when_trusted(root: Path) -> None:
    write_workspace(root, {"a": {"command": "x"}})
    trust_workspace(loaded_for(root))
    assert describe_untrusted(loaded_for(root)) == []
