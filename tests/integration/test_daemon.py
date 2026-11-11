"""Daemon integration tests: every test runs a real daemon subprocess under the temp MCPC_HOME."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.types import CallToolResult, TextContent

import mcpc
from mcpc import paths
from mcpc.client.direct import DirectExecutor
from mcpc.config.models import DaemonSettings, ServerConfig, Settings, StdioServer
from mcpc.daemon import client as dclient
from mcpc.daemon.client import (
    DaemonExecutor,
    connect_or_start,
    daemon_status,
    read_state,
    start_daemon_process,
    stop_daemon,
)
from mcpc.daemon.protocol import Request, write_line
from mcpc.errors import ExitCode, McpcError
from mcpc.executor import ResolvedServer

pytestmark = pytest.mark.slow


# ---- helpers --------------------------------------------------------------------------------------
def make_server(
    cmd: list[str], name: str = "counter", env: dict[str, str] | None = None, request_timeout: float = 30
) -> ResolvedServer:
    spec = StdioServer(command=cmd[0], args=cmd[1:], env=env or {})
    cfg = ServerConfig(name=name, spec=spec, scope="global", source=Path("x"))
    return ResolvedServer(config=cfg, spec=spec, connect_timeout=30, request_timeout=request_timeout)


def make_settings(server_idle: float = 15, daemon_idle: float = 60) -> Settings:
    return Settings(daemon=DaemonSettings(server_idle_minutes=server_idle, daemon_idle_minutes=daemon_idle))


def text_of(result: CallToolResult) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


async def wait_until(predicate: Callable[[], Any], timeout: float = 15.0, interval: float = 0.1) -> None:
    """Await until `predicate()` (sync or async) is truthy, or fail."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if hasattr(value, "__await__"):
            value = await value
        if value:
            return
        await anyio.sleep(interval)
    raise AssertionError("condition not met in time")


def pid_alive(pid: int) -> bool:
    return dclient._pid_alive(pid)


def kill_pid(pid: int) -> None:
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
    if state is not None and pid_alive(state.pid):
        kill_pid(state.pid)


# ---- reuse and crash ------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_session_is_reused_across_calls(counter_stdio_cmd: list[str]) -> None:
    server = make_server(counter_stdio_cmd)
    executor = await connect_or_start(make_settings())
    assert isinstance(executor, DaemonExecutor)

    first = await executor.call_tool(server, "bump", {})
    second = await executor.call_tool(server, "bump", {})
    assert (text_of(first), text_of(second)) == ("1", "2")
    pid1 = text_of(await executor.call_tool(server, "pid", {}))
    pid2 = text_of(await executor.call_tool(server, "pid", {}))
    assert pid1 == pid2

    direct = DirectExecutor()
    assert text_of(await direct.call_tool(server, "bump", {})) == "1"
    assert text_of(await direct.call_tool(server, "bump", {})) == "1"


@pytest.mark.anyio
async def test_snapshot_through_daemon_matches_direct_and_is_cached(counter_stdio_cmd: list[str]) -> None:
    server = make_server(counter_stdio_cmd)
    executor = await connect_or_start(make_settings())
    assert executor is not None
    snap = await executor.snapshot(server)
    direct = await DirectExecutor().snapshot(server)
    assert sorted(t.name for t in snap.tools) == sorted(t.name for t in direct.tools)
    assert {"bump", "pid", "sleep", "die"} <= {t.name for t in snap.tools}
    assert snap.protocol_version == direct.protocol_version

    # The daemon also writes the catalog cache after connecting.
    await wait_until(lambda: any(paths.cache_dir().glob("counter-*.json")))


