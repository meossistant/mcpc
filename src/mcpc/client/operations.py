"""Thin async helpers over a connected `Client`: exhaustive paging and catalog snapshots."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from mcp import Client, MCPError
from mcp.types import METHOD_NOT_FOUND, Prompt, Resource, ResourceTemplate, Tool

from mcpc.catalog.models import ServerSnapshot
from mcpc.errors import ProtocolFailedError
from mcpc.executor import ResolvedServer

__all__ = [
    "list_all_prompts",
    "list_all_resource_templates",
    "list_all_resources",
    "list_all_tools",
    "take_snapshot",
]

_MAX_PAGES = 1000
_T = TypeVar("_T")


async def _collect(fetch: Callable[[str | None], Awaitable[Any]], field: str) -> list[Any]:
    """Follow `next_cursor` until exhausted. Guards against servers that loop cursors forever."""
    out: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(_MAX_PAGES):
        page = await fetch(cursor)
        out.extend(getattr(page, field))
        cursor = page.next_cursor
        if cursor is None:
            return out
        if cursor in seen:
            raise ProtocolFailedError(f"server returned a repeating pagination cursor ({field})")
        seen.add(cursor)
    raise ProtocolFailedError(f"server returned more than {_MAX_PAGES} pages ({field})")


async def list_all_tools(client: Client) -> list[Tool]:
    """List every tool, following pagination."""
    return await _collect(lambda c: client.list_tools(cursor=c), "tools")


async def list_all_resources(client: Client) -> list[Resource]:
    """List every resource, following pagination."""
    return await _collect(lambda c: client.list_resources(cursor=c), "resources")


async def list_all_resource_templates(client: Client) -> list[ResourceTemplate]:
    """List every resource template, following pagination."""
    return await _collect(lambda c: client.list_resource_templates(cursor=c), "resource_templates")


async def list_all_prompts(client: Client) -> list[Prompt]:
    """List every prompt, following pagination."""
    return await _collect(lambda c: client.list_prompts(cursor=c), "prompts")


async def _or_empty(call: Callable[[], Awaitable[list[_T]]], tolerate_missing: bool) -> list[_T]:
    """Run a list call; when capabilities are unknown, 'method not found' means 'nothing there'."""
    try:
        return await call()
    except MCPError as exc:
        if tolerate_missing and exc.code == METHOD_NOT_FOUND:
            return []
        raise


async def take_snapshot(client: Client, server: ResolvedServer) -> ServerSnapshot:
    """Build a full catalog snapshot from a connected client.

    Only lists what the server advertises. If it advertises none of tools/resources/prompts
    (capabilities unknown, e.g. a pinned connect without a prior DiscoverResult), every list is
    tried and 'method not found' counts as empty.
    """
    caps = client.server_capabilities
    unknown = caps.tools is None and caps.resources is None and caps.prompts is None
    want_tools = unknown or caps.tools is not None
    want_resources = unknown or caps.resources is not None
    want_prompts = unknown or caps.prompts is not None

    tools = await _or_empty(lambda: list_all_tools(client), unknown) if want_tools else []
    resources = await _or_empty(lambda: list_all_resources(client), unknown) if want_resources else []
    templates = (
        await _or_empty(lambda: list_all_resource_templates(client), unknown) if want_resources else []
    )
    prompts = await _or_empty(lambda: list_all_prompts(client), unknown) if want_prompts else []

    discover = client.session.discover_result
    return ServerSnapshot(
        server=server.name,
        spec_hash=server.config.spec_hash(),
        protocol_version=client.protocol_version,
        server_info=client.server_info,
        instructions=client.instructions,
        capabilities=caps.model_dump(mode="json", by_alias=True, exclude_none=True),
        discover_result=discover.model_dump(mode="json", by_alias=True) if discover is not None else None,
        tools=tools,
        resources=resources,
        resource_templates=templates,
        prompts=prompts,
    )
