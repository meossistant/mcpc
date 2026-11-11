"""Session pool: one long-lived worker task per resolved server.

A `Client` must be entered and exited in the same task, so each pooled server has a
`ServerWorker` task that runs `async with open_client(...)`, signals ready, and then waits for
a stop event. Request handlers use the shared `Client` from their own tasks.

Nothing here logs specs, env values, headers or tool arguments; only server names and outcomes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

import anyio
from anyio.abc import TaskGroup
from mcp import Client

from mcpc.catalog import store
from mcpc.catalog.models import ServerSnapshot
from mcpc.client.connector import open_client, to_mcpc_error
from mcpc.client.operations import take_snapshot
from mcpc.config.models import HttpServer
from mcpc.errors import ConnectionFailedError, ExitCode, McpcError, from_dict
from mcpc.executor import ResolvedServer

__all__ = ["MAX_CONCURRENCY", "Pool", "ServerWorker"]

log = logging.getLogger(__name__)

MAX_CONCURRENCY = 4
_STOP_WAIT_SECONDS = 10
_T = TypeVar("_T")


class ServerWorker:
    """Owns one connected `Client` for one resolved server (pool key)."""

    def __init__(
        self,
        server: ResolvedServer,
        prior: ServerSnapshot | None,
        on_exit: Callable[[ServerWorker], None],
    ) -> None:
        self.server = server
        self.key = server.pool_key()
        self._prior = prior
        self._on_exit = on_exit
        self.ready = anyio.Event()
        self.stop = anyio.Event()
        self.done = anyio.Event()
        self.limiter = anyio.CapacityLimiter(MAX_CONCURRENCY)
        self.client: Client | None = None
        self.error: McpcError | None = None
        self.retired = False
        self.in_flight = 0
        self.last_used = time.monotonic()
        self.connected_at: datetime | None = None
        self.protocol_version: str | None = None

    # ---- worker task --------------------------------------------------------------------------
    async def run(self) -> None:
        """Task body: connect, serve until told to stop, disconnect. Never raises."""
        name = self.server.name
        try:
            async with open_client(self.server, self._prior) as client:
                self.client = client
                self.protocol_version = client.protocol_version
                self.connected_at = datetime.now(UTC)
                self.last_used = time.monotonic()
                log.info("connected server=%s key=%s", name, self.key[:12])
                self.ready.set()
                await self._serve()
        except Exception as exc:
            err = to_mcpc_error(exc, name)
            if self.ready.is_set():
                log.warning("session ended server=%s kind=%s", name, err.kind)
            else:
                log.warning("connect failed server=%s kind=%s", name, err.kind)
                self.error = err
        finally:
            self.retired = True
            self.client = None
            self.ready.set()
            self._on_exit(self)
            log.info("closed server=%s key=%s", name, self.key[:12])
            self.done.set()

    async def _serve(self) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._save_snapshot)
            await self.stop.wait()
            tg.cancel_scope.cancel()

    async def _save_snapshot(self) -> None:
        """Refresh the on-disk catalog from this fresh session. Failures are logged, not fatal."""
        client = self.client
        if client is None:
            return
        try:
            async with self.limiter:
                with anyio.fail_after(self.server.request_timeout):
                    snap = await take_snapshot(client, self.server)
            store.save(snap, self.server.config)
            log.info("snapshot saved server=%s tools=%d", self.server.name, len(snap.tools))
        except Exception as exc:
            log.warning("snapshot failed server=%s kind=%s", self.server.name, type(exc).__name__)

    # ---- requests -----------------------------------------------------------------------------
    async def execute(self, request: Callable[[Client], Awaitable[_T]]) -> _T:
        """Run `request` on the shared client under the concurrency limit and request timeout."""
        client = self.client
        if client is None:
            raise ConnectionFailedError("session is closed", server=self.server.name)
        self.in_flight += 1
        try:
            async with self.limiter:
                with anyio.fail_after(self.server.request_timeout):
                    return await request(client)
        except McpcError:
            raise
        except Exception as exc:
            raise to_mcpc_error(exc, self.server.name) from exc
        finally:
            self.in_flight -= 1
            self.last_used = time.monotonic()
            if self.retired and self.in_flight == 0:
                self.stop.set()

    def retire(self) -> None:
        """Stop using this session; it closes as soon as its in-flight requests finish."""
        self.retired = True
        if self.in_flight == 0:
            self.stop.set()

    def status(self) -> dict[str, Any]:
        spec = self.server.spec
        transport = spec.transport if isinstance(spec, HttpServer) else "stdio"
        idle = 0.0 if self.in_flight else time.monotonic() - self.last_used
        return {
            "name": self.server.name,
            "key": self.key[:12],
            "transport": transport,
            "protocol_version": self.protocol_version,
            "idle_s": round(idle, 1),
            "in_flight": self.in_flight,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
        }


class Pool:
    """Connected servers keyed by `ResolvedServer.pool_key()`."""

    def __init__(self, server_idle_minutes: float, tg: TaskGroup) -> None:
        self._idle_seconds = server_idle_minutes * 60
        self._tg = tg
        self._workers: dict[str, ServerWorker] = {}
        self._closing = False

    async def run(
        self,
        server: ResolvedServer,
        prior: ServerSnapshot | None,
        request: Callable[[Client], Awaitable[_T]],
        *,
        idempotent: bool,
    ) -> _T:
        """Run `request` on the (possibly new) session for `server`.

        A failed request on a session that was already established before this call is retried once
        on a fresh session when `idempotent`. Raises McpcError only.
        """
        for attempt in range(2):
            worker, reused = await self._acquire(server, prior)
            try:
                return await worker.execute(request)
            except McpcError as err:
                if err.exit_code == ExitCode.CONNECTION:
                    self._retire(worker)
                if attempt == 0 and idempotent and reused and isinstance(err, ConnectionFailedError):
                    log.info("retrying on a fresh session server=%s", server.name)
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def reap_idle(self) -> None:
        """Close sessions that have had no requests for `server_idle_minutes`."""
        now = time.monotonic()
        for worker in list(self._workers.values()):
            if not worker.ready.is_set() or worker.client is None or worker.in_flight:
                continue
            if now - worker.last_used > self._idle_seconds:
                log.info("closing idle session server=%s", worker.server.name)
                self._retire(worker)

    async def aclose(self) -> None:
        """Close every session and wait (bounded) for the worker tasks to finish."""
        self._closing = True
        workers = list(self._workers.values())
        for worker in workers:
            worker.stop.set()
        with anyio.move_on_after(_STOP_WAIT_SECONDS):
            for worker in workers:
                await worker.done.wait()
        self._workers.clear()

    def status(self) -> list[dict[str, Any]]:
        """One entry per connected server."""
        return [w.status() for w in self._workers.values() if w.client is not None and not w.retired]

    def live_count(self) -> int:
        """Number of sessions that exist (connected or connecting)."""
        return len(self._workers)

    def in_flight(self) -> int:
        return sum(w.in_flight for w in self._workers.values())

    # ---- internals ----------------------------------------------------------------------------
    async def _acquire(
        self, server: ResolvedServer, prior: ServerSnapshot | None
    ) -> tuple[ServerWorker, bool]:
        """Return a ready worker and whether it was already established when we asked."""
        if self._closing:
            raise ConnectionFailedError("daemon is shutting down", server=server.name)
        key = server.pool_key()
        worker = self._workers.get(key)
        if worker is None or worker.retired:
            worker = ServerWorker(server, prior, self._worker_exited)
            self._workers[key] = worker
            self._tg.start_soon(worker.run)
        reused = worker.ready.is_set()
        await worker.ready.wait()
        if worker.error is not None:
            raise from_dict(worker.error.to_dict())
        return worker, reused

    def _retire(self, worker: ServerWorker) -> None:
        if self._workers.get(worker.key) is worker:
            del self._workers[worker.key]
        worker.retire()

    def _worker_exited(self, worker: ServerWorker) -> None:
        if self._workers.get(worker.key) is worker:
            del self._workers[worker.key]