@pytest.mark.anyio
async def test_stdio_server_without_cwd_runs_in_callers_directory(
    counter_stdio_cmd: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = Path(counter_stdio_cmd[1]).parent
    monkeypatch.chdir(fixtures)
    server = make_server([counter_stdio_cmd[0], "counter_server.py"])  # relative script path
    executor = await connect_or_start(make_settings())
    assert executor is not None
    assert text_of(await executor.call_tool(server, "bump", {})) == "1"


@pytest.mark.anyio
async def test_crashed_server_is_replaced_on_next_call(counter_stdio_cmd: list[str]) -> None:
    server = make_server(counter_stdio_cmd)
    executor = await connect_or_start(make_settings())
    assert executor is not None
    assert text_of(await executor.call_tool(server, "bump", {})) == "1"
    assert text_of(await executor.call_tool(server, "bump", {})) == "2"

    with pytest.raises(McpcError) as info:
        await executor.call_tool(server, "die", {})
    assert info.value.exit_code == ExitCode.CONNECTION

    assert text_of(await executor.call_tool(server, "bump", {})) == "1"


@pytest.mark.anyio
async def test_idempotent_request_retries_after_server_died_while_idle(counter_stdio_cmd: list[str]) -> None:
    server = make_server(counter_stdio_cmd)
    executor = await connect_or_start(make_settings())
    assert executor is not None
    server_pid = int(text_of(await executor.call_tool(server, "pid", {})))
    kill_pid(server_pid)
    await wait_until(lambda: not pid_alive(server_pid))

    snap = await executor.snapshot(server)  # idempotent: retried on a fresh session
    assert "bump" in {t.name for t in snap.tools}
    new_pid = int(text_of(await executor.call_tool(server, "pid", {})))
    assert new_pid != server_pid


@pytest.mark.anyio
async def test_concurrent_first_requests_share_one_server_process(counter_stdio_cmd: list[str]) -> None:
    server = make_server(counter_stdio_cmd)
    executor = await connect_or_start(make_settings())
    assert executor is not None
    pids: list[str] = []

    async def ask() -> None:
        pids.append(text_of(await executor.call_tool(server, "pid", {})))

    async with anyio.create_task_group() as tg:
        for _ in range(5):
            tg.start_soon(ask)
    assert len(pids) == 5
    assert len(set(pids)) == 1


# ---- starting -------------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_concurrent_connect_or_start_makes_one_daemon() -> None:
    settings = make_settings()
    results: list[DaemonExecutor | None] = []

    async def go() -> None:
        results.append(await connect_or_start(settings))

    async with anyio.create_task_group() as tg:
        tg.start_soon(go)
        tg.start_soon(go)
    assert len(results) == 2 and all(r is not None for r in results)

    state = read_state()
    assert state is not None
    status = await daemon_status()
    assert status is not None and status["pid"] == state.pid
    log_text = (paths.logs_dir() / "daemon.log").read_text(encoding="utf-8")
    assert log_text.count("daemon started") == 1


@pytest.mark.anyio
async def test_connect_or_start_returns_none_and_warns_once_when_spawn_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "executable", str(paths.mcpc_home() / "no-such-python.exe"))
    executor = await connect_or_start(make_settings())
    assert executor is None
    err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(err_lines) == 1
    assert err_lines[0].startswith("mcpc: warning: daemon unavailable, connecting directly (")


@pytest.mark.anyio
async def test_stale_state_file_is_replaced() -> None:
    paths.daemon_state_file().write_text(
        '{"pid": 999999, "port": 1, "token": "stale", "version": "x", "started_at": "now"}',
        encoding="utf-8",
    )
    executor = await connect_or_start(make_settings())
    assert executor is not None
    state = read_state()
    assert state is not None and state.token != "stale"


@pytest.mark.anyio
async def test_start_daemon_process_returns_a_pid() -> None:
    pid = start_daemon_process(make_settings())
    assert pid > 0
    await wait_until(lambda: read_state() is not None)
    assert await daemon_status() is not None


# ---- auth and versions ----------------------------------------------------------------------------
@pytest.mark.anyio
async def test_wrong_token_gets_no_response_and_correct_token_works() -> None:
    assert await connect_or_start(make_settings()) is not None
    state = read_state()
    assert state is not None

    for request in (
        Request(token="wrong-token", version=mcpc.__version__, method="status"),
        Request(token="", version=mcpc.__version__, method="stop"),
    ):
        async with await anyio.connect_tcp("127.0.0.1", state.port) as stream:
            await write_line(stream, request)
            with pytest.raises((anyio.EndOfStream, anyio.BrokenResourceError, OSError)):
                await stream.receive()

    async with await anyio.connect_tcp("127.0.0.1", state.port) as stream:
        await stream.send(b"this is not json\n")
        with pytest.raises((anyio.EndOfStream, anyio.BrokenResourceError, OSError)):
            await stream.receive()

    status = await daemon_status()  # the bad attempts did not stop it
    assert status is not None and status["pid"] == state.pid
    assert "wrong-token" not in (paths.logs_dir() / "daemon.log").read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_request_with_other_version_is_rejected_and_daemon_exits(counter_stdio_cmd: list[str]) -> None:
    assert await connect_or_start(make_settings()) is not None
    state = read_state()
    assert state is not None

    request = Request(
        token=state.token,
        version="0.0.0-other",
        method="call_tool",
        server=make_server(counter_stdio_cmd),
        params={"tool": "bump", "arguments": {}},
    )
    response = await dclient._roundtrip(state, request, 10)
    assert not response.ok and response.error is not None
    assert response.error["kind"] == "protocol"
    assert response.error["message"] == "daemon version mismatch"

    await wait_until(lambda: not pid_alive(state.pid))
    assert read_state() is None


