"""`mcpc call`, `resources`, `read`, `prompts`, `prompt`: requests to servers (plan Â§4.2, Â§4.3)."""

from __future__ import annotations

import difflib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import typer
from mcp_types import Prompt, Tool

from mcpc.arguments import (
    coerce_to_schema,
    parse_prompt_arguments,
    parse_tool_arguments,
    split_pairs,
    validate_arguments,
)
from mcpc.catalog.models import ServerSnapshot
from mcpc.cli.context import Ctx, parse_target, warn
from mcpc.config.models import ServerConfig
from mcpc.errors import McpcError, NotFoundError, UsageError
from mcpc.output import json as out_json
from mcpc.output import text as out_text

_T = TypeVar("_T")


# ---- call ----------------------------------------------------------------------------------------


async def _call(
    c: Ctx,
    target: str,
    tool_arg: str | None,
    args_json: str | None,
    pairs: list[str],
    no_validate: bool,
    max_output: int | None,
    out_dir_opt: Path | None,
) -> None:
    name, tool = parse_target(target, tool_arg)
    cfg = c.server(name)
    server = c.resolve(cfg)
    arguments = parse_tool_arguments(args_json, pairs, stdin=sys.stdin)
    snapshot, tool_def = await _lookup(c, cfg, lambda s: s.tool(tool))
    if snapshot is not None and tool_def is None:
        raise NotFoundError(
            _unknown_message("tool", tool, name, [t.name for t in snapshot.tools]), server=name
        )
    if tool_def is not None:
        arguments = _prepare_arguments(arguments, tool_def, pairs, name, no_validate)
    result = await (await c.executor()).call_tool(server, tool, arguments, prior=snapshot)
    out_dir = _resolve_out_dir(c, out_dir_opt)
    _emit_lazy(
        c,
        lambda: out_text.render_call_result(result, out_dir=out_dir, max_output=max_output),
        lambda: out_json.call_json(name, tool, result, out_dir),
    )
    if result.is_error:
        raise typer.Exit(1)


def _prepare_arguments(
    arguments: dict[str, Any], tool: Tool, pairs: list[str], server: str, no_validate: bool
) -> dict[str, Any]:
    arguments = coerce_to_schema(arguments, tool.input_schema, split_pairs(pairs))
    if not no_validate:
        validate_arguments(arguments, tool.input_schema, server=server, tool=tool.name)
    return arguments


async def _lookup(
    c: Ctx, cfg: ServerConfig, find: Callable[[ServerSnapshot], _T | None]
) -> tuple[ServerSnapshot | None, _T | None]:
    """Find an item (tool, prompt) in the catalog with as few connections as possible.

    Order: cached snapshot, then a fresh-or-refreshed snapshot, then one forced refresh when the
    snapshot that lacked the item came from the cache. A failure to get a snapshot only warns, so
    the server itself gets to report unknown names.
    """
    cached = None if c.refresh else c.cached(cfg)
    if cached is not None and (item := find(cached)) is not None:
        return cached, item
    snapshot = await _try_snapshot(c, cfg, force=None)
    if snapshot is None:
        # The catalog could not be fetched. `cached` here either lacks the item or is an error
        # placeholder with no data, so it must not be used to claim the item doesn't exist: let the
        # call go through and surface the real connection error (or the server's own answer).
        return None, None
    item = find(snapshot)
    came_from_cache = cached is not None and snapshot.fetched_at == cached.fetched_at
    if item is None and came_from_cache:
        snapshot = await _try_snapshot(c, cfg, force=True) or snapshot
        item = find(snapshot)
    return snapshot, item


async def _try_snapshot(c: Ctx, cfg: ServerConfig, *, force: bool | None) -> ServerSnapshot | None:
    try:
        return await c.snapshot(cfg, force=force)
    except McpcError as err:
        warn(f"{cfg.name}: could not read the catalog ({err.message}); continuing without validation")
        return None


def _unknown_message(what: str, name: str, server: str, known: list[str]) -> str:
    close = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
    if close:
        hint = f" Did you mean: {', '.join(close)}?"
    else:
        hint = f" Run 'mcpc show {server}' to see its {what}s."
    return f"unknown {what} '{name}' on server '{server}'.{hint}"


def _emit_lazy(c: Ctx, text: Callable[[], str], data: Callable[[], dict[str, Any]]) -> None:
    """Build only the rendering that will be printed: both would save binary content to disk."""
    c.emit(text=text, data=data)


def _resolve_out_dir(c: Ctx, out_dir: Path | None) -> Path:
    path = out_dir if out_dir is not None else c.out_dir
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(f"cannot create output directory {str(path)!r}: {exc.strerror or exc}") from exc
    return path


# ---- resources, read -----------------------------------------------------------------------------


async def _resources(c: Ctx, name: str) -> None:
    cfg = c.server(name)
    snap = await c.snapshot(cfg)
    empty = not snap.resources and not snap.resource_templates
    c.emit(
        text="no resources" if empty else out_text.render_resources(snap),
        data=out_json.resources_json(cfg.name, snap),
    )


async def _read(c: Ctx, name: str, uri: str, max_output: int | None, out_dir_opt: Path | None) -> None:
    cfg = c.server(name)
    server = c.resolve(cfg)
    result = await (await c.executor()).read_resource(server, uri, prior=c.cached(cfg))
    out_dir = _resolve_out_dir(c, out_dir_opt)
    _emit_lazy(
        c,
        lambda: out_text.render_read_result(result, out_dir=out_dir, max_output=max_output),
        lambda: out_json.read_json(cfg.name, uri, result, out_dir),
    )


# ---- prompts, prompt -----------------------------------------------------------------------------


async def _prompts(c: Ctx, name: str) -> None:
    cfg = c.server(name)
    snap = await c.snapshot(cfg)
    c.emit(
        text=out_text.render_prompts(snap) if snap.prompts else "no prompts",
        data=out_json.prompts_json(cfg.name, snap),
    )


async def _prompt(c: Ctx, target: str, name_arg: str | None, pairs: list[str]) -> None:
    name, prompt_name = parse_target(target, name_arg, what="prompt")
    cfg = c.server(name)
    server = c.resolve(cfg)
    arguments = parse_prompt_arguments(pairs)
    snapshot, prompt_def = await _lookup(c, cfg, lambda s: _find_prompt(s, prompt_name))
    if prompt_def is not None:
        _check_required(prompt_def, arguments, name)
    result = await (await c.executor()).get_prompt(server, prompt_name, arguments, prior=snapshot)
    _emit_lazy(
        c,
        lambda: out_text.render_prompt_result(result),
        lambda: out_json.prompt_json(name, prompt_name, result, _resolve_out_dir(c, None)),
    )


def _find_prompt(snap: ServerSnapshot, name: str) -> Prompt | None:
    return next((p for p in snap.prompts if p.name == name), None)


def _check_required(prompt: Prompt, arguments: dict[str, str], server: str) -> None:
    missing = [a.name for a in prompt.arguments or [] if a.required and a.name not in arguments]
    if missing:
        example = " ".join(f"-a {m}=VALUE" for m in missing)
        raise UsageError(
            f"prompt '{prompt.name}' is missing required arguments: {', '.join(missing)}. "
            f"Example: mcpc prompt {server}/{prompt.name} {example}",
            server=server,
        )
