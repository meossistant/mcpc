"""Typer application: global options and command registration.

Command definitions live in `mcpc.cli.commands`; their implementations are imported when a command runs,
so `--version` and `--help` stay fast.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from mcpc import __version__
from mcpc.cli.commands import register_all

app = typer.Typer(
    name="mcpc",
    help="Command-line MCP client. Discover MCP servers, inspect their tools, and call them.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"mcpc {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    json: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
    workspace: Path | None = typer.Option(
        None, "--workspace", "-w", help="Workspace root (default: detected from the current directory)."
    ),
    config: Path | None = typer.Option(
        None, "--config", help="Global config file (default: ~/.mcpc/mcp.json)."
    ),
    scope: str = typer.Option("all", "--scope", help="Which servers: all, global or workspace."),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the catalog cache and reconnect."),
    timeout: float | None = typer.Option(None, "--timeout", help="Request timeout in seconds."),
    no_daemon: bool = typer.Option(False, "--no-daemon", help="Connect directly, don't use the daemon."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose diagnostics on stderr."),
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show version."),
) -> None:
    if scope not in ("all", "global", "workspace"):
        raise typer.BadParameter("must be all, global or workspace", param_hint="--scope")
    if verbose:
        logging.basicConfig(level=logging.INFO, format="mcpc: %(levelname)s: %(message)s")
    from mcpc.cli.context import Ctx

    ctx.obj = Ctx(
        json=json,
        workspace=workspace,
        config_path=config,
        scope=scope,
        refresh=refresh,
        timeout=timeout,
        no_daemon=no_daemon,
        verbose=verbose,
    )


register_all(app)


def main() -> None:
    app()
