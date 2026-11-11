"""Argument parsing, schema coercion and validation (plan section 4.2)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from mcpc.arguments import (
    coerce_to_schema,
    parse_prompt_arguments,
    parse_tool_arguments,
    split_pairs,
    validate_arguments,
)
from mcpc.errors import ArgumentValidationError, ExitCode, UsageError

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["query"],
}


# --- parse_tool_arguments -------------------------------------------------------------------


def test_nothing_given_is_empty() -> None:
    assert parse_tool_arguments(None, []) == {}


def test_inline_json_object() -> None:
    assert parse_tool_arguments('{"query": "dune", "limit": 5}', []) == {"query": "dune", "limit": 5}


def test_args_from_file(tmp_path: Path) -> None:
    f = tmp_path / "args.json"
    f.write_text('{"query": "dune"}', encoding="utf-8")
    assert parse_tool_arguments(f"@{f}", []) == {"query": "dune"}


def test_args_from_file_with_bom(tmp_path: Path) -> None:
    f = tmp_path / "args.json"
    f.write_bytes(b"\xef\xbb\xbf" + b'{"query": "dune"}')
    assert parse_tool_arguments(f"@{f}", []) == {"query": "dune"}


def test_missing_args_file_is_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="cannot read arguments file"):
        parse_tool_arguments(f"@{tmp_path / 'nope.json'}", [])


def test_args_from_stdin() -> None:
    assert parse_tool_arguments("-", [], stdin=io.StringIO('{"query": "dune"}')) == {"query": "dune"}


def test_args_from_real_stdin_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO('{"a": 1}'))
    assert parse_tool_arguments("-", []) == {"a": 1}


@pytest.mark.parametrize("text", ["{not json", "", "[1, 2]", '"str"', "42", "null", "true"])
def test_bad_args_json_is_usage_error(text: str) -> None:
    with pytest.raises(UsageError) as info:
        parse_tool_arguments(text, [])
    assert info.value.exit_code == ExitCode.USAGE


def test_non_object_message_names_the_type() -> None:
    with pytest.raises(UsageError, match="must be a JSON object, got array"):
        parse_tool_arguments("[1]", [])


def test_invalid_json_on_stdin_names_stdin() -> None:
    with pytest.raises(UsageError, match="stdin"):
        parse_tool_arguments("-", [], stdin=io.StringIO("nope"))


def test_pair_type_inference() -> None:
    result = parse_tool_arguments(
        None,
        [
            "n=5",
            "f=1.5",
            "t=true",
            "off=false",
            "nothing=null",
            'arr=["a","b"]',
            'obj={"k": 1}',
            "word=hello",
            'quoted="hi"',
            "empty=",
            "zip=02134",
            "nan=NaN",
        ],
    )
    assert result == {
        "n": 5,
        "f": 1.5,
        "t": True,
        "off": False,
        "nothing": None,
        "arr": ["a", "b"],
        "obj": {"k": 1},
        "word": "hello",
        "quoted": "hi",
        "empty": "",
        "zip": "02134",
        "nan": "NaN",
    }
    assert type(result["n"]) is int


def test_pair_value_may_contain_equals() -> None:
    assert parse_tool_arguments(None, ["expr=a=b"]) == {"expr": "a=b"}


def test_dotted_keys_are_not_nested() -> None:
    assert parse_tool_arguments(None, ["a.b=1"]) == {"a.b": 1}


def test_pairs_override_json_and_later_pairs_override_earlier() -> None:
    result = parse_tool_arguments('{"query": "old", "keep": 1}', ["query=new", "query=newest"])
    assert result == {"query": "newest", "keep": 1}


@pytest.mark.parametrize("pair", ["novalue", "", "=5"])
def test_malformed_pair_is_usage_error(pair: str) -> None:
    with pytest.raises(UsageError):
        parse_tool_arguments(None, [pair])


def test_missing_equals_message_names_the_pair() -> None:
    with pytest.raises(UsageError, match="'novalue'"):
        parse_tool_arguments(None, ["novalue"])


# --- split_pairs / prompt arguments ---------------------------------------------------------


def test_split_pairs_keeps_raw_strings() -> None:
    assert split_pairs(["a=1", "b=true", "c=x=y"]) == {"a": "1", "b": "true", "c": "x=y"}


def test_prompt_arguments_are_always_strings() -> None:
    assert parse_prompt_arguments(["genre=poetry", "n=5", "flag=true"]) == {
        "genre": "poetry",
        "n": "5",
        "flag": "true",
    }
    assert parse_prompt_arguments([]) == {}
    with pytest.raises(UsageError):
        parse_prompt_arguments(["bad"])


# --- coerce_to_schema -----------------------------------------------------------------------


def test_string_property_gets_raw_string_back() -> None:
    schema = {"type": "object", "properties": {"zip": {"type": "string"}, "n": {"type": "integer"}}}
    raw = {"zip": "123", "n": "5"}
    parsed = parse_tool_arguments(None, ["zip=123", "n=5"])
    assert parsed == {"zip": 123, "n": 5}
    assert coerce_to_schema(parsed, schema, raw) == {"zip": "123", "n": 5}


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [("true", True), ("null", None), ("1.50", 1.5), ("[1]", [1]), ('{"a": 1}', {"a": 1}), ("007", 7)],
)
def test_all_parsed_types_revert_for_string_properties(raw: str, parsed: Any) -> None:
    schema = {"properties": {"v": {"type": "string"}}}
    assert coerce_to_schema({"v": parsed}, schema, {"v": raw}) == {"v": raw}


def test_union_type_keeps_parsed_value_when_it_matches() -> None:
    schema = {"properties": {"v": {"type": ["string", "integer"]}}}
    assert coerce_to_schema({"v": 5}, schema, {"v": "5"}) == {"v": 5}


def test_union_type_reverts_when_parsed_type_not_allowed() -> None:
    schema = {"properties": {"v": {"type": ["string", "null"]}}}
    assert coerce_to_schema({"v": 5}, schema, {"v": "5"}) == {"v": "5"}


def test_anyof_with_string_branch() -> None:
    schema = {"properties": {"v": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
    assert coerce_to_schema({"v": 5}, schema, {"v": "5"}) == {"v": "5"}
    assert coerce_to_schema({"v": None}, schema, {"v": "null"}) == {"v": None}


def test_integer_valued_float_matches_integer_type() -> None:
    schema = {"properties": {"v": {"type": ["string", "integer"]}}}
    assert coerce_to_schema({"v": 2.0}, schema, {"v": "2.0"}) == {"v": 2.0}


def test_args_json_values_are_never_coerced() -> None:
    schema = {"properties": {"zip": {"type": "string"}}}
    assert coerce_to_schema({"zip": 123}, schema, {}) == {"zip": 123}


def test_unknown_or_untyped_properties_are_left_alone() -> None:
    schema = {"properties": {"free": {}, "other": {"type": "integer"}}}
    arguments = {"free": 5, "other": 5, "extra": 5}
    raw = {"free": "5", "other": "5", "extra": "5"}
    assert coerce_to_schema(arguments, schema, raw) == arguments


def test_schema_without_properties() -> None:
    assert coerce_to_schema({"a": 1}, {"type": "object"}, {"a": "1"}) == {"a": 1}
    assert coerce_to_schema({"a": 1}, {}, {"a": "1"}) == {"a": 1}


def test_coerce_does_not_mutate_input() -> None:
    arguments = {"v": 5}
    coerce_to_schema(arguments, {"properties": {"v": {"type": "string"}}}, {"v": "5"})
    assert arguments == {"v": 5}


def test_already_string_values_are_untouched() -> None:
    schema = {"properties": {"v": {"type": "string"}}}
    assert coerce_to_schema({"v": "x"}, schema, {"v": "x"}) == {"v": "x"}


# --- validate_arguments ---------------------------------------------------------------------


def test_valid_arguments_pass() -> None:
    validate_arguments({"query": "dune", "limit": 3, "tags": ["a"]}, SCHEMA, server="bookshop", tool="search")


def test_missing_required_property() -> None:
    with pytest.raises(ArgumentValidationError) as info:
        validate_arguments({}, SCHEMA, server="bookshop", tool="search_books")
    err = info.value
    assert (
        str(err.message)
        == "invalid arguments for bookshop/search_books:\n  - query: 'query' is a required property"
    )
    assert err.detail == [{"path": "query", "message": "'query' is a required property"}]
    assert err.exit_code == ExitCode.USAGE
    assert err.server == "bookshop"


def test_all_errors_are_collected_in_stable_order() -> None:
    with pytest.raises(ArgumentValidationError) as info:
        validate_arguments({"limit": "x", "tags": ["ok", 3]}, SCHEMA, server="s", tool="t")
    lines = info.value.message.splitlines()
    assert lines[0] == "invalid arguments for s/t:"
    assert lines[1:] == [
        "  - limit: 'x' is not of type 'integer'",
        "  - query: 'query' is a required property",
        "  - tags[1]: 3 is not of type 'string'",
    ]
    assert [d["path"] for d in info.value.detail] == ["limit", "query", "tags[1]"]


def test_constraint_violation_message() -> None:
    with pytest.raises(ArgumentValidationError, match=r"limit: 0 is less than the minimum of 1"):
        validate_arguments({"query": "q", "limit": 0}, SCHEMA, server="s", tool="t")


def test_nested_path() -> None:
    schema = {
        "type": "object",
        "properties": {
            "filter": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        },
    }
    with pytest.raises(ArgumentValidationError) as info:
        validate_arguments({"filter": {}}, schema, server="s", tool="t")
    assert info.value.detail == [{"path": "filter.n", "message": "'n' is a required property"}]


def test_additional_properties_false_reports_root() -> None:
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    with pytest.raises(ArgumentValidationError) as info:
        validate_arguments({"x": 1}, schema, server="s", tool="t")
    assert info.value.detail[0]["path"] == "(root)"
    assert "x" in info.value.detail[0]["message"]


def test_boolean_is_not_an_integer() -> None:
    with pytest.raises(ArgumentValidationError, match="limit: True is not of type 'integer'"):
        validate_arguments({"query": "q", "limit": True}, SCHEMA, server="s", tool="t")


def test_invalid_schema_is_skipped_silently() -> None:
    validate_arguments({"a": 1}, {"type": "no-such-type"}, server="s", tool="t")
    validate_arguments({"a": 1}, {"properties": 5}, server="s", tool="t")


def test_unresolvable_ref_is_skipped_silently() -> None:
    schema = {"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}
    validate_arguments({"a": 1}, schema, server="s", tool="t")


def test_empty_schema_accepts_anything() -> None:
    validate_arguments({"a": 1}, {}, server="s", tool="t")


def test_local_ref_is_resolved() -> None:
    schema = {
        "type": "object",
        "properties": {"p": {"$ref": "#/$defs/pos"}},
        "$defs": {"pos": {"type": "integer", "minimum": 1}},
    }
    validate_arguments({"p": 2}, schema, server="s", tool="t")
    with pytest.raises(ArgumentValidationError, match="p: 0 is less than the minimum of 1"):
        validate_arguments({"p": 0}, schema, server="s", tool="t")


def test_draft_07_schema_is_honoured() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"n": {"type": "integer"}},
    }
    with pytest.raises(ArgumentValidationError):
        validate_arguments({"n": "x"}, schema, server="s", tool="t")


def test_very_long_messages_are_truncated() -> None:
    schema = {"type": "object", "properties": {"v": {"enum": ["a"]}}}
    with pytest.raises(ArgumentValidationError) as info:
        validate_arguments({"v": "z" * 2000}, schema, server="s", tool="t")
    assert len(info.value.detail[0]["message"]) <= 300


def test_end_to_end_with_coercion() -> None:
    schema = {"type": "object", "properties": {"zip": {"type": "string"}}, "required": ["zip"]}
    parsed = parse_tool_arguments(None, ["zip=02134"])
    coerced = coerce_to_schema(parsed, schema, split_pairs(["zip=02134"]))
    assert coerced == {"zip": "02134"}
    validate_arguments(coerced, schema, server="s", tool="t")
