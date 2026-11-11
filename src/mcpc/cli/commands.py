"""Typer command definitions (names, options, help text) for every `mcpc` command.

This module is deliberately cheap to import: it needs only `typer` and the standard library. Each command
imports its implementation module (`servers`, `search`, `requests`, `daemon_cmd`) and `context` when it
actually runs, so `mcpc --version` and `mcpc --help` do not pay for pydantic models, the MCP types or the
catalog code.
"""

from __future__ import annotations

from pathlib import Path

import typer

_TARGET_HELP = "'server/name' (or give the server and the name as two arguments)."
_OUT_DIR_HELP = "Directory for saved images, audio and blobs (default: ~/.mcpc/tmp)."
_MAX_OUTPUT_HELP = "Truncate text output to this many characters."
_HELP_OPTIONS = {"help_option_names": ["-h", "--help"]}


def register_all(app: typer.Typer) -> None:
    """Add every command to the app (the order here is the order in `mcpc --help`)."""
    register_servers(app)
    register_search(app)
    register_requests(app)
    register_daemon(app)
    register_install(app)


def register_servers(app: typer.Typer) -> None:
    """Add list, show, refresh, check, config, trust and untrust to the app."""

    @app.command("list")
    def list_cmd(
        ctx: typer.Context,
        full: bool = typer.Option(False, "--full", help="Show transport, source, status and counts."),
        all_: bool = typer.Option(False, "--all", help="Include disabled servers."),
    ) -> None:
        """List configured MCP servers (from the cache; connects only with --refresh)."""
        from mcpc.cli import servers as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._list_body(c, full, all_))

    @app.command("show")
    def show_cmd(
        ctx: typer.Context,
        target: str = typer.Argument(..., help="'server' or 'server/tool'."),
        full: bool = typer.Option(
            False, "--full", help="Parameters, schemas and annotations for every tool."
        ),
    ) -> None:
        """Show a server's tools, or the full detail of one tool."""
        from mcpc.cli import servers as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._show_body(c, target, full))

    @app.command("refresh")
    def refresh_cmd(
        ctx: typer.Context,
        servers: list[str] | None = typer.Argument(None, help="Servers to refresh (default: all enabled)."),
    ) -> None:
        """Reconnect and rebuild the catalog cache, in parallel."""
        from mcpc.cli import servers as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._refresh_body(c, servers, check=False))

    @app.command("check")
    def check_cmd(
        ctx: typer.Context,
        servers: list[str] | None = typer.Argument(None, help="Servers to check (default: all enabled)."),
    ) -> None:
        """Connect and report protocol version, latency and capabilities. Non-zero exit if any fail."""
        from mcpc.cli import servers as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._refresh_body(c, servers, check=True))

    config_app = typer.Typer(
        name="config",
        help="Show the merged configuration (and `config path` for file locations).",
        invoke_without_command=True,
        add_completion=False,
        context_settings=_HELP_OPTIONS,
    )

    @config_app.callback()
    def config_cmd(ctx: typer.Context) -> None:
        """Merged configuration with the source of each server."""
        if ctx.invoked_subcommand is not None:
            return
        from mcpc.cli import servers as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)

        async def body() -> None:
            c.emit(text=impl._config_text(c), data=impl._config_data(c))

        run_command(c, body)

    @config_app.command("path")
    def config_path_cmd(ctx: typer.Context) -> None:
        """Print the global and workspace config file locations."""
        from mcpc.cli import servers as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)

        async def body() -> None:
            impl._config_path_output(c)

        run_command(c, body)

    app.add_typer(config_app, name="config")

    @app.command("trust")
    def trust_cmd(
        ctx: typer.Context,
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the y/N prompt (still needs a terminal)."),
    ) -> None:
        """Approve this workspace's MCP servers. Needs an interactive terminal."""
        from mcpc.cli import servers as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._trust_body(c, yes))

    @app.command("untrust")
    def untrust_cmd(ctx: typer.Context) -> None:
        """Revoke trust for this workspace."""
        from mcpc.cli import servers as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._untrust_body(c))


def register_search(app: typer.Typer) -> None:
    """Add the `search` command to the app."""

    @app.command("search")
    def search(
        ctx: typer.Context,
        query: str = typer.Argument(..., help="What you are looking for, in plain words."),
        limit: int = typer.Option(10, "--limit", "-n", help="Maximum number of results."),
        server: str | None = typer.Option(None, "--server", "-s", help="Only results from this server."),
        type_: str = typer.Option("tool", "--type", "-t", help="Search for 'tool' or 'server'."),
    ) -> None:
        """Search tools (or servers) across all configured servers with BM25 ranking."""
        from mcpc.cli import search as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._search(c, query, limit, server, type_))


