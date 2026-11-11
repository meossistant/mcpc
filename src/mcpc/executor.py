"""Executor interface: how the CLI talks to servers, independent of direct vs daemon mode.

Implementations:
  - mcpc.client.direct.DirectExecutor   (opens a Client per call, in-process)
  - mcpc.daemon.client.DaemonExecutor   (forwards to the background daemon over loopback TCP)

All methods receive a ResolvedServer (interpolated, trust-checked) and raise McpcError on failure.
They return SDK result types so rendering code is shared.
"""

from __future__ import annotations

from typing import Any, Protocol

from mcp_types import CallToolResult, GetPromptResult, ReadResourceResult
from pydantic import BaseModel

from mcpc.catalog.models import ServerSnapshot
from mcpc.config.models import HttpServer, ServerConfig, StdioServer, resolved_spec_hash


class ResolvedServer(BaseModel):
    """A ServerConfig whose spec has been interpolated. Lives in memory only; never persist it."""

    config: ServerConfig  # original (templated) config, for name/timeout/mode/spec_hash
    spec: StdioServer | HttpServer  # interpolated spec, contains real secrets
    connect_timeout: float = 30
    request_timeout: float = 300

    @property
    def name(self) -> str:
        return self.config.name

    def pool_key(self) -> str:
        return resolved_spec_hash(self.spec)


class Executor(Protocol):
    async def snapshot(self, server: ResolvedServer, prior: ServerSnapshot | None = None) -> ServerSnapshot:
        """Connect (or reuse a session) and return a full catalog snapshot.

        `prior` is the cached snapshot, used to pick the connect mode (legacy / pinned+prior_discover).
        """
        ...

    async def call_tool(
        self,
        server: ResolvedServer,
        tool: str,
        arguments: dict[str, Any],
        prior: ServerSnapshot | None = None,
    ) -> CallToolResult: ...

    async def read_resource(
        self, server: ResolvedServer, uri: str, prior: ServerSnapshot | None = None
    ) -> ReadResourceResult: ...

    async def get_prompt(
        self,
        server: ResolvedServer,
        name: str,
        arguments: dict[str, str],
        prior: ServerSnapshot | None = None,
    ) -> GetPromptResult: ...

    async def aclose(self) -> None: ...
