"""mcpc error hierarchy, exit codes, and translation of SDK/transport exceptions.

Every failure that reaches the CLI is an McpcError. `translate()` is the single place
that maps third-party exceptions to McpcError.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    TOOL_ERROR = 1  # tool returned is_error=True (not raised; CLI maps it)
    USAGE = 2  # bad args, unknown server/tool, invalid JSON, schema validation, config error
    CONNECTION = 3  # connect/transport failure, timeout
    PROTOCOL = 4  # MCPError, invalid server response, unsupported feature
    UNTRUSTED = 5  # workspace not trusted


class McpcError(Exception):
    exit_code: ExitCode = ExitCode.USAGE
    kind: str = "error"

    def __init__(self, message: str, *, server: str | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.server = server
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": int(self.exit_code), "kind": self.kind, "message": self.message}
        if self.server is not None:
            d["server"] = self.server
        if self.detail is not None:
            d["detail"] = self.detail
        return d

    def __str__(self) -> str:
        return f"{self.server}: {self.message}" if self.server else self.message


class UsageError(McpcError):
    exit_code = ExitCode.USAGE
    kind = "usage"


class ConfigError(McpcError):
    exit_code = ExitCode.USAGE
    kind = "config"


class NotFoundError(McpcError):
    """Unknown server, tool, prompt or resource."""

    exit_code = ExitCode.USAGE
    kind = "not_found"


class ArgumentValidationError(McpcError):
    """Tool arguments failed client-side JSON Schema validation."""

    exit_code = ExitCode.USAGE
    kind = "validation"


class ConnectionFailedError(McpcError):
    exit_code = ExitCode.CONNECTION
    kind = "connection"


class TimeoutFailedError(McpcError):
    exit_code = ExitCode.CONNECTION
    kind = "timeout"


class ProtocolFailedError(McpcError):
    exit_code = ExitCode.PROTOCOL
    kind = "protocol"


class UnsupportedFeatureError(McpcError):
    """Server needs something mcpc doesn't do (e.g. elicitation, sampling)."""

    exit_code = ExitCode.PROTOCOL
    kind = "unsupported"


class UntrustedWorkspaceError(McpcError):
    exit_code = ExitCode.UNTRUSTED
    kind = "untrusted"


_KIND_TO_CLASS: dict[str, type[McpcError]] = {
    c.kind: c
    for c in (
        UsageError,
        ConfigError,
        NotFoundError,
        ArgumentValidationError,
        ConnectionFailedError,
        TimeoutFailedError,
        ProtocolFailedError,
        UnsupportedFeatureError,
        UntrustedWorkspaceError,
    )
}


def from_dict(d: dict[str, Any]) -> McpcError:
    """Rebuild an McpcError from `to_dict()` output (used across the daemon IPC boundary)."""
    cls = _KIND_TO_CLASS.get(d.get("kind", ""), McpcError)
    err = cls(d.get("message", "unknown error"), server=d.get("server"), detail=d.get("detail"))
    if cls is McpcError:
        err.exit_code = ExitCode(d.get("code", int(ExitCode.PROTOCOL)))
    return err


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        out: list[BaseException] = []
        for e in exc.exceptions:
            out.extend(_leaf_exceptions(e))
        return out
    return [exc]


def translate(exc: BaseException, server: str | None = None) -> McpcError:
    """Map any exception raised while talking to a server to an McpcError.

    Handles ExceptionGroup (anyio task groups wrap transport errors), MCPError,
    pydantic.ValidationError, httpx2 errors, OSError (bad command), TimeoutError.
    """
    if isinstance(exc, McpcError):
        if exc.server is None:
            exc.server = server
        return exc

    leaves = _leaf_exceptions(exc)
    # Prefer the most specific leaf: McpcError > MCPError > others.
    leaves.sort(key=_priority)
    leaf = leaves[0] if leaves else exc
    if leaf is not exc:
        return translate(leaf, server)

    # Imports kept local so this module stays cheap to import.
    import httpx2
    import pydantic
    from mcp import MCPError
    from mcp.shared.exceptions import UrlElicitationRequiredError

    name = type(exc).__name__
    if isinstance(exc, UrlElicitationRequiredError) or "Elicitation" in name or "InputRequired" in name:
        return UnsupportedFeatureError(
            f"server requires user input (elicitation), which mcpc does not support: {exc}", server=server
        )
    if isinstance(exc, MCPError):
        return ProtocolFailedError(
            f"MCP error {exc.code}: {exc.message}",
            server=server,
            detail={"code": exc.code, "data": _jsonable(exc.data)},
        )
    if isinstance(exc, pydantic.ValidationError):
        return ProtocolFailedError(f"server returned an invalid response: {_first_line(exc)}", server=server)
    if isinstance(exc, (TimeoutError, httpx2.TimeoutException)):
        return TimeoutFailedError(f"timed out ({name})", server=server)
    if isinstance(exc, httpx2.HTTPStatusError):
        return ConnectionFailedError(f"HTTP {exc.response.status_code} from {exc.request.url}", server=server)
    if isinstance(exc, httpx2.HTTPError):
        return ConnectionFailedError(f"HTTP transport error: {name}: {exc}", server=server)
    if isinstance(exc, FileNotFoundError):
        return ConnectionFailedError(f"command not found: {exc}", server=server)
    if isinstance(exc, (OSError, EOFError, ConnectionError)):
        return ConnectionFailedError(f"{name}: {exc}", server=server)
    try:
        import anyio

        if isinstance(exc, (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)):
            return ConnectionFailedError(f"connection closed by server ({name})", server=server)
    except ImportError:  # pragma: no cover
        pass
    return ProtocolFailedError(f"{name}: {exc}", server=server)


def _priority(e: BaseException) -> int:
    from mcp import MCPError

    if isinstance(e, McpcError):
        return 0
    if isinstance(e, MCPError):
        return 1
    if isinstance(e, (KeyboardInterrupt, SystemExit)):
        return 9
    return 2


def _first_line(exc: BaseException) -> str:
    s = str(exc).strip().splitlines()
    return " ".join(s[:2]) if s else type(exc).__name__


def _jsonable(v: Any) -> Any:
    import json

    try:
        json.dumps(v)
        return v
    except TypeError:
        return repr(v)
