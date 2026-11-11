"""Filesystem locations used by mcpc. Everything lives under MCPC_HOME (default ~/.mcpc)."""

from __future__ import annotations

import os
from pathlib import Path

WORKSPACE_DIR_NAME = ".mcpc"
CONFIG_FILE_NAME = "mcp.json"


def mcpc_home() -> Path:
    """Root directory for global config, cache, logs and daemon state."""
    env = os.environ.get("MCPC_HOME")
    return Path(env).expanduser() if env else Path.home() / ".mcpc"


def global_config_path() -> Path:
    return mcpc_home() / CONFIG_FILE_NAME


def workspace_config_path(workspace_root: Path) -> Path:
    return workspace_root / WORKSPACE_DIR_NAME / CONFIG_FILE_NAME


def cache_dir() -> Path:
    return _ensure(mcpc_home() / "cache")


def logs_dir() -> Path:
    return _ensure(mcpc_home() / "logs")


def tmp_dir() -> Path:
    return _ensure(mcpc_home() / "tmp")


def trust_file() -> Path:
    return mcpc_home() / "trust.json"


def daemon_state_file() -> Path:
    return mcpc_home() / "daemon.json"


def daemon_lock_file() -> Path:
    return _ensure(mcpc_home()) / "daemon.lock"


def server_log_path(server_name: str) -> Path:
    return logs_dir() / f"{server_name}.log"


def _ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p
