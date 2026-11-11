"""`${...}` interpolation of server specs and redaction for display.

Plan section 3.5. Interpolation happens only right before connecting. Resolved values must never
be logged, cached, or placed in error messages.

Syntax:
    ${env:NAME}   ${NAME}                  environment variable (error when unset)
    ${env:NAME:-default}  ${NAME:-default} environment variable with a fallback
    ${workspaceFolder}                     detected (or explicit) workspace root
    ${userHome}                            the user's home directory
    $${                                    escape for a literal "${"
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mcpc.config.models import HttpServer, ServerConfig, Settings, StdioServer
from mcpc.errors import ConfigError
from mcpc.executor import ResolvedServer

REDACTED = "***"

# `$${` is an escape and is matched first, so the text after it is never interpolated.
_TOKEN_RE = re.compile(r"\$\$\{|\$\{([^}]*)\}")
_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PURE_TEMPLATE_RE = re.compile(r"\$\{[^{}]*\}")
_USERINFO_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/?#@]*@")

_BUILTINS = ("workspaceFolder", "userHome")


def interpolate_str(
    value: str, *, workspace_root: Path, env: Mapping[str, str], server: str | None = None
) -> str:
    """Expand every `${...}` template in `value`.

    Raises ConfigError for an unset variable without default or an unknown template form. The
    message names the variable, never its value. `server` only labels the error.
    """

    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        if body is None:  # "$${" escape
            return "${"
        return _expand(body, workspace_root, env, server)

    return _TOKEN_RE.sub(replace, value)


def resolve(
    cfg: ServerConfig,
    *,
    workspace_root: Path,
    settings: Settings,
    env: Mapping[str, str] | None = None,
) -> ResolvedServer:
    """Interpolate `cfg.spec` and attach the effective timeouts."""
    source_env: Mapping[str, str] = os.environ if env is None else env

    def sub(value: str) -> str:
        return interpolate_str(value, workspace_root=workspace_root, env=source_env, server=cfg.name)

    spec = cfg.spec
    resolved: StdioServer | HttpServer
    if isinstance(spec, StdioServer):
        resolved = spec.model_copy(
            update={
                "command": sub(spec.command),
                "args": [sub(a) for a in spec.args],
                "env": {k: sub(v) for k, v in spec.env.items()},
                "cwd": sub(spec.cwd) if spec.cwd is not None else None,
            }
        )
    else:
        resolved = spec.model_copy(
            update={"url": sub(spec.url), "headers": {k: sub(v) for k, v in spec.headers.items()}}
        )
    return ResolvedServer(
        config=cfg,
        spec=resolved,
        connect_timeout=settings.connect_timeout,
        request_timeout=cfg.timeout or settings.request_timeout,
    )


def redact_spec(spec: StdioServer | HttpServer) -> dict[str, Any]:
    """Display form of a (raw, templated) spec.

    Header and env values become "***" unless the whole value is a single `${...}` template, which
    is shown as written. Credentials in the URL (`user:pass@`) become `***@`. Args are unchanged.
    """
    data = spec.model_dump(mode="json")
    if isinstance(spec, StdioServer):
        data["env"] = {k: _redact_value(v) for k, v in spec.env.items()}
    else:
        data["headers"] = {k: _redact_value(v) for k, v in spec.headers.items()}
        data["url"] = _USERINFO_RE.sub(rf"\g<1>{REDACTED}@", spec.url)
    return data


def find_references(value: str) -> list[str]:
    """Bodies of the `${...}` templates in `value` (escaped `$${` excluded), e.g. `env:TOKEN`."""
    return [m.group(1) for m in _TOKEN_RE.finditer(value) if m.group(1) is not None]


def is_builtin_reference(body: str) -> bool:
    """True for `workspaceFolder` / `userHome`, which carry no secret."""
    return body in _BUILTINS


def _redact_value(value: str) -> str:
    return value if _PURE_TEMPLATE_RE.fullmatch(value) else REDACTED


def _expand(body: str, workspace_root: Path, env: Mapping[str, str], server: str | None) -> str:
    if body == "workspaceFolder":
        return str(workspace_root)
    if body == "userHome":
        return str(Path.home())
    name, default = _split_variable(body, server)
    if name in env:
        return env[name]
    if default is not None:
        return default
    raise ConfigError(f"environment variable {name} is not set", server=server)


def _split_variable(body: str, server: str | None) -> tuple[str, str | None]:
    """Parse `[env:]NAME[:-default]` into (name, default)."""
    rest = body[4:] if body.startswith("env:") else body
    name, sep, default = rest.partition(":-")
    if not _VAR_NAME_RE.fullmatch(name):
        raise ConfigError(f"unsupported template '${{{body}}}'", server=server)
    return name, (default if sep else None)
