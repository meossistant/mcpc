"""Catalog snapshot model. Shared contract between client, catalog store, daemon, search, output."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mcp_types import Implementation, Prompt, Resource, ResourceTemplate, Tool
from pydantic import BaseModel, Field


class ServerSnapshot(BaseModel):
    """Everything mcpc knows about one server's catalog at a point in time.

    SDK types are stored as-is; serialize with model_dump(mode="json", by_alias=True)
    and load with ServerSnapshot.model_validate(...).
    """

    server: str
    spec_hash: str  # ServerConfig.spec_hash() at fetch time
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    protocol_version: str | None = None
    server_info: Implementation | None = None
    instructions: str | None = None
    capabilities: dict[str, Any] | None = None  # ServerCapabilities dumped to JSON
    discover_result: dict[str, Any] | None = None  # DiscoverResult JSON (2026-era only), for prior_discover
    tools: list[Tool] = []
    resources: list[Resource] = []
    resource_templates: list[ResourceTemplate] = []
    prompts: list[Prompt] = []
    last_error: str | None = None  # set when the latest refresh failed; other fields keep the last good data

    def age_hours(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        return (now - self.fetched_at).total_seconds() / 3600

    def is_legacy(self) -> bool:
        return self.protocol_version is not None and self.discover_result is None

    def tool(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)


def first_sentence(text: str, max_len: int = 120) -> str:
    text = " ".join(text.split())
    for sep in (". ", "? ", "! "):
        i = text.find(sep)
        if i != -1:
            text = text[: i + 1]
            break
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def server_description(description: str | None, snap: ServerSnapshot | None) -> str:
    """Description fallback: config description -> instructions (first sentence) -> server title."""
    if description:
        return description
    if snap is not None:
        if snap.instructions:
            return first_sentence(snap.instructions)
        if snap.server_info is not None and snap.server_info.title:
            return snap.server_info.title
    return "(no description)"


def tool_summary(tool: Tool) -> str:
    """First line of a tool's description, or its title, or empty."""
    if tool.description:
        line = tool.description.strip().splitlines()[0].strip()
        if line:
            return line
    return tool.title or ""
