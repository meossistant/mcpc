"""JSON Schema -> flat parameter rows, used by every renderer that describes a tool."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_MAX_NEST = 1  # nested object properties are flattened one level deep
_MAX_REF_DEPTH = 8


@dataclass(frozen=True)
class ParamRow:
    """One parameter of a tool, ready to render."""

    name: str  # dotted for flattened nested properties, e.g. "filter.status"
    type: str  # "string", "array<string>", "string|null", "object", ...
    required: bool
    default: Any | None
    has_default: bool
    description: str
    enum: list[Any] | None


def param_rows(input_schema: dict[str, Any]) -> list[ParamRow]:
    """Flatten an object schema into parameter rows, in schema order.

    Nested object properties follow their parent as `parent.child` rows (one level only; deeper
    objects are typed `object`). A nested property counts as required only when both the parent
    and the nested schema require it.
    """
    root = input_schema if isinstance(input_schema, dict) else {}
    top = _resolve(root, root)
    props = top.get("properties")
    if not isinstance(props, dict):
        return []
    required = set(_required(top))
    rows: list[ParamRow] = []
    for name, sub in props.items():
        _add_rows(rows, root, str(name), sub, name in required, level=0)
    return rows


def schema_type(schema: Any, root: dict[str, Any] | None = None) -> str:
    """Type string for any (sub)schema: `string`, `array<string>`, `string|null`, `object`..."""
    if not isinstance(schema, dict):
        return "any"
    return _type_str(schema, root if root is not None else schema, frozenset())


def output_summary(schema: dict[str, Any], *, max_props: int = 8) -> str:
    """One-line summary of an output schema, e.g. `object {result: string}`."""
    resolved = _resolve(schema, schema)
    props = resolved.get("properties")
    type_ = schema_type(schema, schema)
    if not isinstance(props, dict) or not props:
        return type_
    required = set(_required(resolved))
    parts: list[str] = []
    for name, sub in list(props.items())[:max_props]:
        mark = "" if name in required else "?"
        parts.append(f"{name}{mark}: {schema_type(sub, schema)}")
    if len(props) > max_props:
        parts.append("...")
    return f"{type_} {{{', '.join(parts)}}}"


def example_placeholder(type_str: str) -> str:
    """`<string>`-style placeholder for the example command line."""
    alts: list[str] = []
    for alt in split_alternatives(type_str):
        alt = re.sub(r"\(.*?\)", "", alt)  # drop (format)
        alt = alt.split("<", 1)[0]  # array<string> -> array
        if alt and alt != "null" and alt not in alts:
            alts.append(alt)
    return f"<{'|'.join(alts) or 'value'}>"


# --- internals -----------------------------------------------------------------------------------


def _required(schema: dict[str, Any]) -> list[str]:
    req = schema.get("required")
    return [r for r in req if isinstance(r, str)] if isinstance(req, list) else []


def _add_rows(
    rows: list[ParamRow], root: dict[str, Any], name: str, raw: Any, required: bool, level: int
) -> None:
    sub = raw if isinstance(raw, dict) else {}
    resolved = _resolve(sub, root)
    type_ = _type_str(sub, root, frozenset())
    if level >= _MAX_NEST and _is_object(resolved):
        type_ = "object"
    rows.append(
        ParamRow(
            name=name,
            type=type_,
            required=required,
            default=resolved.get("default"),
            has_default="default" in resolved,
            description=_description(sub, resolved, name.rsplit(".", 1)[-1]),
            enum=_enum(resolved, root),
        )
    )
    nested = resolved.get("properties")
    if level < _MAX_NEST and _is_object(resolved) and isinstance(nested, dict):
        nested_required = set(_required(resolved))
        for child, child_schema in nested.items():
            _add_rows(
                rows, root, f"{name}.{child}", child_schema, required and child in nested_required, level + 1
            )


def _is_object(resolved: dict[str, Any]) -> bool:
    t = resolved.get("type")
    return t == "object" or (t is None and isinstance(resolved.get("properties"), dict))


def _ref_target(ref: str, root: dict[str, Any]) -> dict[str, Any] | None:
    for prefix in ("#/$defs/", "#/definitions/"):
        if ref.startswith(prefix):
            defs = root.get(prefix[2:-1])
            target = defs.get(ref[len(prefix) :]) if isinstance(defs, dict) else None
            return target if isinstance(target, dict) else None
    return None


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Follow `$ref` (and single-branch `allOf`) chains, merging sibling keywords over the target."""
    seen: set[str] = set()
    current = schema
    for _ in range(_MAX_REF_DEPTH):
        ref = current.get("$ref")
        all_of = current.get("allOf")
        if isinstance(ref, str) and ref not in seen:
            seen.add(ref)
            target = _ref_target(ref, root)
            if target is None:
                break
            current = {**target, **{k: v for k, v in current.items() if k != "$ref"}}
        elif isinstance(all_of, list) and len(all_of) == 1 and isinstance(all_of[0], dict):
            current = {**all_of[0], **{k: v for k, v in current.items() if k != "allOf"}}
        else:
            break
    return current