def register_requests(app: typer.Typer) -> None:
    """Add call, resources, read, prompts and prompt to the app."""

    @app.command("call")
    def call(
        ctx: typer.Context,
        target: str = typer.Argument(..., help=_TARGET_HELP),
        tool: str | None = typer.Argument(None, help="Tool name, when TARGET is only the server."),
        args: str | None = typer.Option(
            None, "--args", "-j", help="Arguments as a JSON object, @file.json, or - to read stdin."
        ),
        arg: list[str] | None = typer.Option(
            None, "--arg", "-a", help="One argument as key=value (repeatable). Overrides --args."
        ),
        no_validate: bool = typer.Option(False, "--no-validate", help="Skip client-side schema validation."),
        max_output: int | None = typer.Option(None, "--max-output", help=_MAX_OUTPUT_HELP),
        out_dir: Path | None = typer.Option(None, "--out-dir", help=_OUT_DIR_HELP),
    ) -> None:
        """Call a tool. Exit code 1 when the tool reports an error."""
        from mcpc.cli import requests as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._call(c, target, tool, args, arg or [], no_validate, max_output, out_dir))

    @app.command("resources")
    def resources(ctx: typer.Context, server: str = typer.Argument(..., help="Server name.")) -> None:
        """List a server's resources and resource templates."""
        from mcpc.cli import requests as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._resources(c, server))

    @app.command("read")
    def read(
        ctx: typer.Context,
        server: str = typer.Argument(..., help="Server name."),
        uri: str = typer.Argument(..., help="Resource URI."),
        max_output: int | None = typer.Option(None, "--max-output", help=_MAX_OUTPUT_HELP),
        out_dir: Path | None = typer.Option(None, "--out-dir", help=_OUT_DIR_HELP),
    ) -> None:
        """Read one resource."""
        from mcpc.cli import requests as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._read(c, server, uri, max_output, out_dir))

    @app.command("prompts")
    def prompts(ctx: typer.Context, server: str = typer.Argument(..., help="Server name.")) -> None:
        """List a server's prompts."""
        from mcpc.cli import requests as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._prompts(c, server))

    @app.command("prompt")
    def prompt(
        ctx: typer.Context,
        target: str = typer.Argument(..., help=_TARGET_HELP),
        name: str | None = typer.Argument(None, help="Prompt name, when TARGET is only the server."),
        arg: list[str] | None = typer.Option(
            None, "--arg", "-a", help="One prompt argument as key=value (repeatable)."
        ),
    ) -> None:
        """Render a prompt."""
        from mcpc.cli import requests as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._prompt(c, target, name, arg or []))


def register_daemon(app: typer.Typer) -> None:
    """Add the `daemon` command group to the app."""
    daemon_app = typer.Typer(
        name="daemon",
        help="Control the background daemon that keeps server connections open.",
        no_args_is_help=True,
        add_completion=False,
        context_settings=_HELP_OPTIONS,
    )

    @daemon_app.command("start")
    def start_cmd(ctx: typer.Context) -> None:
        """Start the daemon if it is not running."""
        from mcpc.cli import daemon_cmd as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._start_body(c))

    @daemon_app.command("stop")
    def stop_cmd(ctx: typer.Context) -> None:
        """Stop the daemon and close all its server sessions."""
        from mcpc.cli import daemon_cmd as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._stop_body(c))

    @daemon_app.command("status")
    def status_cmd(ctx: typer.Context) -> None:
        """Show whether the daemon runs, plus its live server sessions."""
        from mcpc.cli import daemon_cmd as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._status_body(c))

    @daemon_app.command("restart")
    def restart_cmd(ctx: typer.Context) -> None:
        """Stop the daemon, then start a new one."""
        from mcpc.cli import daemon_cmd as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._restart_body(c))

    app.add_typer(daemon_app, name="daemon")


def register_install(app: typer.Typer) -> None:
    """Add install-instruction to the app."""

    @app.command("install-instruction")
    def install_instruction_cmd(
        ctx: typer.Context,
        skill: bool = typer.Option(
            False, "--skill", help="Print the raw SKILL.md file content instead of the install steps."
        ),
    ) -> None:
        """Print instructions that guide an AI agent through installing the mcpc skill."""
        from mcpc.cli import install_instruction as impl
        from mcpc.cli.context import get_ctx, run_command

        c = get_ctx(ctx)
        run_command(c, lambda: impl._body(c, skill))
