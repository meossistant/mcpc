"""Plain-text renderers. Pure functions returning strings; the CLI prints them.

Output is ASCII-friendly, uncolored and compact: the main reader is an AI agent with limited context.
The only side effect is `save_blob`, which writes binary content (images, audio, blobs) to disk.
"""

from __future__ import annotations

import base64
import binascii
import inspect
import json
import mimetypes
import uuid
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
    ResourceLink,
    ResourceTemplate,
    TextContent,
    TextResourceContents,
    Tool,
)

from mcpc.catalog.models import ServerSnapshot, server_description, tool_summary
from mcpc.config.models import ServerConfig
from mcpc.errors import McpcError
from mcpc.output.schema import ParamRow, example_placeholder, output_summary, param_rows
from mcpc.output.schemas import ServerListRow

EMPTY_SERVER_LIST = "No MCP servers configured. Add them to ~/.mcpc/mcp.json or .mcpc/mcp.json."
NO_OUTPUT = "(no output)"

_LABEL_WIDTH = 13
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "application/pdf": ".pdf",
    "application/json": ".json",
    "text/plain": ".txt",
}

# --- binary content ------------------------------------------------------------------------------


def mime_extension(mime_type: str | None) -> str:
    """File extension (with dot) for a MIME type; `.bin` when unknown."""
    base = (mime_type or "").split(";", 1)[0].strip().lower()
    if base in _MIME_EXT:
        return _MIME_EXT[base]
    return (mimetypes.guess_extension(base) if base else None) or ".bin"