@pytest.mark.anyio
async def test_connect_or_start_replaces_daemon_of_another_version() -> None:
    old_code = (
        "import mcpc; mcpc.__version__ = '0.0.0-old'; "
        "from mcpc.daemon.__main__ import main; raise SystemExit(main())"
    )
    subprocess.Popen(
        [sys.executable, "-c", old_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(paths.mcpc_home()),
    )
    await wait_until(lambda: (s := read_state()) is not None and s.version == "0.0.0-old", timeout=20)
    old = read_state()
    assert old is not None

    executor = await connect_or_start(make_settings())
    assert executor is not None
    new = read_state()
    assert new is not None
    assert new.pid != old.pid and new.version == mcpc.__version__
    await wait_until(lambda: not pid_alive(old.pid))


# ---- idle handling --------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_idle_server_session_is_reaped(counter_stdio_cmd: list[str]) -> None:
    server = make_server(counter_stdio_cmd)
    executor = await connect_or_start(make_settings(server_idle=0.02, daemon_idle=60))
    assert executor is not None
    assert text_of(await executor.call_tool(server, "bump", {})) == "1"
    status = await daemon_status()
    assert status is not None and [s["name"] for s in status["servers"]] == ["counter"]

    async def no_servers() -> bool:
        current = await daemon_status()
        return current is not None and current["servers"] == []

    await wait_until(no_servers, timeout=20)
    assert text_of(await executor.call_tool(server, "bump", {})) == "1"  # fresh process


@pytest.mark.anyio
async def test_idle_daemon_exits() -> None:
    assert await connect_or_start(make_settings(server_idle=0.02, daemon_idle=0.03)) is not None
    state = read_state()
    assert state is not None
    await wait_until(lambda: not pid_alive(state.pid), timeout=30)
    assert read_state() is None


@pytest.mark.anyio
async def test_stop_closes_sessions_and_removes_state(counter_stdio_cmd: list[str]) -> None:
    server = make_server(counter_stdio_cmd)
    executor = await connect_or_start(make_settings())
    assert executor is not None
    server_pid = int(text_of(await executor.call_tool(server, "pid", {})))
    state = read_state()
    assert state is not None

    assert await stop_daemon() is True
    assert not pid_alive(state.pid)
    assert read_state() is None
    await wait_until(lambda: not pid_alive(server_pid))
    assert await stop_daemon() is False
    assert await daemon_status() is None
    log_text = (paths.logs_dir() / "daemon.log").read_text(encoding="utf-8")
    assert "daemon exited" in log_text
    assert "crashed" not in log_text


# ---- secrets --------------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_secrets_are_not_written_to_disk(counter_stdio_cmd: list[str]) -> None:
    secret = "s3cr3t-value"
    server = make_server(counter_stdio_cmd, env={"SECRET_X": secret})
    executor = await connect_or_start(make_settings())
    assert executor is not None
    await executor.call_tool(server, "bump", {})
    await executor.snapshot(server)
    await wait_until(lambda: any(paths.cache_dir().glob("counter-*.json")))

    state_text = paths.daemon_state_file().read_text(encoding="utf-8")
    assert await stop_daemon() is True

    assert secret not in state_text
    log_text = (paths.logs_dir() / "daemon.log").read_text(encoding="utf-8")
    assert "request method=call_tool server=counter outcome=ok" in log_text
    assert secret not in log_text
    cache_files = list(paths.cache_dir().iterdir())
    assert cache_files
    for path in cache_files:
        assert secret not in path.read_text(encoding="utf-8")
    for path in paths.logs_dir().iterdir():
        assert secret not in path.read_text(encoding="utf-8", errors="replace")