def _type_str(schema: dict[str, Any], root: dict[str, Any], refs: frozenset[str]) -> str:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in refs:  # cycle
            return "object"
        refs = refs | {ref}
    resolved = _resolve(schema, root)
    for key in ("anyOf", "oneOf"):
        branches = resolved.get(key)
        if isinstance(branches, list) and branches:
            return _join([_type_str(b, root, refs) if isinstance(b, dict) else "any" for b in branches])
    if "const" in resolved and "type" not in resolved:
        return _infer([resolved["const"]])
    t = resolved.get("type")
    if isinstance(t, list):
        return _join([_single_type(resolved, str(x), root, refs) for x in t])
    if isinstance(t, str):
        return _single_type(resolved, t, root, refs)
    if isinstance(resolved.get("enum"), list) and resolved["enum"]:
        return _infer(resolved["enum"])
    if isinstance(resolved.get("properties"), dict):
        return "object"
    return "any"


def _single_type(resolved: dict[str, Any], t: str, root: dict[str, Any], refs: frozenset[str]) -> str:
    if t == "array":
        items = resolved.get("items")
        if isinstance(items, dict):
            return f"array<{_type_str(items, root, refs)}>"
        return "array"
    fmt = resolved.get("format")
    if t == "string" and isinstance(fmt, str) and fmt:
        return f"string({fmt})"
    return t


def _join(parts: list[str]) -> str:
    flat: list[str] = []
    for part in parts:
        for piece in split_alternatives(part):
            if piece not in flat:
                flat.append(piece)
    return "|".join(flat)


def split_alternatives(type_str: str) -> list[str]:
    """Split a type string on top-level `|` (not inside `<...>`)."""
    out: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(type_str):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "|" and depth == 0:
            out.append(type_str[start:i])
            start = i + 1
    out.append(type_str[start:])
    return out


def _infer(values: list[Any]) -> str:
    kinds: list[str] = []
    for v in values:
        k = _py_kind(v)
        if k not in kinds:
            kinds.append(k)
    return "|".join(kinds)


def _py_kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    return "object"


def _enum(resolved: dict[str, Any], root: dict[str, Any]) -> list[Any] | None:
    enum = resolved.get("enum")
    if isinstance(enum, list) and enum:
        return list(enum)
    if "const" in resolved:
        return [resolved["const"]]
    branches = resolved.get("anyOf") or resolved.get("oneOf")
    if isinstance(branches, list):
        values: list[Any] = []
        for b in branches:
            r = _resolve(b, root) if isinstance(b, dict) else {}
            if r.get("type") == "null":
                continue
            sub = _enum(r, root)
            if sub is None:  # a non-enum branch: no closed set of values
                return None
            values.extend(v for v in sub if v not in values)
        return values or None
    return None


def _normalize(s: str) -> str:
    return re.sub(r"[\s_\-.]+", "", s).lower()


def _description(raw: dict[str, Any], resolved: dict[str, Any], leaf: str) -> str:
    desc = raw.get("description") or resolved.get("description")
    if isinstance(desc, str) and desc.strip():
        return " ".join(desc.split())
    title = raw.get("title") or resolved.get("title")
    if isinstance(title, str) and title.strip() and _normalize(title) != _normalize(leaf):
        return " ".join(title.split())
    return ""
