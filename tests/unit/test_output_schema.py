"""param_rows and friends: JSON Schema -> parameter rows."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from mcpc.output.schema import example_placeholder, output_summary, param_rows, schema_type


def _by_name(schema: dict[str, Any]) -> dict[str, Any]:
    return {r.name: r for r in param_rows(schema)}


def test_simple_types_required_and_defaults_in_schema_order() -> None:
    rows = param_rows(
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "title": "Query"},
                "limit": {"type": "integer", "default": 10, "title": "Limit"},
                "ratio": {"type": "number"},
                "flag": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        }
    )
    assert [r.name for r in rows] == ["query", "limit", "ratio", "flag"]
    assert [r.type for r in rows] == ["string", "integer", "number", "boolean"]
    assert [r.required for r in rows] == [True, False, False, False]
    limit = rows[1]
    assert limit.has_default and limit.default == 10
    assert rows[3].has_default and rows[3].default is False
    assert not rows[0].has_default and rows[0].default is None


def test_pydantic_title_noise_is_not_a_description() -> None:
    rows = _by_name(
        {
            "type": "object",
            "properties": {
                "book_title": {"type": "string", "title": "Book Title"},
                "q": {"type": "string", "title": "Search text"},
                "n": {"type": "integer", "title": "N", "description": "How many"},
            },
        }
    )
    assert rows["book_title"].description == ""
    assert rows["q"].description == "Search text"
    assert rows["n"].description == "How many"


def test_any_of_null_for_optionals() -> None:
    rows = _by_name(
        {
            "type": "object",
            "properties": {
                "comment": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
                "tags": {
                    "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                    "default": None,
                },
                "either": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
            },
        }
    )
    assert rows["comment"].type == "string|null"
    assert rows["comment"].has_default and rows["comment"].default is None
    assert rows["tags"].type == "array<string>|null"
    assert rows["either"].type == "integer|string"


def test_ref_to_defs_enum_and_description_from_target() -> None:
    schema = {
        "type": "object",
        "$defs": {
            "Genre": {"type": "string", "enum": ["fiction", "poetry"], "description": "Book genre"},
            "Tag": {"type": "string"},
        },
        "properties": {
            "genre": {"$ref": "#/$defs/Genre"},
            "opt_genre": {"anyOf": [{"$ref": "#/$defs/Genre"}, {"type": "null"}], "default": None},
            "tags": {"type": "array", "items": {"$ref": "#/$defs/Tag"}},
        },
        "required": ["genre"],
    }
    rows = _by_name(schema)
    assert rows["genre"].type == "string"
    assert rows["genre"].enum == ["fiction", "poetry"]
    assert rows["genre"].description == "Book genre"
    assert rows["opt_genre"].type == "string|null"
    assert rows["opt_genre"].enum == ["fiction", "poetry"]
    assert rows["tags"].type == "array<string>"


def test_legacy_definitions_and_ref_cycle_are_guarded() -> None:
    schema = {
        "type": "object",
        "definitions": {
            "Node": {"type": "object", "properties": {"next": {"$ref": "#/definitions/Node"}}},
            "A": {"$ref": "#/definitions/B"},
            "B": {"$ref": "#/definitions/A"},
        },
        "properties": {
            "node": {"$ref": "#/definitions/Node"},
            "loop": {"$ref": "#/definitions/A"},
            "missing": {"$ref": "#/$defs/Nope"},
        },
    }
    rows = _by_name(schema)  # must terminate
    assert rows["node"].type == "object"
    assert rows["node.next"].type == "object"
    assert "loop" in rows and "missing" in rows


def test_enum_const_and_literal() -> None:
    class Args(BaseModel):
        mode: Literal["fast", "slow"] = "fast"
        kind: Literal["only"]

    rows = _by_name(Args.model_json_schema())
    assert rows["mode"].enum == ["fast", "slow"]
    assert rows["mode"].default == "fast"
    assert rows["kind"].enum == ["only"]
    assert rows["kind"].required


def test_nested_objects_flatten_one_level_with_dotted_names() -> None:
    schema = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "object",
                "description": "Filter options",
                "properties": {
                    "status": {"type": "string", "enum": ["open", "closed"]},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "range": {"type": "object", "properties": {"lo": {"type": "integer"}}},
                },
                "required": ["status"],
            },
            "other": {"type": "string"},
        },
        "required": ["filter"],
    }
    rows = param_rows(schema)
    assert [r.name for r in rows] == ["filter", "filter.status", "filter.labels", "filter.range", "other"]
    by = {r.name: r for r in rows}
    assert by["filter"].type == "object" and by["filter"].required
    assert by["filter.status"].required and by["filter.status"].enum == ["open", "closed"]
    assert not by["filter.labels"].required
    assert by["filter.labels"].type == "array<string>"
    assert by["filter.range"].type == "object"  # deeper than one level: not flattened


def test_nested_required_needs_required_parent() -> None:
    schema = {
        "type": "object",
        "properties": {
            "opts": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
        },
    }
    by = _by_name(schema)
    assert not by["opts"].required
    assert not by["opts.a"].required


def test_arrays_and_format() -> None:
    rows = _by_name(
        {
            "type": "object",
            "properties": {
                "when": {"type": "string", "format": "date-time"},
                "matrix": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
                "bag": {"type": "array"},
                "objs": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"x": {"type": "integer"}}},
                },
                "multi": {"type": ["string", "null"]},
            },
        }
    )
    assert rows["when"].type == "string(date-time)"
    assert rows["matrix"].type == "array<array<integer>>"
    assert rows["bag"].type == "array"
    assert rows["objs"].type == "array<object>"
    assert rows["multi"].type == "string|null"


def test_degenerate_schemas() -> None:
    assert param_rows({}) == []
    assert param_rows({"type": "object"}) == []
    assert param_rows({"type": "object", "properties": {}}) == []
    assert param_rows("nope") == []  # type: ignore[arg-type]
    rows = _by_name({"type": "object", "properties": {"x": {}, "y": True}})  # type: ignore[dict-item]
    assert rows["x"].type == "any" and rows["y"].type == "any"


def test_schema_type_and_output_summary() -> None:
    assert schema_type({"type": "string"}) == "string"
    assert schema_type({"type": "array", "items": {"type": "integer"}}) == "array<integer>"
    assert schema_type(None) == "any"
    assert (
        output_summary(
            {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]}
        )
        == "object {result: string}"
    )
    assert (
        output_summary(
            {
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
                "required": ["a"],
            }
        )
        == "object {a: integer, b?: string}"
    )
    assert output_summary({"type": "array", "items": {"type": "string"}}) == "array<string>"
    many = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(10)}}
    assert output_summary(many, max_props=2).endswith("p0?: string, p1?: string, ...}")


def test_example_placeholder() -> None:
    assert example_placeholder("string") == "<string>"
    assert example_placeholder("integer") == "<integer>"
    assert example_placeholder("array<string>") == "<array>"
    assert example_placeholder("string(date-time)") == "<string>"
    assert example_placeholder("string|null") == "<string>"
    assert example_placeholder("integer|string") == "<integer|string>"
    assert example_placeholder("null") == "<value>"
