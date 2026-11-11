"""Config data models. Shared contract: other packages import these, don't redefine them."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

Scope = Literal["global", "workspace"]


class StdioServer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["stdio"] = "stdio"
    command: str
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: str | None = None


class HttpServer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["http"] = "http"
    url: str
    transport: Literal["streamable-http", "sse"] = "streamable-http"
    headers: dict[str, str] = {}


ServerSpec = Annotated[StdioServer | HttpServer, Field(discriminator="kind")]


class ServerConfig(BaseModel):
    """One MCP server after loading and merging. `spec` may still contain ${...} templates."""

    name: str
    spec: ServerSpec
    description: str | None = None
    tags: list[str] = []
    timeout: float | None = None  # seconds per request; None -> settings default
    mode: Literal["auto", "legacy"] = "auto"
    disabled: bool = False
    scope: Scope
    source: Path
    overrides_global: bool = False  # workspace entry shadowing a global one with the same name

    def spec_hash(self) -> str:
        """sha256 of the normalized spec (as written, i.e. uninterpolated). Cache key component."""
        return _hash(self.spec.model_dump(mode="json"))

    @property
    def is_stdio(self) -> bool:
        return isinstance(self.spec, StdioServer)


class DaemonSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    enabled: bool = True
    server_idle_minutes: float = Field(
        default=15, validation_alias=AliasChoices("serverIdleMinutes", "server_idle_minutes")
    )
    daemon_idle_minutes: float = Field(
        default=60, validation_alias=AliasChoices("daemonIdleMinutes", "daemon_idle_minutes")
    )


class Settings(BaseModel):
    """`settings` block, read from the global file only."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    cache_ttl_hours: float = Field(
        default=24, validation_alias=AliasChoices("cacheTtlHours", "cache_ttl_hours")
    )
    connect_timeout: float = Field(
        default=30, validation_alias=AliasChoices("connectTimeout", "connect_timeout")
    )
    request_timeout: float = Field(
        default=300, validation_alias=AliasChoices("requestTimeout", "request_timeout")
    )
    daemon: DaemonSettings = Field(default_factory=lambda: DaemonSettings())


class LoadedConfig(BaseModel):
    """Result of config.loader.load_config()."""

    workspace_root: Path
    global_path: Path
    workspace_path: Path
    servers: dict[str, ServerConfig]  # includes disabled servers; callers filter
    settings: Settings = Field(default_factory=lambda: Settings())
    warnings: list[str] = []

    def enabled(self) -> list[ServerConfig]:
        return [s for s in self.servers.values() if not s.disabled]


def _hash(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resolved_spec_hash(spec: StdioServer | HttpServer) -> str:
    """Hash of an interpolated spec. Daemon pool key. Never persist the spec itself."""
    return _hash(spec.model_dump(mode="json"))
