"""Workspace trust (plan section 3.6, option B).

Workspace config files come from repositories. Servers defined there that could run commands or
send secrets are *untrusted* until a person approves the exact workspace config file. Approval is
stored as the sha256 of that file; any edit revokes it. The interactive prompt (and TTY check)
belongs to the CLI; this module only provides the decisions and the storage.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcpc import paths
from mcpc.config.interpolate import find_references, is_builtin_reference
from mcpc.config.models import HttpServer, LoadedConfig, ServerConfig, StdioServer
from mcpc.errors import ConfigError, UntrustedWorkspaceError

TRUST_ALL_ENV = "MCPC_TRUST_ALL"
_TRUST_FILE_VERSION = 1


def needs_trust(cfg: ServerConfig) -> bool:
    """True when `cfg` comes from a workspace file and could run a command or leak a secret.

    That is every workspace stdio server, and workspace HTTP servers whose url or header values
    reference anything other than `${workspaceFolder}` / `${userHome}`.
    """
    if cfg.scope != "workspace":
        return False
    spec = cfg.spec
    if isinstance(spec, StdioServer):
        return True
    return _http_references_env(spec)


def workspace_fingerprint(loaded: LoadedConfig) -> str | None:
    """sha256 (hex) of the workspace config file bytes; None when the file does not exist."""
    try:
        data = loaded.workspace_path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def is_trusted(loaded: LoadedConfig) -> bool:
    """True when MCPC_TRUST_ALL=1, or the stored hash for this workspace matches the file now."""
    if os.environ.get(TRUST_ALL_ENV) == "1":
        return True
    fingerprint = workspace_fingerprint(loaded)
    if fingerprint is None:
        return False
    entry = _read_workspaces().get(_workspace_key(loaded))
    return isinstance(entry, dict) and entry.get("sha256") == fingerprint


def untrusted_servers(loaded: LoadedConfig) -> list[str]:
    """Names of enabled servers that need trust while the workspace is not trusted."""
    candidates = [s.name for s in loaded.enabled() if needs_trust(s)]
    if not candidates or is_trusted(loaded):
        return []
    return candidates


def ensure_trusted(loaded: LoadedConfig, cfg: ServerConfig) -> None:
    """Raise UntrustedWorkspaceError (exit code 5) if `cfg` may not be started."""
    if needs_trust(cfg) and not is_trusted(loaded):
        raise UntrustedWorkspaceError(
            f'run "mcpc trust" in {loaded.workspace_root} to allow its MCP servers', server=cfg.name
        )


def trust_workspace(loaded: LoadedConfig) -> None:
    """Record the current workspace config hash as trusted."""
    fingerprint = workspace_fingerprint(loaded)
    if fingerprint is None:
        raise ConfigError(f"{loaded.workspace_path}: no workspace config file to trust")
    workspaces = _read_workspaces()
    workspaces[_workspace_key(loaded)] = {
        "sha256": fingerprint,
        "trusted_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    _write_workspaces(workspaces)


def untrust_workspace(loaded: LoadedConfig) -> bool:
    """Remove the trust entry for this workspace. Returns whether one existed."""
    workspaces = _read_workspaces()
    if workspaces.pop(_workspace_key(loaded), None) is None:
        return False
    _write_workspaces(workspaces)
    return True


def describe_untrusted(loaded: LoadedConfig) -> list[str]:
    """Human-readable lines (raw, uninterpolated specs) for the `mcpc trust` prompt."""
    lines: list[str] = []
    for name in untrusted_servers(loaded):
        lines.append(f"{name}: {_describe_spec(loaded.servers[name].spec)}")
    return lines


def _describe_spec(spec: StdioServer | HttpServer) -> str:
    if isinstance(spec, StdioServer):
        return " ".join([spec.command, *spec.args])
    if spec.headers:
        return f"{spec.url} (headers: {', '.join(spec.headers)})"
    return spec.url


def _http_references_env(spec: HttpServer) -> bool:
    values = [spec.url, *spec.headers.values()]
    return any(not is_builtin_reference(ref) for value in values for ref in find_references(value))


def _workspace_key(loaded: LoadedConfig) -> str:
    return os.path.normcase(str(loaded.workspace_root.resolve()))


def _read_workspaces() -> dict[str, Any]:
    """The `workspaces` mapping of trust.json. Missing or corrupt files read as empty."""
    try:
        data = json.loads(paths.trust_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    workspaces = data.get("workspaces") if isinstance(data, dict) else None
    return dict(workspaces) if isinstance(workspaces, dict) else {}


def _write_workspaces(workspaces: dict[str, Any]) -> None:
    """Atomically write trust.json (temp file in the same directory + os.replace)."""
    target = paths.trust_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": _TRUST_FILE_VERSION, "workspaces": workspaces}
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".trust-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
