"""Pydantic models for every `--json` output shape. Tests pin this contract."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Status = Literal["ok", "error", "untrusted", "disabled", "not cached"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerListRow(_Out):
    """One server in `mcpc list`. Shared by the text and JSON renderers."""

    name: str
    description: str = ""
    scope: Literal["global", "workspace"] = "global"
    source: str = ""
    transport: Literal["stdio", "streamable-http", "sse"] = "stdio"
    target: str = ""  # redacted command line or URL, as given by the caller
    status: Status = "not cached"
    protocol_version: str | None = None
    tools: int | None = None
    resources: int | None = None
    resource_templates: int | None = None
    prompts: int | None = None
    cache_age_hours: float | None = None
    overrides_global: bool = False
    live: bool | None = None
    error: str | None = None


class ServerListOutput(_Out):
    servers: list[ServerListRow]


class ToolSummaryOutput(_Out):
    name: str
    summary: str = ""


class ToolOutput(_Out):
    server: str
    name: str
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None


class ServerInfoOutput(_Out):
    name: str
    version: str
    title: str | None = None


class ResourceOutput(_Out):
    name: str
    uri: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    size: int | None = None


class ResourceTemplateOutput(_Out):
    name: str
    uri_template: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None


class PromptArgumentOutput(_Out):
    name: str
    description: str | None = None
    required: bool = False


class PromptOutput(_Out):
    name: str
    title: str | None = None
    description: str | None = None
    arguments: list[PromptArgumentOutput] = []


class ServerOutput(_Out):
    """`mcpc show <server>`. Compact: `tools` holds name+summary. Full: complete ToolOutput entries."""

    name: str
    description: str
    scope: Literal["global", "workspace"]
    source: str
    transport: Literal["stdio", "streamable-http", "sse"]
    status: Status
    protocol_version: str | None = None
    server_info: ServerInfoOutput | None = None
    instructions: str | None = None
    error: str | None = None  # why the catalog is missing or stale
    tools: list[ToolSummaryOutput] | list[ToolOutput] | None = None
    resources: list[ResourceOutput] | None = None
    resource_templates: list[ResourceTemplateOutput] | None = None
    prompts: list[PromptOutput] | None = None


class ResourcesOutput(_Out):
    server: str
    resources: list[ResourceOutput]
    resource_templates: list[ResourceTemplateOutput]


class PromptsOutput(_Out):
    server: str
    prompts: list[PromptOutput]


class ToolHitOutput(_Out):
    server: str
    tool: str
    score: float
    summary: str = ""


class ServerHitOutput(_Out):
    server: str
    score: float
    description: str = ""
    top_tools: list[ToolHitOutput] = []


class SearchOutput(_Out):
    query: str
    type: Literal["tool", "server"]
    results: list[ToolHitOutput] | list[ServerHitOutput]


class CallOutput(_Out):
    server: str
    tool: str
    is_error: bool
    content: list[dict[str, Any]]
    structured_content: Any | None = None


class ReadOutput(_Out):
    server: str
    uri: str
    contents: list[dict[str, Any]]


class PromptMessageOutput(_Out):
    role: str
    content: dict[str, Any]


class PromptResultOutput(_Out):
    server: str
    name: str
    description: str | None = None
    messages: list[PromptMessageOutput]


class ErrorBody(_Out):
    code: int
    kind: str
    message: str
    server: str | None = None
    detail: Any | None = None


class ErrorOutput(_Out):
    error: ErrorBody
