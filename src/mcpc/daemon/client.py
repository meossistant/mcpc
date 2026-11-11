"""CLI side of the daemon: find or start it, send requests, and expose it as an `Executor`."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Any, TypeVar

import anyio
import filelock
from mcp_types import CallToolResult, GetPromptResult, ReadResourceResult
from pydantic import BaseModel, ValidationError

import mcpc
from mcpc import paths
from mcpc.catalog.models import ServerSnapshot
from mcpc.config.models import Settings, StdioServer
from mcpc.daemon.protocol import VERSION_MISMATCH_MESSAGE, Request, Response, StateFile, read_line, write_line
from mcpc.errors import ConnectionFailedError, McpcError, ProtocolFailedError, from_dict
from mcpc.executor import ResolvedServer

__all__ = [
    "DaemonExecutor",
    "DaemonUnavailable",
    "connect_or_start",
    "daemon_status",
    "ensure_daemon",
    "read_state",
    "running_daemon",
    "start_daemon_process",
    "stop_daemon",
]

log = logging.getLogger(__name__)

PING_TIMEOUT = 1.0
START_TIMEOUT = 3.0
LOCK_TIMEOUT = 10.0
POLL_INTERVAL = 0.05
CONTROL_TIMEOUT = 10.0
_M = TypeVar("_M", bound=BaseModel)


class DaemonUnavailable(Exception):
    """The daemon cannot be reached or started. The message is a short, secret-free reason."""


class _NotConnected(Exception):
    """TCP connect to the daemon failed; nothing was sent, so the request is safe to retry."""


# ---- low level ----------------------------------------------------------------------------------
def read_state() -> StateFile | None:
    """Parse the daemon state file; None if it is missing or unreadable."""
    try:
        return StateFile.model_validate_json(paths.daemon_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _roundtrip(state: StateFile, request: Request, timeout: float) -> Response:
    """Send one request on a new connection and read the one response."""
    try:
        with anyio.fail_after(timeout):
            try:
                stream = await anyio.connect_tcp("127.0.0.1", state.port)
            except OSError as exc:
                raise _NotConnected(type(exc).__name__) from exc
            async with stream:
                await write_line(stream, request)
                line = await read_line(stream)
        return Response.model_validate_json(line)
    except TimeoutError:
        raise ConnectionFailedError("timed out waiting for the daemon") from None
    except anyio.EndOfStream:
        raise ConnectionFailedError("daemon closed the connection without a response") from None
    except anyio.DelimiterNotFound:
        raise ConnectionFailedError("daemon response is too large") from None
    except ValidationError:
        raise ProtocolFailedError("daemon sent an invalid response") from None
    except (OSError, anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
        raise ConnectionFailedError(f"daemon connection failed ({type(exc).__name__})") from exc


def _request(state: StateFile, method: str, **fields: Any) -> Request:
    return Request(token=state.token, version=mcpc.__version__, method=method, **fields)  # type: ignore[arg-type]


async def _control(state: StateFile, method: str, timeout: float) -> Any:
    """Send a server-less request (ping/status/stop) and return its result. Raises on any failure."""
    try:
        response = await _roundtrip(state, _request(state, method), timeout)
    except _NotConnected as exc:
        raise ConnectionFailedError(f"cannot connect to the daemon ({exc})") from exc
    if not response.ok:
        raise from_dict(response.error or {})
    return response.result


async def _ping(state: StateFile) -> dict[str, Any] | None:
    """The daemon's `{version, pid}`, or None when nothing (valid) answers within PING_TIMEOUT."""
    try:
        result = await _control(state, "ping", PING_TIMEOUT)
    except McpcError:
        return None
    return result if isinstance(result, dict) else None


async def _find_running() -> tuple[StateFile, dict[str, Any]] | None:
    state = read_state()
    if state is None:
        return None
    pong = await _ping(state)
    return None if pong is None else (state, pong)


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied means it exists
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:  # our own child that already exited
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _stop(state: StateFile, timeout: float) -> bool:
    """Ask the daemon to stop and wait until its process is gone. True if it is gone."""
    try:
        await _control(state, "stop", min(timeout, CONTROL_TIMEOUT))
    except McpcError:
        return False
    with anyio.move_on_after(timeout):
        while _pid_alive(state.pid):
            await anyio.sleep(POLL_INTERVAL)
        return True
    return False


