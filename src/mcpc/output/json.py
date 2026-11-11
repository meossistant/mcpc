"""`--json` builders. Each returns a plain dict that validates against a model in `output/schemas.py`.

Binary content (images, audio, blobs) is saved to disk and replaced by a file path, so agents never
receive base64 data. `to_json` turns a builder result (or any model) into the final string.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mcp_types import (
    AudioContent,
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    GetPromptResult,
    ImageContent,
    Prompt,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    TextResourceContents,
    Tool,
)
from pydantic import BaseModel

from mcpc.catalog.models import ServerSnapshot, server_description, tool_summary
from mcpc.config.models import ServerConfig, StdioServer
from mcpc.errors import McpcError
from mcpc.output import schemas as s
from mcpc.output.text import save_blob

Builder = dict[str, Any]


def to_json(value: BaseModel | dict[str, Any] | list[Any]) -> str:
    """Serialize a model or builder result: indent=2, unsorted keys, no `None` model fields."""
    data = value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value
    return json.dumps(data, indent=2, ensure_ascii=False)


def _dump(model: BaseModel) -> Builder:
    return model.model_dump(mode="json", exclude_none=True)


# --- lists and show ------------------------------------------------------------------------------


def server_list_json(rows: Sequence[s.ServerListRow]) -> Builder:
    """`mcpc list --json`."""
    return _dump(s.ServerListOutput(servers=list(rows)))


def server_json(
    cfg: ServerConfig, snap: ServerSnapshot | None, full: bool, *, status: s.Status | None = None
) -> Builder:
    """`mcpc show <server> --json`. Pass `status` to override the one derived from `cfg` and `snap`."""
    out = s.ServerOutput(
        name=cfg.name,
        description=server_description(cfg.description, snap),
        scope=cfg.scope,
        source=str(cfg.source),
        transport="stdio" if isinstance(cfg.spec, StdioServer) else cfg.spec.transport,
        status=status or _status(cfg, snap),
        protocol_version=snap.protocol_version if snap else None,
        error=snap.last_error if snap else "not cached",
    )
    if snap is not None:
        _fill_catalog(out, cfg.name, snap, full)
    return _dump(out)


def _status(cfg: ServerConfig, snap: ServerSnapshot | None) -> s.Status:
    if cfg.disabled:
        return "disabled"
    if snap is None:
        return "not cached"
    return "error" if snap.last_error and not snap.tools else "ok"


def _fill_catalog(out: s.ServerOutput, server: str, snap: ServerSnapshot, full: bool) -> None:
    if snap.server_info is not None:
        info = snap.server_info
        out.server_info = s.ServerInfoOutput(name=info.name, version=info.version, title=info.title)
    if full:
        out.instructions = snap.instructions
        out.tools = [_tool_model(server, t) for t in snap.tools]
        out.resources = [_resource_model(r) for r in snap.resources]
        out.resource_templates = [_template_model(t) for t in snap.resource_templates]
        out.prompts = [_prompt_model(p) for p in snap.prompts]
    else:
        out.tools = [s.ToolSummaryOutput(name=t.name, summary=tool_summary(t)) for t in snap.tools]


def tool_json(server: str, tool: Tool) -> Builder:
    """`mcpc show <server>/<tool> --json`, including the raw `input_schema`."""
    return _dump(_tool_model(server, tool))


def _tool_model(server: str, tool: Tool) -> s.ToolOutput:
    annotations = (
        tool.annotations.model_dump(mode="json", by_alias=True, exclude_none=True)
        if tool.annotations
        else None
    )
    return s.ToolOutput(
        server=server,
        name=tool.name,
        title=tool.title,
        description=tool.description,
        input_schema=tool.input_schema,
        output_schema=tool.output_schema,
        annotations=annotations or None,
    )


def _resource_model(r: Resource) -> s.ResourceOutput:
    return s.ResourceOutput(
        name=r.name, uri=r.uri, title=r.title, description=r.description, mime_type=r.mime_type, size=r.size
    )


def _template_model(t: ResourceTemplate) -> s.ResourceTemplateOutput:
    return s.ResourceTemplateOutput(
        name=t.name,
        uri_template=t.uri_template,
        title=t.title,
        description=t.description,
        mime_type=t.mime_type,
    )


def _prompt_model(p: Prompt) -> s.PromptOutput:
    args = [
        s.PromptArgumentOutput(name=a.name, description=a.description, required=bool(a.required))
        for a in p.arguments or []
    ]
    return s.PromptOutput(name=p.name, title=p.title, description=p.description, arguments=args)


def resources_json(server: str, snap: ServerSnapshot) -> Builder:
    """`mcpc resources <server> --json`."""
    return _dump(
        s.ResourcesOutput(
            server=server,
            resources=[_resource_model(r) for r in snap.resources],
            resource_templates=[_template_model(t) for t in snap.resource_templates],
        )
    )


def prompts_json(server: str, snap: ServerSnapshot) -> Builder:
    """`mcpc prompts <server> --json`."""
    return _dump(s.PromptsOutput(server=server, prompts=[_prompt_model(p) for p in snap.prompts]))


# --- search --------------------------------------------------------------------------------------


def _tool_hit(hit: Any, server: str | None = None) -> s.ToolHitOutput:
    if isinstance(hit, str):  # a bare tool name
        return s.ToolHitOutput(server=server or "", tool=hit, score=0.0)
    return s.ToolHitOutput(
        server=hit.server, tool=hit.tool, score=round(float(hit.score), 4), summary=hit.summary or ""
    )


def tool_hits_json(query: str, hits: Sequence[Any]) -> Builder:
    """`mcpc search --type tool --json`. Hits expose `.server .tool .score .summary`."""
    return _dump(s.SearchOutput(query=query, type="tool", results=[_tool_hit(h) for h in hits]))


def server_hits_json(query: str, hits: Sequence[Any]) -> Builder:
    """`mcpc search --type server --json`. Hits expose `.server .score .description .top_tools`."""
    results = [
        s.ServerHitOutput(
            server=h.server,
            score=round(float(h.score), 4),
            description=h.description or "",
            top_tools=[_tool_hit(t, h.server) for t in h.top_tools],
        )
        for h in hits
    ]
    return _dump(s.SearchOutput(query=query, type="server", results=results))


def skill_hits_json(query: str, hits: Sequence[Any]) -> Builder:
    """`mcpc search --type skill --json`."""
    results = [
        s.SkillHitOutput(
            name=h.name,
            score=round(float(h.score), 4),
            summary=h.summary or "",
            agents=list(getattr(h, "agents", [])),
            plugin=getattr(h, "plugin", None),
            path=getattr(h, "path", ""),
        )
        for h in hits
    ]
    return _dump(s.SearchOutput(query=query, type="skill", results=results))


def plugin_hits_json(query: str, hits: Sequence[Any]) -> Builder:
    """`mcpc search --type plugin --json`."""
    results = [
        s.PluginHitOutput(
            name=h.name,
            score=round(float(h.score), 4),
            summary=h.summary or "",
            agents=list(getattr(h, "agents", [])),
            version=getattr(h, "version", None),
            skills_count=getattr(h, "skills_count", 0),
            servers_count=getattr(h, "servers_count", 0),
            source_dir=getattr(h, "source_dir", ""),
        )
        for h in hits
    ]
    return _dump(s.SearchOutput(query=query, type="plugin", results=results))


def unified_hits_json(query: str, hits: Sequence[Any], type_name: str = "all") -> Builder:
    """`mcpc search --type all --json`."""
    results = [
        s.UnifiedHitOutput(
            kind=getattr(h, "kind", "tool"),
            target=getattr(
                h, "target", f"{getattr(h, 'server', '')}/{getattr(h, 'tool', '')}"
            ),
            score=round(float(h.score), 4),
            summary=getattr(h, "summary", "") or getattr(h, "description", ""),
            server=getattr(h, "server", None),
            tool=getattr(h, "tool", None),
            agents=list(getattr(h, "agents", [])),
            plugin=getattr(h, "plugin", None),
            version=getattr(h, "version", None),
            path=getattr(h, "path", None),
            top_tools=[_tool_hit(t, getattr(h, "server", "")) for t in getattr(h, "top_tools", [])],
        )
        for h in hits
    ]
    return _dump(s.SearchOutput(query=query, type=type_name, results=results))


# --- call / read / prompt ------------------------------------------------------------------------


def call_json(server: str, tool: str, result: CallToolResult, out_dir: Path) -> Builder:
    """`mcpc call --json`. Binary blocks are saved under `out_dir` and replaced by their path."""
    return _dump(
        s.CallOutput(
            server=server,
            tool=tool,
            is_error=result.is_error,
            content=[_block_json(b, out_dir) for b in result.content],
            structured_content=result.structured_content,
        )
    )


def read_json(server: str, uri: str, result: ReadResourceResult, out_dir: Path) -> Builder:
    """`mcpc read --json`."""
    contents = [_contents_json(c, out_dir) for c in result.contents]
    return _dump(s.ReadOutput(server=server, uri=uri, contents=contents))


def prompt_json(server: str, name: str, result: GetPromptResult, out_dir: Path | None = None) -> Builder:
    """`mcpc prompt --json`. Without `out_dir`, binary blocks keep only their MIME type."""
    messages = [
        s.PromptMessageOutput(role=m.role, content=_block_json(m.content, out_dir)) for m in result.messages
    ]
    return _dump(
        s.PromptResultOutput(server=server, name=name, description=result.description, messages=messages)
    )


def _dump_block(block: BaseModel) -> dict[str, Any]:
    return block.model_dump(mode="json", by_alias=True, exclude_none=True)


def _block_json(block: Any, out_dir: Path | None) -> dict[str, Any]:
    if isinstance(block, (ImageContent, AudioContent)):
        return {"type": block.type, **_binary_json(block.data, block.mime_type, out_dir)}
    if isinstance(block, EmbeddedResource):
        data = _dump_block(block)
        data["resource"] = _contents_json(block.resource, out_dir)
        return data
    return _dump_block(block)


def _contents_json(item: TextResourceContents | BlobResourceContents, out_dir: Path | None) -> dict[str, Any]:
    if isinstance(item, TextResourceContents):
        return _dump_block(item)
    return {"uri": item.uri, **_binary_json(item.blob, item.mime_type, out_dir)}


def _binary_json(data_b64: str, mime_type: str | None, out_dir: Path | None) -> dict[str, Any]:
    """`{"mimeType", "path", "size"}` for saved data; just the MIME type when there is nowhere to save."""
    info: dict[str, Any] = {"mimeType": mime_type or "application/octet-stream"}
    if out_dir is None:
        return info
    try:
        path, size = save_blob(data_b64, mime_type, out_dir)
    except ValueError as exc:
        return {**info, "error": str(exc)}
    return {**info, "path": str(path), "size": size}


# --- errors --------------------------------------------------------------------------------------


def error_json(err: McpcError) -> Builder:
    """`{"error": {code, kind, message, server?, detail?}}`."""
    return _dump(s.ErrorOutput(error=s.ErrorBody(**err.to_dict())))
