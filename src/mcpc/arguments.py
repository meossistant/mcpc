"""Parsing and client-side validation of tool and prompt arguments (plan §4.2)."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from mcpc.errors import ArgumentValidationError, UsageError

_MAX_ERROR_CHARS = 300


def split_pairs(pairs: Sequence[str]) -> dict[str, str]:
    """Split ``key=value`` items into raw strings. The first ``=`` separates; later pairs win."""
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise UsageError(f"invalid argument {pair!r}: expected key=value")
        if not key:
            raise UsageError(f"invalid argument {pair!r}: key is empty")
        out[key] = value
    return out


def parse_tool_arguments(
    args_json: str | None, pairs: Sequence[str], *, stdin: TextIO | None = None
) -> dict[str, Any]:
    """Build the tool argument object from ``--args`` and ``-a key=value`` items.

    ``args_json`` is None, JSON object text, ``@path`` (a JSON file) or ``-`` (JSON from stdin).
    Pair values are parsed as JSON when valid, else kept as strings; they override ``args_json``.
    Dotted keys are not nested. Use `coerce_to_schema` afterwards to restore string-typed values.
    """
    arguments = _load_args_json(args_json, stdin) if args_json is not None else {}
    for key, raw in split_pairs(pairs).items():
        arguments[key] = _parse_value(raw)
    return arguments


def parse_prompt_arguments(pairs: Sequence[str]) -> dict[str, str]:
    """Parse ``key=value`` items for a prompt. Prompt arguments are always strings."""
    return split_pairs(pairs)


def coerce_to_schema(
    arguments: dict[str, Any], input_schema: dict[str, Any], raw_pairs: Mapping[str, str]
) -> dict[str, Any]:
    """Return a copy of ``arguments`` where JSON-parsed pair values go back to their raw string
    when the property's schema allows ``string`` but not the parsed type.

    Example: ``-a zip=02134`` is already a string, but ``-a title=123`` parses to an int; with
    ``{"type": "string"}`` the result is ``"123"``.
    """
    properties = input_schema.get("properties")
    result = dict(arguments)
    if not isinstance(properties, dict):
        return result
    for key, raw in raw_pairs.items():
        if key not in result or isinstance(result[key], str):
            continue
        schema = properties.get(key)
        if not isinstance(schema, dict):
            continue
        types = _schema_types(schema)
        if "string" in types and not _matches_any_type(result[key], types):
            result[key] = raw
    return result


def validate_arguments(
    arguments: dict[str, Any], input_schema: dict[str, Any], *, server: str, tool: str
) -> None:
    """Validate arguments against the tool's JSON Schema (2020-12 unless ``$schema`` says otherwise).

    Raises ArgumentValidationError listing every problem. A schema that is itself invalid, or
    that cannot be evaluated, is skipped silently.
    """
    from jsonschema.validators import Draft202012Validator, validator_for  # slow import: only when validating

    cls = validator_for(input_schema, default=Draft202012Validator)
    try:
        cls.check_schema(input_schema)
        validator = cls(input_schema)
        errors = list(validator.iter_errors(arguments))
    except Exception:
        # Invalid schema, unresolvable $ref, ...: a broken server schema must not block the call.
        return
    if not errors:
        return
    details = sorted((_describe(e) for e in errors), key=lambda d: (d["path"], d["message"]))
    lines = "\n".join(f"  - {d['path']}: {d['message']}" for d in details)
    raise ArgumentValidationError(
        f"invalid arguments for {server}/{tool}:\n{lines}", server=server, detail=details
    )


def _load_args_json(args_json: str, stdin: TextIO | None) -> dict[str, Any]:
    if args_json == "-":
        text = (stdin if stdin is not None else sys.stdin).read()
        origin = "stdin"
    elif args_json.startswith("@"):
        path = Path(args_json[1:]).expanduser()
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise UsageError(f"cannot read arguments file {str(path)!r}: {exc.strerror or exc}") from exc
        origin = str(path)
    else:
        text, origin = args_json, "--args"
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise UsageError(f"invalid JSON in {origin}: {exc}") from exc
    if not isinstance(value, dict):
        raise UsageError(f"arguments in {origin} must be a JSON object, got {_json_type(value)}")
    return value


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw, parse_constant=_reject_constant)
    except ValueError:
        return raw


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _schema_types(schema: dict[str, Any]) -> set[str]:
    """Types allowed by a property schema: its ``type`` plus the ``type`` of anyOf/oneOf branches."""
    types: set[str] = set()
    for sub in (schema, *schema.get("anyOf", []), *schema.get("oneOf", [])):
        if not isinstance(sub, dict):
            continue
        declared = sub.get("type")
        if isinstance(declared, str):
            types.add(declared)
        elif isinstance(declared, list):
            types.update(t for t in declared if isinstance(t, str))
    return types


def _matches_any_type(value: Any, types: set[str]) -> bool:
    kind = _json_type(value)
    if kind in types:
        return True
    if kind == "integer":
        return "number" in types
    return kind == "number" and "integer" in types and float(value).is_integer()


def _describe(error: Any) -> dict[str, str]:
    path = [str(p) if not isinstance(p, int) else f"[{p}]" for p in error.absolute_path]
    if error.validator == "required":
        # jsonschema reports the parent path; point at the missing property instead.
        for name in error.validator_value:
            if error.message == f"{name!r} is a required property":
                path.append(str(name))
                break
    text = _join_path(path) or "(root)"
    message = error.message
    if len(message) > _MAX_ERROR_CHARS:
        message = message[: _MAX_ERROR_CHARS - 1] + "…"
    return {"path": text, "message": message}


def _join_path(parts: list[str]) -> str:
    out = ""
    for part in parts:
        out += part if part.startswith("[") else (f".{part}" if out else part)
    return out
