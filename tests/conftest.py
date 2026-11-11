"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(FIXTURES))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def mcpc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets an isolated MCPC_HOME so nothing touches the real ~/.mcpc."""
    home = tmp_path / "mcpc-home"
    home.mkdir()
    monkeypatch.setenv("MCPC_HOME", str(home))
    monkeypatch.delenv("MCPC_TRUST_ALL", raising=False)
    return home


@pytest.fixture
def bookshop_stdio_cmd() -> list[str]:
    """Command line that starts the bookshop fixture over stdio."""
    return [sys.executable, str(FIXTURES / "bookshop_server.py")]


@pytest.fixture
def weather_stdio_cmd() -> list[str]:
    return [sys.executable, str(FIXTURES / "weather_server.py")]


@pytest.fixture
def counter_stdio_cmd() -> list[str]:
    """Stateful server (tools: bump, pid, sleep, die) for daemon reuse/crash tests."""
    return [sys.executable, str(FIXTURES / "counter_server.py")]


@pytest.fixture
def write_config(mcpc_home: Path, tmp_path: Path):
    """Write global and/or workspace mcp.json. Returns the workspace root.

    Usage: ws = write_config(global_servers={...}, workspace_servers={...}, settings={...})
    The workspace root is tmp_path / "ws" (contains .mcpc/mcp.json when workspace_servers is given).
    """
    import json

    def _write(
        global_servers: dict | None = None,
        workspace_servers: dict | None = None,
        settings: dict | None = None,
    ) -> Path:
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        (ws / ".git").mkdir(exist_ok=True)
        g: dict = {"mcpServers": global_servers or {}}
        if settings is not None:
            g["settings"] = settings
        (mcpc_home / "mcp.json").write_text(json.dumps(g, indent=2), encoding="utf-8")
        if workspace_servers is not None:
            (ws / ".mcpc").mkdir(exist_ok=True)
            (ws / ".mcpc" / "mcp.json").write_text(
                json.dumps({"mcpServers": workspace_servers}, indent=2), encoding="utf-8"
            )
        return ws

    return _write


@pytest.fixture
def bookshop_http_url():
    """Start the bookshop fixture over Streamable HTTP on a free port; yield its URL."""
    import socket
    import subprocess
    import time

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, str(FIXTURES / "bookshop_server.py"), "--http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("bookshop HTTP fixture did not start")
    yield f"http://127.0.0.1:{port}/mcp"
    proc.kill()
    proc.wait(timeout=5)
