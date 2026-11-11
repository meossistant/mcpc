"""`mcpc daemon ...` through the Typer app, against a real daemon subprocess."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

from mcpc.cli.app import app
from mcpc.config.models import ServerConfig, StdioServer
from mcpc.daemon import client as dclient
from mcpc.daemon.client import connect_or_start, read_state, stop_daemon
from mcpc.executor import ResolvedServer

pytestmark = pytest.mark.slow

runner = CliRunner()


def _kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


@pytest.fixture(autouse=True)
def _daemon_teardown() -> Iterator[None]:
    """Stop any daemon the test started; kill it as a last resort."""
    yield
    state = read_state()
    with contextlib.suppress(Exception):
        anyio.run(stop_daemon)
    if state is not None and dclient._pid_alive(state.pid):
        _kill(state.pid)


@pytest.fixture(autouse=True)
def _config(write_config) -> None:
    """Defaults are fine; this makes sure ~/.mcpc/mcp.json exists under the temp MCPC_HOME."""
    write_config(global_servers={})


def test_status_when_not_running() -> None:
    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 0
    assert result.output.strip() == "daemon not running"

    result = runner.invoke(app, ["--json", "daemon", "status"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"running": False}


def test_stop_when_not_running_is_not_an_error() -> None:
    result = runner.invoke(app, ["daemon", "stop"])
    assert result.exit_code == 0
    assert result.output.strip() == "daemon not running"


def test_start_status_stop_cycle() -> None:
    started = runner.invoke(app, ["daemon", "start"])
    assert started.exit_code == 0, started.output
    assert started.output.startswith("daemon started (pid ")
    state = read_state()
    assert state is not None
    assert started.output.strip() == f"daemon started (pid {state.pid})"

    again = runner.invoke(app, ["daemon", "start"])
    assert again.exit_code == 0
    assert again.output.strip() == f"daemon already running (pid {state.pid}, port {state.port})"

    status = runner.invoke(app, ["daemon", "status"])
    assert status.exit_code == 0
    assert status.output.startswith(f"daemon running (pid {state.pid}, version ")
    assert "servers:" in status.output

    as_json = runner.invoke(app, ["--json", "daemon", "status"])
    assert as_json.exit_code == 0
    payload = json.loads(as_json.output)
    assert payload["running"] is True and payload["pid"] == state.pid
    assert payload["servers"] == []
    assert {"version", "started_at", "uptime_s"} <= payload.keys()

    stopped = runner.invoke(app, ["daemon", "stop"])
    assert stopped.exit_code == 0
    assert stopped.output.strip() == "daemon stopped"
    assert read_state() is None
    assert runner.invoke(app, ["daemon", "stop"]).output.strip() == "daemon not running"


def test_restart_gives_a_new_process() -> None:
    assert runner.invoke(app, ["daemon", "start"]).exit_code == 0
    old = read_state()
    assert old is not None

    result = runner.invoke(app, ["daemon", "restart"])
    assert result.exit_code == 0, result.output
    new = read_state()
    assert new is not None and new.pid != old.pid
    assert result.output.strip() == f"daemon restarted (pid {new.pid})"
    assert not dclient._pid_alive(old.pid)


def test_restart_when_not_running_just_starts() -> None:
    result = runner.invoke(app, ["daemon", "restart"])
    assert result.exit_code == 0, result.output
    assert read_state() is not None


def test_status_lists_live_servers(counter_stdio_cmd: list[str]) -> None:
    spec = StdioServer(command=counter_stdio_cmd[0], args=counter_stdio_cmd[1:])
    cfg = ServerConfig(name="counter", spec=spec, scope="global", source=Path("x"))
    server = ResolvedServer(config=cfg, spec=spec)

    async def touch() -> None:
        from mcpc.config.models import Settings

        executor = await connect_or_start(Settings())
        assert executor is not None
        await executor.call_tool(server, "bump", {})

    anyio.run(touch)

    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].startswith("daemon running (pid ")
    assert lines[1] == "servers:"
    assert lines[2].split()[:2] == ["counter", "stdio"]
    assert "idle " in lines[2]

    payload = json.loads(runner.invoke(app, ["--json", "daemon", "status"]).output)
    (entry,) = payload["servers"]
    assert entry["name"] == "counter" and entry["transport"] == "stdio"
    assert len(entry["key"]) == 12
    assert entry["in_flight"] == 0 and entry["protocol_version"]
