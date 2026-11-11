"""The daemon process: loopback TCP listener, token auth and request dispatch.

Started with `python -m mcpc.daemon`. It never reads config files; the CLI sends fully resolved
server specs. Logging goes to `logs/daemon.log` and never includes specs, env, headers, tokens or
tool arguments, only server names, methods, durations and outcomes.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import os
import secrets
import signal
import sys
import tempfile
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from anyio.abc import SocketAttribute, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import ValidationError

import mcpc
from mcpc import paths
from mcpc.client.operations import take_snapshot
from mcpc.daemon.pool import Pool
from mcpc.daemon.protocol import (
    VERSION_MISMATCH_MESSAGE,
    Header,
    Request,
    Response,
    StateFile,
    read_line,
    write_line,
)
from mcpc.errors import McpcError, ProtocolFailedError, UsageError, translate
from mcpc.executor import ResolvedServer

__all__ = ["Daemon", "remove_state_file", "serve", "write_state_file"]

log = logging.getLogger(__name__)

REQUEST_LINE_TIMEOUT = 5.0
DRAIN_TIMEOUT = 30.0
_VERSION_EXEMPT = frozenset({"ping", "status", "stop"})


# ---- state file ---------------------------------------------------------------------------------
def write_state_file(state: StateFile) -> None:
    """Atomically write the daemon state file (mode 0600 on POSIX)."""
    target = paths.daemon_state_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".daemon-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(state.model_dump_json())
        if sys.platform != "win32":
            tmp.chmod(0o600)
        _replace_with_retry(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _replace_with_retry(src: Path, dst: Path, attempts: int = 20) -> None:
    """`os.replace`, retrying on Windows PermissionError while a reader has the target open."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def remove_state_file(pid: int) -> None:
    """Delete the state file, but only if it still belongs to process `pid`."""
    target = paths.daemon_state_file()
    try:
        current = StateFile.model_validate_json(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if current.pid == pid:
        with contextlib.suppress(OSError):
            target.unlink()


# ---- daemon ---------------------------------------------------------------------------------------
class Daemon:
    """Request handling state for one daemon process."""

    def __init__(self, token: str, server_idle_minutes: float, daemon_idle_minutes: float) -> None:
        self.token = token
        self.pid = os.getpid()
        self.started_at = datetime.now(UTC)
        self.shutdown = anyio.Event()
        self.accept_done = anyio.Event()
        self.server_idle_seconds = server_idle_minutes * 60
        self.daemon_idle_seconds = daemon_idle_minutes * 60
        self.pool: Pool | None = None
        self._started_monotonic = time.monotonic()
        self._last_activity = self._started_monotonic
        self._active = 0

    # ---- connection handling ------------------------------------------------------------------
    async def handle_connection(self, stream: SocketStream) -> None:
        """Serve exactly one request on `stream`, then close it."""
        self._active += 1
        try:
            async with stream:
                await self._serve_one(stream)
        except Exception as exc:
            log.warning("connection error kind=%s", type(exc).__name__)
        finally:
            self._active -= 1
            self._last_activity = time.monotonic()

    async def _serve_one(self, stream: SocketStream) -> None:
        line = await self._read_request_line(stream)
        if line is None:
            return
        header = self._authenticate(line)
        if header is None:
            return
        self._last_activity = time.monotonic()
        if header.version != mcpc.__version__ and header.method not in _VERSION_EXEMPT:
            err = ProtocolFailedError(VERSION_MISMATCH_MESSAGE)
            log.info("request rejected method=%s outcome=version_mismatch", header.method)
            await write_line(stream, Response.failure(err))
            self.shutdown.set()
            return
        response, stop_after = await self._respond(line, header)
        await write_line(stream, response)
        if stop_after:
            self.shutdown.set()

    async def _read_request_line(self, stream: SocketStream) -> bytes | None:
        try:
            with anyio.fail_after(REQUEST_LINE_TIMEOUT):
                return await read_line(BufferedByteReceiveStream(stream))
        except (TimeoutError, anyio.EndOfStream, anyio.DelimiterNotFound):
            log.warning("incomplete or oversized request line; closing")
        except (OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
            log.warning("connection dropped while reading request")
        return None

    def _authenticate(self, line: bytes) -> Header | None:
        """Parse the header and check the token; None means 'close without answering'."""
        try:
            header = Header.model_validate_json(line)
        except ValidationError:
            log.warning("malformed request; closing")
            return None
        if not hmac.compare_digest(header.token.encode(), self.token.encode()):
            log.warning("request with wrong token; closing")
            return None
        return header

    async def _respond(self, line: bytes, header: Header) -> tuple[Response, bool]:
        """Run the request and build the response. Returns (response, shut_down_afterwards)."""
        start = time.perf_counter()
        server_name: str | None = None
        outcome = "ok"
        try:
            request = Request.model_validate_json(line)
            server_name = request.server.name if request.server else None
            result = await self._dispatch(request)
            response = Response.success(result)
        except ValidationError:
            outcome = "bad_request"
            response = Response.failure(UsageError("malformed request"))
        except McpcError as err:
            outcome = err.kind
            response = Response.failure(err)
        except Exception as exc:
            err = translate(exc, server_name)
            outcome = err.kind
            response = Response.failure(err)
        log.info(
            "request method=%s server=%s outcome=%s duration_ms=%d",
            header.method,
            server_name or "-",
            outcome,
            (time.perf_counter() - start) * 1000,
        )
        return response, header.method == "stop" and response.ok

    # ---- dispatch -----------------------------------------------------------------------------
    async def _dispatch(self, req: Request) -> Any:
        match req.method:
            case "ping":
                return {"version": mcpc.__version__, "pid": self.pid}
            case "status":
                return self.status()
            case "stop":
                return {"stopping": True}
            case "snapshot":
                server = _need_server(req)
                snap = await self._pool().run(
                    server, req.prior, lambda c: take_snapshot(c, server), idempotent=True
                )
                return snap.model_dump(mode="json", by_alias=True)
            case "call_tool":
                server = _need_server(req)
                tool = _need_str(req, "tool")
                arguments = req.params.get("arguments") or {}
                result = await self._pool().run(
                    server, req.prior, lambda c: c.call_tool(tool, arguments), idempotent=False
                )
                return result.model_dump(mode="json", by_alias=True)
            case "read_resource":
                server = _need_server(req)
                uri = _need_str(req, "uri")
                read = await self._pool().run(
                    server, req.prior, lambda c: c.read_resource(uri), idempotent=True
                )
                return read.model_dump(mode="json", by_alias=True)
            case "get_prompt":
                server = _need_server(req)
                name = _need_str(req, "name")
                args = req.params.get("arguments") or None
                prompt = await self._pool().run(
                    server, req.prior, lambda c: c.get_prompt(name, args), idempotent=True
                )
                return prompt.model_dump(mode="json", by_alias=True)
        raise UsageError(f"unknown method '{req.method}'")  # pragma: no cover

    def _pool(self) -> Pool:
        if self.pool is None:  # pragma: no cover - set before the listener starts
            raise ProtocolFailedError("daemon is not ready")
        return self.pool

    def status(self) -> dict[str, Any]:
        """Payload of the `status` method."""
        return {
            "version": mcpc.__version__,
            "pid": self.pid,
            "started_at": self.started_at.isoformat(),
            "uptime_s": round(time.monotonic() - self._started_monotonic, 1),
            "servers": self.pool.status() if self.pool else [],
        }

    # ---- housekeeping -------------------------------------------------------------------------
    async def maintain(self) -> None:
        """Reap idle sessions and exit when the daemon itself has been idle long enough."""
        tick = min(5.0, max(0.1, min(self.server_idle_seconds, self.daemon_idle_seconds) / 3))
        while True:
            await anyio.sleep(tick)
            pool = self._pool()
            await pool.reap_idle()
            if self._idle_exit_due(pool):
                log.info("daemon idle; shutting down")
                self.shutdown.set()
                return

    def _idle_exit_due(self, pool: Pool) -> bool:
        if pool.live_count() or self._active:
            return False
        return time.monotonic() - self._last_activity > self.daemon_idle_seconds

    async def drain(self) -> None:
        """Wait (bounded) for in-flight connections to finish."""
        with anyio.move_on_after(DRAIN_TIMEOUT):
            while self._active:
                await anyio.sleep(0.05)


async def _accept_loop(daemon: Daemon, listener: Any, tg: Any, scope: anyio.CancelScope) -> None:
    try:
        with scope:
            await listener.serve(daemon.handle_connection, tg)
    finally:
        daemon.accept_done.set()


async def _watch_signals(daemon: Daemon) -> None:  # pragma: no cover - POSIX only
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async for sig in signals:
            log.info("received signal %s", int(sig))
            daemon.shutdown.set()
            return


def _setup_logging() -> None:
    handler = logging.FileHandler(paths.logs_dir() / "daemon.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("mcpc").setLevel(logging.INFO)


async def serve(server_idle_minutes: float = 15.0, daemon_idle_minutes: float = 60.0) -> None:
    """Run the daemon until stopped, idle, or told that the CLI version changed."""
    _setup_logging()
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = int(listener.extra(SocketAttribute.local_port))
    daemon = Daemon(secrets.token_urlsafe(32), server_idle_minutes, daemon_idle_minutes)
    accept_scope = anyio.CancelScope()
    try:
        async with anyio.create_task_group() as tg:
            daemon.pool = Pool(server_idle_minutes, tg)
            tg.start_soon(_accept_loop, daemon, listener, tg, accept_scope)
            tg.start_soon(daemon.maintain)
            if sys.platform != "win32":  # pragma: no cover
                tg.start_soon(_watch_signals, daemon)
            write_state_file(
                StateFile(
                    pid=daemon.pid,
                    port=port,
                    token=daemon.token,
                    version=mcpc.__version__,
                    started_at=daemon.started_at.isoformat(),
                )
            )
            log.info("daemon started pid=%d port=%d version=%s", daemon.pid, port, mcpc.__version__)
            await daemon.shutdown.wait()
            await _shutdown(daemon, listener, accept_scope)
            tg.cancel_scope.cancel()
    except Exception as exc:
        log.error("daemon crashed %s", _describe(exc))
        raise
    finally:
        remove_state_file(daemon.pid)
        log.info("daemon exited pid=%d", daemon.pid)


async def _shutdown(daemon: Daemon, listener: Any, accept_scope: anyio.CancelScope) -> None:
    """Stop accepting, free the state file for a successor, finish in-flight work, close sessions."""
    log.info("daemon shutting down")
    accept_scope.cancel()
    with anyio.CancelScope(shield=True):
        await daemon.accept_done.wait()
        await listener.aclose()
        remove_state_file(daemon.pid)
        await daemon.drain()
        if daemon.pool is not None:
            await daemon.pool.aclose()


def _describe(exc: BaseException) -> str:
    """Exception types and call sites only: messages may contain config values, so they are left out."""
    if isinstance(exc, BaseExceptionGroup):
        return "[" + ", ".join(_describe(e) for e in exc.exceptions) + "]"
    frames = traceback.extract_tb(exc.__traceback__)
    where = " <- ".join(f"{Path(f.filename).name}:{f.lineno}" for f in reversed(frames[-3:]))
    return f"{type(exc).__name__} at {where}"


def _need_server(req: Request) -> ResolvedServer:
    if req.server is None:
        raise UsageError(f"method '{req.method}' requires a server")
    return req.server


def _need_str(req: Request, key: str) -> str:
    value = req.params.get(key)
    if not isinstance(value, str):
        raise UsageError(f"method '{req.method}' requires string parameter '{key}'")
    return value
