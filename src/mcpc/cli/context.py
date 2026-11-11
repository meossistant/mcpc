"""Per-invocation CLI state and helpers shared by every command module.

Shared contract for Wave 2: command modules (servers.py, search.py, requests.py, daemon.py) use
`Ctx` and `run_command`; they don't parse global options or handle errors themselves.
"""

from __future__ import annotations

import difflib
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import anyio
import typer
from pydantic import BaseModel

from mcpc import paths, trust
from mcpc.catalog import refresh as catalog_refresh
from mcpc.catalog import store
from mcpc.catalog.models import ServerSnapshot
from mcpc.config import interpolate, loader
from mcpc.config.models import LoadedConfig, ServerConfig
from mcpc.errors import ExitCode, McpcError, NotFoundError, UsageError, translate
from mcpc.executor import Executor, ResolvedServer
from mcpc.output import json as out_json
from mcpc.output import text as out_text

_T = TypeVar("_T")


@dataclass
class Ctx:
    """Global options plus lazily loaded config and executor. Stored in `typer.Context.obj`."""

    json: bool = False
    workspace: Path | None = None
    config_path: Path | None = None
    scope: str = "all"  # all | global | workspace
    refresh: bool = False
    timeout: float | None = None
    no_daemon: bool = False
    verbose: bool = False
    agent: str = "none"
    _loaded: LoadedConfig | None = field(default=None, repr=False)
    _executor: Executor | None = field(default=None, repr=False)

    # ---- config -------------------------------------------------------------------------------
    def config(self) -> LoadedConfig:
        """Load (once) and return the merged config; warnings go to stderr."""
        if self._loaded is None:
            self._loaded = loader.load_config(self.workspace, self.config_path, agent_mode=self.agent)
            for w in self._loaded.warnings:
                warn(w)
        return self._loaded

    def servers(self, *, include_disabled: bool = False) -> list[ServerConfig]:
        """Servers in config order, filtered by --scope (and disabled unless include_disabled)."""
        out = []
        for s in self.config().servers.values():
            if s.disabled and not include_disabled:
                continue
            if self.scope != "all" and s.scope != self.scope:
                continue
            out.append(s)
        return out

    def server(self, name: str) -> ServerConfig:
        """Look up one server by name (ignores --scope). Unknown -> NotFoundError with suggestions.

        Disabled servers are returned; callers that connect should use `resolve()`, which refuses them.
        """
        servers = self.config().servers
        if name in servers:
            return servers[name]
        close = difflib.get_close_matches(name, list(servers), n=3)
        hint = (
            f" Did you mean: {', '.join(close)}?" if close else " Run 'mcpc list' to see configured servers."
        )
        raise NotFoundError(f"unknown server '{name}'.{hint}")

    def is_untrusted(self, cfg: ServerConfig) -> bool:
        loaded = self.config()
        return trust.needs_trust(cfg) and not trust.is_trusted(loaded)

    def resolve(self, cfg: ServerConfig) -> ResolvedServer:
        """Trust check + interpolation + --timeout override. Raises McpcError."""
        if cfg.disabled:
            raise UsageError(f"server '{cfg.name}' is disabled in {cfg.source}", server=cfg.name)
        loaded = self.config()
        trust.ensure_trusted(loaded, cfg)
        resolved = interpolate.resolve(cfg, workspace_root=loaded.workspace_root, settings=loaded.settings)
        if self.timeout is not None:
            resolved = resolved.model_copy(update={"request_timeout": self.timeout})
        return resolved

    # ---- execution ----------------------------------------------------------------------------
    async def executor(self) -> Executor:
        if self._executor is None:
            from mcpc.executor_factory import make_executor

            self._executor = await make_executor(self.config().settings, no_daemon=self.no_daemon)
        return self._executor

    async def aclose(self) -> None:
        if self._executor is not None:
            await self._executor.aclose()
            self._executor = None

    async def snapshot(self, cfg: ServerConfig, *, force: bool | None = None) -> ServerSnapshot:
        """Cached snapshot if fresh, else refresh via the executor. Raises McpcError."""
        settings = self.config().settings
        return await catalog_refresh.get_snapshot(
            self.resolve(cfg),
            await self.executor(),
            ttl_hours=settings.cache_ttl_hours,
            force=self.refresh if force is None else force,
        )

    def cached(self, cfg: ServerConfig) -> ServerSnapshot | None:
        """Cached snapshot regardless of age (no connection)."""
        return store.load(cfg)

    @property
    def out_dir(self) -> Path:
        """Where binary content (images, audio, blobs) is written."""
        return paths.tmp_dir()

    # ---- output -------------------------------------------------------------------------------
    def emit(
        self,
        text: str | Callable[[], str] | None = None,
        data: BaseModel | dict[str, Any] | list[Any] | Callable[[], Any] | None = None,
    ) -> None:
        """Print `data` as JSON when --json, else `text`.

        Either argument may be a zero-argument callable; only the one that is printed gets evaluated
        (renderers that save binary content to disk must not run twice).
        """
        if self.json:
            if data is None:
                raise RuntimeError("emit(): --json requested but no JSON data provided")
            typer.echo(out_json.to_json(data() if callable(data) else data))
        elif text is not None:
            typer.echo(text() if callable(text) else text)


def warn(message: str) -> None:
    """Warning line on stderr."""
    typer.echo(f"mcpc: warning: {message}", err=True)


def get_ctx(ctx: typer.Context) -> Ctx:
    obj = ctx.find_root().obj
    if not isinstance(obj, Ctx):  # pragma: no cover - app callback always sets it
        obj = Ctx()
        ctx.find_root().obj = obj
    return obj


def fail(c: Ctx, err: McpcError) -> NoReturn:
    """Report an error (stderr always; stdout JSON too with --json) and exit with its code."""
    typer.echo(out_text.render_error(err), err=True)
    if c.json:
        typer.echo(out_json.to_json(out_json.error_json(err)))
    raise typer.Exit(int(err.exit_code))


def run_command(c: Ctx, body: Callable[[], Awaitable[_T]]) -> _T:
    """Run an async command body with uniform error handling and executor cleanup.

    McpcError (or any exception, translated) -> `fail()`. typer.Exit passes through.
    """

    async def main() -> _T:
        try:
            return await body()
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await c.aclose()
                except Exception:  # pragma: no cover - cleanup must not mask the real error
                    logging.getLogger(__name__).debug("executor close failed", exc_info=True)

    try:
        return anyio.run(main)
    except typer.Exit:
        raise
    except McpcError as e:
        fail(c, e)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except Exception as e:
        if c.verbose:
            import traceback

            traceback.print_exc(file=sys.stderr)
        fail(c, translate(e))


def parse_target(target: str, second: str | None, *, what: str = "tool") -> tuple[str, str]:
    """Accept `server/name` or `server name`. Returns (server, name)."""
    if second is not None:
        if "/" in target:
            raise UsageError(f"give either 'server/{what}' or 'server {what}', not both")
        return target, second
    server, sep, name = target.partition("/")
    if not sep or not server or not name:
        raise UsageError(f"expected 'server/{what}' (or 'server {what}'), got '{target}'")
    return server, name


EXIT = ExitCode
