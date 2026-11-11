"""`mcpc daemon start|stop|status|restart`."""

from __future__ import annotations

from typing import Any

from mcpc.cli.context import Ctx
from mcpc.daemon import client as daemon_client
from mcpc.daemon.protocol import StateFile
from mcpc.errors import ConnectionFailedError


def _duration(seconds: float) -> str:
    """Short human duration: 12s, 5m, 2h 5m, 3d 4h."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def _status_text(payload: dict[str, Any]) -> str:
    head = (
        f"daemon running (pid {payload['pid']}, version {payload['version']}, "
        f"up {_duration(payload['uptime_s'])})"
    )
    servers = payload.get("servers") or []
    if not servers:
        return f"{head}\nservers: none"
    rows = [
        (s["name"], s["transport"], s.get("protocol_version") or "-", f"idle {_duration(s['idle_s'])}")
        for s in servers
    ]
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    lines = ["  " + "  ".join(r[i].ljust(widths[i]) for i in range(3)) + f"  {r[3]}" for r in rows]
    return "\n".join([head, "servers:", *lines])


async def _start(c: Ctx) -> StateFile:
    """Start the daemon (or restart it if it is another version) and return its state."""
    try:
        return await daemon_client.ensure_daemon(c.config().settings)
    except daemon_client.DaemonUnavailable as exc:
        raise ConnectionFailedError(f"could not start the daemon: {exc}") from exc


async def _start_body(c: Ctx) -> None:
    running = await daemon_client.running_daemon()
    if running is not None:
        c.emit(
            text=f"daemon already running (pid {running.pid}, port {running.port})",
            data={"running": True, "started": False, "pid": running.pid, "port": running.port},
        )
        return
    state = await _start(c)
    c.emit(
        text=f"daemon started (pid {state.pid})",
        data={"running": True, "started": True, "pid": state.pid, "port": state.port},
    )


async def _stop_body(c: Ctx) -> None:
    stopped = await daemon_client.stop_daemon()
    c.emit(text="daemon stopped" if stopped else "daemon not running", data={"stopped": stopped})


async def _status_body(c: Ctx) -> None:
    payload = await daemon_client.daemon_status()
    if payload is None:
        c.emit(text="daemon not running", data={"running": False})
        return
    c.emit(text=_status_text(payload), data={"running": True, **payload})


async def _restart_body(c: Ctx) -> None:
    await daemon_client.stop_daemon()
    state = await _start(c)
    c.emit(
        text=f"daemon restarted (pid {state.pid})",
        data={"running": True, "started": True, "pid": state.pid, "port": state.port},
    )