# ---- starting -----------------------------------------------------------------------------------
def _spawn(settings: Settings) -> subprocess.Popen[Any]:
    cmd = [
        sys.executable,
        "-m",
        "mcpc.daemon",
        "--server-idle-minutes",
        str(settings.daemon.server_idle_minutes),
        "--daemon-idle-minutes",
        str(settings.daemon.daemon_idle_minutes),
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        # Don't pin the caller's working directory (it would block deleting it on Windows).
        "cwd": str(paths.mcpc_home()),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def start_daemon_process(settings: Settings) -> int:
    """Spawn a detached daemon process and return its pid (without waiting for it to be ready).

    Most callers want `ensure_daemon`, which takes the start lock and waits for readiness.
    """
    return _spawn(settings).pid


async def _wait_for_daemon(proc: subprocess.Popen[Any], previous_token: str | None) -> StateFile:
    deadline = anyio.current_time() + START_TIMEOUT
    while True:
        state = read_state()
        if state is not None and state.token != previous_token and await _ping(state) is not None:
            return state
        if (code := proc.poll()) is not None:
            raise DaemonUnavailable(f"daemon process exited with code {code}")
        if anyio.current_time() >= deadline:
            raise DaemonUnavailable(f"daemon did not start within {START_TIMEOUT:g}s")
        await anyio.sleep(POLL_INTERVAL)


async def _start_locked(settings: Settings) -> StateFile:
    lock = filelock.FileLock(str(paths.daemon_lock_file()), timeout=LOCK_TIMEOUT, thread_local=False)
    try:
        await anyio.to_thread.run_sync(lock.acquire)
    except filelock.Timeout:
        raise DaemonUnavailable("timed out waiting for another mcpc to start the daemon") from None
    try:
        # Another CLI may have started it while we waited for the lock.
        found = await _find_running()
        if found is not None:
            if found[1].get("version") == mcpc.__version__:
                return found[0]
            await _stop(found[0], timeout=5)
        previous = read_state()
        try:
            proc = _spawn(settings)
        except OSError as exc:
            raise DaemonUnavailable(f"cannot start daemon: {type(exc).__name__}: {exc}") from exc
        return await _wait_for_daemon(proc, previous.token if previous else None)
    finally:
        lock.release()


async def ensure_daemon(settings: Settings) -> StateFile:
    """Return the state of a running daemon of this mcpc version, starting (or restarting) it if needed.

    Raises DaemonUnavailable.
    """
    found = await _find_running()
    if found is not None and found[1].get("version") == mcpc.__version__:
        return found[0]
    return await _start_locked(settings)


def _short_reason(exc: BaseException) -> str:
    text = str(exc) if isinstance(exc, DaemonUnavailable) else f"{type(exc).__name__}: {exc}"
    first = " ".join(text.split())
    return first if len(first) <= 160 else first[:159] + "Ã¢â‚¬Â¦"


async def connect_or_start(settings: Settings) -> DaemonExecutor | None:
    """Connect to the daemon, starting it if necessary. Never raises.

    On any failure prints one warning line on stderr and returns None (caller falls back to direct mode).
    """
    try:
        state = await ensure_daemon(settings)
    except Exception as exc:
        log.debug("daemon unavailable", exc_info=True)
        sys.stderr.write(f"mcpc: warning: daemon unavailable, connecting directly ({_short_reason(exc)})\n")
        return None
    return DaemonExecutor(state, settings)


# ---- control commands -----------------------------------------------------------------------------
async def running_daemon() -> StateFile | None:
    """State of the daemon if one answers a ping (any version), else None."""
    found = await _find_running()
    return None if found is None else found[0]


async def daemon_status() -> dict[str, Any] | None:
    """The daemon's status payload, or None if it is not running."""
    found = await _find_running()
    if found is None:
        return None
    try:
        result = await _control(found[0], "status", CONTROL_TIMEOUT)
    except McpcError:
        return None
    return result if isinstance(result, dict) else None


async def stop_daemon(timeout: float = 5) -> bool:
    """Stop the running daemon and wait for it to exit. False if none was running.

    Raises ConnectionFailedError if the daemon accepted the request but is still alive after `timeout`.
    """
    found = await _find_running()
    if found is None:
        return False
    if not await _stop(found[0], timeout):
        raise ConnectionFailedError(f"daemon (pid {found[0].pid}) did not stop within {timeout:g}s")
    return True


# ---- executor -----------------------------------------------------------------------------------
class DaemonExecutor:
    """Implements `mcpc.executor.Executor` by forwarding to the daemon over loopback TCP."""

    def __init__(self, state: StateFile, settings: Settings) -> None:
        self._state = state
        self._settings = settings

    async def snapshot(self, server: ResolvedServer, prior: ServerSnapshot | None = None) -> ServerSnapshot:
        """Catalog snapshot taken on the daemon's (possibly already open) session."""
        result = await self._call("snapshot", server, prior, {})
        return _rebuild(ServerSnapshot, result, server)

    async def call_tool(
        self,
        server: ResolvedServer,
        tool: str,
        arguments: dict[str, Any],
        prior: ServerSnapshot | None = None,
    ) -> CallToolResult:
        """Call a tool. A result with `is_error=True` is returned, not raised."""
        result = await self._call("call_tool", server, prior, {"tool": tool, "arguments": arguments})
        return _rebuild(CallToolResult, result, server)

    async def read_resource(
        self, server: ResolvedServer, uri: str, prior: ServerSnapshot | None = None
    ) -> ReadResourceResult:
        """Read one resource by URI."""
        result = await self._call("read_resource", server, prior, {"uri": uri})
        return _rebuild(ReadResourceResult, result, server)

    async def get_prompt(
        self,
        server: ResolvedServer,
        name: str,
        arguments: dict[str, str],
        prior: ServerSnapshot | None = None,
    ) -> GetPromptResult:
        """Render a prompt."""
        result = await self._call("get_prompt", server, prior, {"name": name, "arguments": arguments})
        return _rebuild(GetPromptResult, result, server)

    async def aclose(self) -> None:
        """Connections are per request; nothing to close."""

    async def _call(
        self, method: str, server: ResolvedServer, prior: ServerSnapshot | None, params: dict[str, Any]
    ) -> Any:
        """Send one request. Restarts the daemon and retries once if it is gone or on another version."""
        server = _with_caller_cwd(server)
        timeout = server.connect_timeout + 2 * server.request_timeout + 10
        for attempt in range(2):
            request = _request(self._state, method, server=server, prior=prior, params=params)
            try:
                response = await _roundtrip(self._state, request, timeout)
            except _NotConnected as exc:
                if attempt:
                    raise ConnectionFailedError(f"cannot connect to the daemon ({exc})") from exc
                await self._restart()
                continue
            if response.ok:
                return response.result
            err = from_dict(response.error or {})
            if attempt == 0 and err.kind == "protocol" and err.message == VERSION_MISMATCH_MESSAGE:
                await self._restart()
                continue
            raise err
        raise AssertionError("unreachable")  # pragma: no cover

    async def _restart(self) -> None:
        try:
            self._state = await ensure_daemon(self._settings)
        except DaemonUnavailable as exc:
            raise ConnectionFailedError(f"daemon unavailable ({exc})") from exc


def _with_caller_cwd(server: ResolvedServer) -> ResolvedServer:
    """Give a stdio server without cwd the caller's working directory, as direct mode would use.

    The daemon runs elsewhere, so relative commands or paths would otherwise resolve differently.
    """
    spec = server.spec
    if not isinstance(spec, StdioServer) or spec.cwd is not None:
        return server
    try:
        cwd = os.getcwd()
    except OSError:
        return server
    return server.model_copy(update={"spec": spec.model_copy(update={"cwd": cwd})})


def _rebuild(model: type[_M], payload: Any, server: ResolvedServer) -> _M:
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise ProtocolFailedError("daemon returned an invalid result", server=server.name) from None