def save_blob(data_b64: str, mime_type: str | None, out_dir: Path) -> tuple[Path, int]:
    """Decode base64 `data_b64` into `out_dir/<uuid>.<ext>`. Returns (path, size in bytes).

    Raises ValueError when the data is not valid base64.
    """
    try:
        raw = base64.b64decode(data_b64, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64 data: {exc}") from exc
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{uuid.uuid4().hex}{mime_extension(mime_type)}"
    path.write_bytes(raw)
    return path, len(raw)


def _saved_note(kind: str, data_b64: str, mime_type: str | None, out_dir: Path) -> str:
    mime = mime_type or "application/octet-stream"
    try:
        path, size = save_blob(data_b64, mime_type, out_dir)
    except ValueError:
        return f"[{kind} not saved: invalid base64 data ({mime})]"
    return f"[{kind} saved: {path} ({mime}, {size} bytes)]"


# --- server list ---------------------------------------------------------------------------------


def render_server_list(rows: Sequence[ServerListRow], *, full: bool) -> str:
    """`mcpc list`: compact (`name  description`) or full (one block per server)."""
    if not rows:
        return EMPTY_SERVER_LIST
    return _list_full(rows) if full else _list_compact(rows)


def _list_compact(rows: Sequence[ServerListRow]) -> str:
    width = max(len(r.name) for r in rows)
    lines = []
    for r in rows:
        suffix = f" [{r.status}]" if r.status in ("untrusted", "error", "disabled") else ""
        lines.append(f"{r.name.ljust(width)}  {r.description}{suffix}".rstrip())
    return "\n".join(lines)


def _list_full(rows: Sequence[ServerListRow]) -> str:
    return "\n\n".join(_list_block(r) for r in rows)


def _field(label: str, value: str) -> str:
    return f"  {(label + ':').ljust(_LABEL_WIDTH)}{value}"


def _list_block(r: ServerListRow) -> str:
    lines = [r.name]
    if r.description:
        lines.append(_field("description", r.description))
    lines.append(_field("status", _status_text(r)))
    scope = r.scope + (" (overrides global)" if r.overrides_global else "")
    lines.append(_field("scope", scope + (f"   source: {r.source}" if r.source else "")))
    lines.append(_field("transport", r.transport + (f"   {r.target}" if r.target else "")))
    provides = _provides(r)
    if provides:
        lines.append(_field("provides", provides))
    return "\n".join(lines)


def _status_text(r: ServerListRow) -> str:
    details: list[str] = []
    if r.error:
        details.append(_first_line(r.error))
    if r.protocol_version:
        details.append(f"protocol {r.protocol_version}")
    if r.cache_age_hours is not None:
        details.append(f"cached {r.cache_age_hours:.1f}h ago")
    if r.live:
        details.append("live in daemon")
    return f"{r.status} ({', '.join(details)})" if details else r.status


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _provides(r: ServerListRow) -> str:
    parts: list[str] = []
    if r.tools is not None:
        parts.append(_plural(r.tools, "tool"))
    for count, word in ((r.resources, "resource"), (r.resource_templates, "template"), (r.prompts, "prompt")):
        if count:
            parts.append(_plural(count, word))
    return ", ".join(parts)


# --- show ----------------------------------------------------------------------------------------


def render_server(cfg: ServerConfig, snap: ServerSnapshot | None, *, full: bool) -> str:
    """`mcpc show <server>`: compact tool list, or full detail for tools/resources/prompts."""
    header = f"{cfg.name}: {server_description(cfg.description, snap)}"
    if snap is None:
        return f"{header}\n(catalog not available: not cached)"
    if not snap.tools and snap.last_error and not _has_catalog(snap):
        return f"{header}\n(catalog not available: {_first_line(snap.last_error)})"
    return _show_full(cfg, snap, header) if full else _show_compact(cfg, snap, header)


def _has_catalog(snap: ServerSnapshot) -> bool:
    return bool(snap.tools or snap.resources or snap.resource_templates or snap.prompts)


def _stale_note(snap: ServerSnapshot) -> list[str]:
    if snap.last_error:
        return [f"(last refresh failed: {_first_line(snap.last_error)})"]
    return []


def _show_compact(cfg: ServerConfig, snap: ServerSnapshot, header: str) -> str:
    lines = [header, *_stale_note(snap), f"tools ({len(snap.tools)}):"]
    for tool in snap.tools:
        summary = tool_summary(tool)
        lines.append(f"  {tool.name}: {summary}" if summary else f"  {tool.name}")
    return "\n".join(lines)


def _show_full(cfg: ServerConfig, snap: ServerSnapshot, header: str) -> str:
    head = [header]
    info = snap.server_info
    if info is not None:
        ident = f"{info.name} {info.version}".strip()
        proto = f" (protocol {snap.protocol_version})" if snap.protocol_version else ""
        head.append(f"server: {ident}{proto}")
    elif snap.protocol_version:
        head.append(f"protocol: {snap.protocol_version}")
    head.extend(_stale_note(snap))
    if snap.instructions:
        head.append("instructions:")
        head.append(_indent(inspect.cleandoc(snap.instructions), 2))
    sections = ["\n".join(head)]
    if snap.tools:
        tools = "\n\n".join(_indent(render_tool(cfg.name, t), 2) for t in snap.tools)
        sections.append(f"tools ({len(snap.tools)}):\n{tools}")
    sections.extend(_catalog_sections(snap))
    return "\n\n".join(sections)


def _catalog_sections(snap: ServerSnapshot) -> list[str]:
    """`resources:`, `resource templates:` and `prompts:` sections; empty ones are skipped."""
    return [*_resource_sections(snap), *_prompt_sections(snap)]


def _resource_sections(snap: ServerSnapshot) -> list[str]:
    sections: list[str] = []
    if snap.resources:
        body = "\n".join(_resource_entry(r) for r in snap.resources)
        sections.append(f"resources ({len(snap.resources)}):\n{body}")
    if snap.resource_templates:
        body = "\n".join(_template_entry(t) for t in snap.resource_templates)
        sections.append(f"resource templates ({len(snap.resource_templates)}):\n{body}")
    return sections


def _prompt_sections(snap: ServerSnapshot) -> list[str]:
    if not snap.prompts:
        return []
    body = "\n".join(_prompt_entry(p) for p in snap.prompts)
    return [f"prompts ({len(snap.prompts)}):\n{body}"]


def _described(head: str, description: str | None) -> str:
    if description and description.strip():
        return f"{head}\n{_indent(inspect.cleandoc(description), 4)}"
    return head


def _resource_entry(r: Resource) -> str:
    mime = f"  ({r.mime_type})" if r.mime_type else ""
    return _described(f"  {r.name}  {r.uri}{mime}", r.description)


def _template_entry(t: ResourceTemplate) -> str:
    mime = f"  ({t.mime_type})" if t.mime_type else ""
    return _described(f"  {t.name}  {t.uri_template}{mime}", t.description)


def _prompt_entry(p: Prompt) -> str:
    title = f"  ({p.title})" if p.title and p.title != p.name else ""
    lines = [_described(f"  {p.name}{title}", p.description)]
    for arg in p.arguments or []:
        flag = "required" if arg.required else "optional"
        desc = f" - {_one_line(arg.description)}" if arg.description else ""
        lines.append(f"    {arg.name}  {flag}{desc}")
    return "\n".join(lines)


def render_resources(snap: ServerSnapshot) -> str:
    """`mcpc resources <server>`: resources and resource templates."""
    sections = _resource_sections(snap)
    return "\n\n".join(sections) if sections else "No resources."


def render_prompts(snap: ServerSnapshot) -> str:
    """`mcpc prompts <server>`."""
    sections = _prompt_sections(snap)
    return sections[0] if sections else "No prompts."


# --- tool detail ---------------------------------------------------------------------------------


def render_tool(server: str, tool: Tool) -> str:
    """`mcpc show <server>/<tool>`: everything an agent needs right before calling the tool."""
    title = tool.title or (tool.annotations.title if tool.annotations else None)
    head = f"{server}/{tool.name}"
    if title and title != tool.name:
        head += f"  ({title})"
    lines = [head]
    if tool.description and tool.description.strip():
        lines.append(_indent(inspect.cleandoc(tool.description), 2))
    rows = param_rows(tool.input_schema)
    if rows:
        lines.append("parameters:")
        lines.extend(f"  {_param_line(r)}" for r in rows)
    else:
        lines.append("parameters: none")
    hints = _annotation_hints(tool)
    if hints:
        lines.append(f"annotations: {', '.join(hints)}")
    if tool.output_schema:
        lines.append(f"output: {output_summary(tool.output_schema)}")
    lines.append(f"example: {_example(server, tool, rows)}")
    return "\n".join(lines)


def _param_line(r: ParamRow) -> str:
    flags: list[str] = []
    if r.required:
        flags.append("required")
    if r.has_default:
        flags.append(f"default: {_value(r.default)}")
    if r.enum:
        flags.append(f"enum: {', '.join(_value(v) for v in r.enum)}")
    line = f"{r.name}  {r.type}"
    if flags:
        line += "  " + "  ".join(flags)
    if r.description:
        line += f" - {r.description}"
    return line


def _value(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def _annotation_hints(tool: Tool) -> list[str]:
    ann = tool.annotations
    if ann is None:
        return []
    table = (
        (ann.read_only_hint, "read-only", "read-write"),
        (ann.destructive_hint, "destructive", "non-destructive"),
        (ann.idempotent_hint, "idempotent", "non-idempotent"),
        (ann.open_world_hint, "open-world", "closed-world"),
    )
    return [on if value else off for value, on, off in table if value is not None]


def _example(server: str, tool: Tool, rows: list[ParamRow]) -> str:
    args = [f"-a {r.name}={example_placeholder(r.type)}" for r in rows if r.required and "." not in r.name]
    return " ".join([f"mcpc call {server}/{tool.name}", *args])


# --- search hits ---------------------------------------------------------------------------------


def render_tool_hits(hits: Sequence[Any]) -> str:
    """Ranked `server/tool  summary` lines. Hits expose `.server`, `.tool`, `.summary`."""
    if not hits:
        return "No matching tools."
    names = [f"{h.server}/{h.tool}" for h in hits]
    width = max(len(n) for n in names)
    return "\n".join(
        f"{name.ljust(width)}  {_one_line(h.summary)}".rstrip() for name, h in zip(names, hits, strict=True)
    )


def render_server_hits(hits: Sequence[Any]) -> str:
    """Ranked servers, each followed by its top matching tools."""
    if not hits:
        return "No matching servers."
    width = max(len(h.server) for h in hits)
    lines: list[str] = []
    for h in hits:
        lines.append(f"{h.server.ljust(width)}  {_one_line(h.description)}".rstrip())
        tools = [getattr(t, "tool", t) for t in h.top_tools]
        if tools:
            lines.append(f"  tools: {', '.join(str(t) for t in tools)}")
    return "\n".join(lines)


# --- call / read / prompt results ----------------------------------------------------------------


def render_call_result(result: CallToolResult, *, out_dir: Path, max_output: int | None = None) -> str:
    """Text for `mcpc call`: text blocks verbatim; binary blocks saved under `out_dir`."""
    pieces = [_block_text(b, out_dir) for b in result.content]
    has_text = any(isinstance(b, TextContent) for b in result.content)
    if not has_text and result.structured_content is not None:
        pieces.append(json.dumps(result.structured_content, indent=2, ensure_ascii=False))
    text = "\n".join(pieces) if pieces else NO_OUTPUT
    if result.is_error:
        text = f"error: {text}"
    return truncate(text, max_output)


def render_read_result(result: ReadResourceResult, *, out_dir: Path, max_output: int | None = None) -> str:
    """Text for `mcpc read`."""
    many = len(result.contents) > 1
    pieces: list[str] = []
    for item in result.contents:
        body = _contents_text(item, out_dir)
        pieces.append(f"[resource: {item.uri}]\n{body}" if many else body)
    return truncate("\n".join(pieces) if pieces else NO_OUTPUT, max_output)


def render_prompt_result(result: GetPromptResult) -> str:
    """Text for `mcpc prompt`: a `[role]` line then the message content, per message."""
    if not result.messages:
        return "(no messages)"
    blocks = [f"[{m.role}]\n{_block_text(m.content, None)}" for m in result.messages]
    return "\n\n".join(blocks)


def truncate(text: str, max_output: int | None) -> str:
    """Cut `text` to `max_output` characters and append a note saying how much was shown."""
    if max_output is None or max_output < 0 or len(text) <= max_output:
        return text
    return f"{text[:max_output]}\n[output truncated: {max_output} of {len(text)} characters shown]"


def _block_text(block: Any, out_dir: Path | None) -> str:
    """Render one content block. With `out_dir=None` binary data is described, not saved."""
    if isinstance(block, TextContent):
        return block.text
    if isinstance(block, (ImageContent, AudioContent)):
        return _binary_note(block.type, block.data, block.mime_type, out_dir)
    if isinstance(block, ResourceLink):
        return f"[resource link: {block.uri} ({block.name})]"
    if isinstance(block, EmbeddedResource):
        body = _contents_text(block.resource, out_dir)
        return f"[resource: {block.resource.uri}]\n{body}" if body else f"[resource: {block.resource.uri}]"
    return f"[unsupported content: {getattr(block, 'type', type(block).__name__)}]"


def _contents_text(item: TextResourceContents | BlobResourceContents, out_dir: Path | None) -> str:
    if isinstance(item, TextResourceContents):
        return item.text
    return _binary_note("blob", item.blob, item.mime_type, out_dir)


def _binary_note(kind: str, data_b64: str, mime_type: str | None, out_dir: Path | None) -> str:
    if out_dir is not None:
        return _saved_note(kind, data_b64, mime_type, out_dir)
    mime = mime_type or "application/octet-stream"
    return f"[{kind}: {mime}, {_decoded_size(data_b64)} bytes]"


def _decoded_size(data_b64: str) -> int:
    try:
        return len(base64.b64decode(data_b64, validate=False))
    except (binascii.Error, ValueError):
        return 0


# --- errors --------------------------------------------------------------------------------------


def render_error(err: McpcError) -> str:
    """One line for stderr: `mcpc: <server>: <message>` or `mcpc: <message>`."""
    if err.server:
        return f"mcpc: {err.server}: {err.message}"
    return f"mcpc: {err.message}"


# --- helpers -------------------------------------------------------------------------------------


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(f"{pad}{line}" if line.strip() else "" for line in text.splitlines())


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0].strip() if lines else ""


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())
