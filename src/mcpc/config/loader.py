"""Config loading: workspace detection, parsing of mcp.json files, global/workspace merge.

Plan sections 3.1, 3.3 and 3.4. A single bad server entry never raises; it becomes a warning
in `LoadedConfig.warnings`. Only an unreadable file, invalid JSON, or a non-object top level
raises `ConfigError`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mcpc import paths
from mcpc.config.models import (
    SERVER_NAME_RE,
    HttpServer,
    LoadedConfig,
    Scope,
    ServerConfig,
    Settings,
    StdioServer,
)
from mcpc.errors import ConfigError

# Accepted values of "transport" / "type" mapped to a canonical form.
_TRANSPORT_ALIASES = {
    "stdio": "stdio",
    "sse": "sse",
    "http": "streamable-http",
    "streamable-http": "streamable-http",
}


class _EntryError(Exception):
    """A single server entry is invalid. The message becomes a warning."""


def find_workspace_root(start: Path | None = None) -> Path:
    """Find the workspace root for `start` (default: the current directory).

    Walk up to the first directory containing a `.mcpc/` directory; failing that, the first
    directory containing `.git`; failing that, `start` itself. The global mcpc home directory
    never counts as a workspace marker.
    """
    origin = (start if start is not None else Path.cwd()).resolve()
    if origin.is_file():
        origin = origin.parent
    chain = [origin, *origin.parents]
    for directory in chain:
        marker = directory / paths.WORKSPACE_DIR_NAME
        if marker.is_dir() and not _is_mcpc_home(marker):
            return directory
    for directory in chain:
        if (directory / ".git").exists():
            return directory
    return origin


def load_config(
    workspace: Path | None = None,
    config_path: Path | None = None,
    agent_mode: str | None = None,
) -> LoadedConfig:
    """Load and merge the global and workspace config files, plus optional coding agent servers.

    `workspace` is an explicit workspace root (`--workspace`); otherwise it is detected from the
    current directory. `config_path` replaces the global file location (`--config`).
    `agent_mode` specifies coding agent integration: "none", "all", "claude-code", "antigravity".
    """
    root = workspace.expanduser().resolve() if workspace is not None else find_workspace_root()
    global_path = config_path.expanduser() if config_path is not None else paths.global_config_path()
    workspace_path = paths.workspace_config_path(root)
    warnings: list[str] = []

    if config_path is not None and not global_path.is_file():
        warnings.append(f"{global_path}: config file not found")

    global_data = _read_object(global_path)
    global_entries = _parse_servers(global_data, global_path, "global", warnings)
    settings = _parse_settings(global_data, global_path, warnings)

    workspace_entries: dict[str, ServerConfig | None] = {}
    if not _same_path(workspace_path, global_path):
        workspace_data = _read_object(workspace_path)
        workspace_entries = _parse_servers(workspace_data, workspace_path, "workspace", warnings)
        if "settings" in workspace_data:
            warnings.append(f"{workspace_path}: 'settings' is only read from the global file; ignored")

    servers = _merge(global_entries, workspace_entries, workspace_path, warnings)

    active_agent_mode = (
        agent_mode if agent_mode is not None and agent_mode != "none" else settings.agent_mode
    )
    if active_agent_mode and active_agent_mode != "none":
        from mcpc.agents.discovery import _get_server_endpoint_key, load_all_agent_servers

        agent_servers = load_all_agent_servers(active_agent_mode, root, warnings)
        existing_endpoints: dict[str, str] = {
            _get_server_endpoint_key(s): s.name for s in servers.values()
        }
        for s in agent_servers:
            endpoint_key = _get_server_endpoint_key(s)
            if endpoint_key in existing_endpoints:
                orig_name = existing_endpoints[endpoint_key]
                orig = servers[orig_name]
                merged_tags = list(dict.fromkeys(orig.tags + s.tags))
                servers[orig_name] = orig.model_copy(update={"tags": merged_tags})
                continue

            if s.name not in servers:
                servers[s.name] = s
                existing_endpoints[endpoint_key] = s.name
            else:
                agent_tag = (
                    s.tags[0].split(":")[-1] if (s.tags and s.tags[0].startswith("agent:")) else "agent"
                )
                prefixed = f"{agent_tag}-{s.name}"
                if prefixed not in servers:
                    servers[prefixed] = s.model_copy(update={"name": prefixed})
                    existing_endpoints[endpoint_key] = prefixed

    return LoadedConfig(
        workspace_root=root,
        global_path=global_path,
        workspace_path=workspace_path,
        servers=servers,
        settings=settings,
        warnings=warnings,
        agent_mode=active_agent_mode,
    )


def _merge(
    global_entries: dict[str, ServerConfig | None],
    workspace_entries: dict[str, ServerConfig | None],
    workspace_path: Path,
    warnings: list[str],
) -> dict[str, ServerConfig]:
    """Global first, then workspace. Workspace wins; a disable-only entry just disables."""
    servers: dict[str, ServerConfig] = {n: c for n, c in global_entries.items() if c is not None}
    for name, entry in workspace_entries.items():
        existing = servers.get(name)
        if entry is None:  # disable-only override
            if existing is None:
                warnings.append(
                    f"{workspace_path}: server '{name}': disables a global server that does not exist"
                )
            else:
                servers[name] = existing.model_copy(update={"disabled": True})
        elif existing is not None:
            servers[name] = entry.model_copy(update={"overrides_global": True})
        else:
            servers[name] = entry
    return servers


def _read_object(path: Path) -> dict[str, Any]:
    """Read a JSON file whose top level must be an object. A missing file gives `{}`."""
    if not path.exists():
        return {}
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read config file ({type(exc).__name__})") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")
    return data


def _parse_settings(data: dict[str, Any], path: Path, warnings: list[str]) -> Settings:
    raw = data.get("settings")
    if raw is None:
        return Settings.model_validate({})
    if not isinstance(raw, dict):
        warnings.append(f"{path}: 'settings' must be an object; using defaults")
        return Settings.model_validate({})
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        warnings.append(f"{path}: invalid 'settings' ({_describe_errors(exc)}); using defaults")
        return Settings.model_validate({})


def _parse_servers(
    data: dict[str, Any], path: Path, scope: Scope, warnings: list[str]
) -> dict[str, ServerConfig | None]:
    """Parse `mcpServers`. A `None` value marks a workspace disable-only override."""
    raw_servers = data.get("mcpServers")
    if raw_servers is None:
        return {}
    if not isinstance(raw_servers, dict):
        warnings.append(f"{path}: 'mcpServers' must be an object; ignored")
        return {}
    out: dict[str, ServerConfig | None] = {}
    for name, raw in raw_servers.items():
        try:
            if not SERVER_NAME_RE.fullmatch(name):
                raise _EntryError("invalid server name (allowed: letters, digits, '_', '.', '-')")
            if scope == "workspace" and _is_disable_only(raw):
                out[name] = None
            else:
                out[name] = _parse_entry(name, raw, path, scope)
        except _EntryError as exc:
            warnings.append(f"{path}: server '{name}': {exc}; skipped")
    return out


def _is_disable_only(raw: Any) -> bool:
    return isinstance(raw, dict) and set(raw) == {"disabled"} and raw["disabled"] is True


def _parse_entry(name: str, raw: Any, source: Path, scope: Scope) -> ServerConfig:
    if not isinstance(raw, dict):
        raise _EntryError("entry must be an object")
    has_command = raw.get("command") is not None
    has_url = raw.get("url") is not None
    if has_command == has_url:
        raise _EntryError("exactly one of 'command' or 'url' is required")
    transport = _transport_hint(raw)
    if has_command:
        if transport not in (None, "stdio"):
            raise _EntryError(f"transport '{transport}' needs 'url', not 'command'")
        spec: StdioServer | HttpServer = _build_stdio(raw)
    else:
        if transport == "stdio":
            raise _EntryError("transport 'stdio' needs 'command', not 'url'")
        spec = _build_http(raw, transport)
    return _build_config(name, raw, spec, source, scope)


def _transport_hint(raw: dict[str, Any]) -> str | None:
    """Canonical transport from `transport` (preferred) or `type`, or None when neither is set."""
    result: str | None = None
    for key in ("type", "transport"):  # "transport" is read last, so it wins
        value = raw.get(key)
        if value is None:
            continue
        canonical = _TRANSPORT_ALIASES.get(value.lower()) if isinstance(value, str) else None
        if canonical is None:
            raise _EntryError(f"unsupported '{key}' value (use stdio, sse, http or streamable-http)")
        result = canonical
    return result


def _build_stdio(raw: dict[str, Any]) -> StdioServer:
    data = _pick(raw, ("command", "args", "env", "cwd"))
    try:
        spec = StdioServer.model_validate(data)
    except ValidationError as exc:
        raise _EntryError(_describe_errors(exc)) from None
    if not spec.command.strip():
        raise _EntryError("'command' must not be empty")
    return spec


def _build_http(raw: dict[str, Any], transport: str | None) -> HttpServer:
    data = _pick(raw, ("url", "headers"))
    if transport is not None:
        data["transport"] = transport
    try:
        spec = HttpServer.model_validate(data)
    except ValidationError as exc:
        raise _EntryError(_describe_errors(exc)) from None
    if not spec.url.strip():
        raise _EntryError("'url' must not be empty")
    return spec


def _build_config(
    name: str, raw: dict[str, Any], spec: StdioServer | HttpServer, source: Path, scope: Scope
) -> ServerConfig:
    timeout = raw.get("timeout")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0
    ):
        raise _EntryError("'timeout' must be a positive number of seconds")
    if "disabled" in raw and not isinstance(raw["disabled"], bool):
        raise _EntryError("'disabled' must be true or false")
    data = _pick(raw, ("description", "tags", "timeout", "mode", "disabled"))
    try:
        return ServerConfig.model_validate(
            {"name": name, "spec": spec, "scope": scope, "source": source, **data}
        )
    except ValidationError as exc:
        raise _EntryError(_describe_errors(exc)) from None


def _pick(raw: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: raw[k] for k in keys if raw.get(k) is not None}


def _describe_errors(exc: ValidationError) -> str:
    """Summarize validation errors by location and message only (never echo input values)."""
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return "; ".join(parts)


def _is_mcpc_home(candidate: Path) -> bool:
    return _same_path(candidate, paths.mcpc_home())


def _same_path(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(a.resolve()) == os.path.normcase(b.resolve())
    except OSError:
        return False
