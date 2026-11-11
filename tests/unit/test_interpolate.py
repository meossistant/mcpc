"""Tests for mcpc.config.interpolate: templates, resolve(), redaction."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcpc.config.interpolate import find_references, interpolate_str, redact_spec, resolve
from mcpc.config.models import HttpServer, ServerConfig, Settings, StdioServer
from mcpc.errors import ConfigError

ROOT = Path("/work/proj")
ENV = {"TOKEN": "s3cret", "EMPTY": "", "NAME": "bob"}


def interp(value: str, env: dict[str, str] | None = None) -> str:
    return interpolate_str(value, workspace_root=ROOT, env=ENV if env is None else env)


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("${env:TOKEN}", "s3cret"),
        ("${TOKEN}", "s3cret"),
        ("Bearer ${env:TOKEN}", "Bearer s3cret"),
        ("${NAME}-${env:NAME}", "bob-bob"),
        ("${env:MISSING:-fallback}", "fallback"),
        ("${MISSING:-fallback}", "fallback"),
        ("${MISSING:-}", ""),
        ("${env:TOKEN:-fallback}", "s3cret"),
        ("${TOKEN:-fallback}", "s3cret"),
        ("${EMPTY}", ""),
        ("${MISSING:-a:-b}", "a:-b"),
        ("plain text", "plain text"),
        ("", ""),
        ("$notatemplate and $", "$notatemplate and $"),
        ("${unterminated", "${unterminated"),
    ],
)
def test_env_syntaxes(template: str, expected: str) -> None:
    assert interp(template) == expected


def test_builtins() -> None:
    assert interp("${workspaceFolder}/src") == f"{ROOT}/src"
    assert interp("${userHome}") == str(Path.home())


def test_escape_gives_literal() -> None:
    assert interp("$${env:TOKEN}") == "${env:TOKEN}"
    assert interp("a $${ b ${NAME}") == "a ${ b bob"
    assert interp("$${workspaceFolder}") == "${workspaceFolder}"


def test_unset_variable_error_does_not_leak() -> None:
    with pytest.raises(ConfigError) as info:
        interpolate_str("x ${env:NOPE} y", workspace_root=ROOT, env=ENV, server="srv")
    assert info.value.message == "environment variable NOPE is not set"
    assert info.value.server == "srv"
    assert "s3cret" not in str(info.value)


def test_unset_without_env_prefix() -> None:
    with pytest.raises(ConfigError, match="NOPE is not set"):
        interp("${NOPE}")


def test_error_does_not_include_other_resolved_values() -> None:
    with pytest.raises(ConfigError) as info:
        interp("${TOKEN} ${NOPE}")
    assert "s3cret" not in info.value.message


@pytest.mark.parametrize("template", ["${foo:bar}", "${env:}", "${}", "${a b}", "${1X}", "${env:A-B}"])
def test_unknown_forms_raise(template: str) -> None:
    with pytest.raises(ConfigError, match="unsupported template"):
        interp(template)


def test_find_references() -> None:
    assert find_references("a ${env:X} ${workspaceFolder} $${skip} ${Y:-d}") == [
        "env:X",
        "workspaceFolder",
        "Y:-d",
    ]
    assert find_references("nothing") == []


# --- resolve -------------------------------------------------------------------------------------


def make_cfg(spec: StdioServer | HttpServer, **kw: object) -> ServerConfig:
    return ServerConfig(name="srv", spec=spec, scope="workspace", source=Path("mcp.json"), **kw)  # type: ignore[arg-type]


def test_resolve_stdio() -> None:
    cfg = make_cfg(
        StdioServer(
            command="${env:BIN:-node}",
            args=["--root", "${workspaceFolder}", "--user", "${NAME}", "$${raw}"],
            env={"API_KEY": "${env:TOKEN}", "STATIC": "1"},
            cwd="${workspaceFolder}/sub",
        )
    )
    out = resolve(cfg, workspace_root=ROOT, settings=Settings(), env=ENV)
    spec = out.spec
    assert isinstance(spec, StdioServer)
    assert spec.command == "node"
    assert spec.args == ["--root", str(ROOT), "--user", "bob", "${raw}"]
    assert spec.env == {"API_KEY": "s3cret", "STATIC": "1"}
    assert spec.cwd == f"{ROOT}/sub"
    assert out.config is cfg
    # The original config keeps its templates.
    assert isinstance(cfg.spec, StdioServer) and cfg.spec.env["API_KEY"] == "${env:TOKEN}"


def test_resolve_http() -> None:
    cfg = make_cfg(
        HttpServer(
            url="https://${env:NAME}.example/mcp",
            transport="sse",
            headers={"Authorization": "Bearer ${env:TOKEN}"},
        )
    )
    spec = resolve(cfg, workspace_root=ROOT, settings=Settings(), env=ENV).spec
    assert isinstance(spec, HttpServer)
    assert spec.url == "https://bob.example/mcp"
    assert spec.transport == "sse"
    assert spec.headers == {"Authorization": "Bearer s3cret"}


def test_resolve_cwd_none_stays_none() -> None:
    out = resolve(make_cfg(StdioServer(command="x")), workspace_root=ROOT, settings=Settings(), env={})
    assert isinstance(out.spec, StdioServer) and out.spec.cwd is None


def test_resolve_timeouts() -> None:
    settings = Settings(connectTimeout=7, requestTimeout=120)  # type: ignore[call-arg]
    default = resolve(make_cfg(StdioServer(command="x")), workspace_root=ROOT, settings=settings, env={})
    assert (default.connect_timeout, default.request_timeout) == (7, 120)
    custom = resolve(
        make_cfg(StdioServer(command="x"), timeout=15), workspace_root=ROOT, settings=settings, env={}
    )
    assert (custom.connect_timeout, custom.request_timeout) == (7, 15)


def test_resolve_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPC_TEST_VAR", "from-os")
    cfg = make_cfg(StdioServer(command="x", env={"V": "${MCPC_TEST_VAR}"}))
    spec = resolve(cfg, workspace_root=ROOT, settings=Settings()).spec
    assert isinstance(spec, StdioServer) and spec.env == {"V": "from-os"}


def test_resolve_unset_names_server_without_leak() -> None:
    cfg = make_cfg(StdioServer(command="x", args=["${TOKEN}"], env={"K": "${env:MISSING}"}))
    with pytest.raises(ConfigError) as info:
        resolve(cfg, workspace_root=ROOT, settings=Settings(), env=ENV)
    assert info.value.server == "srv"
    assert info.value.message == "environment variable MISSING is not set"
    assert "s3cret" not in str(info.value)


# --- redaction -----------------------------------------------------------------------------------


def test_redact_http_headers_and_userinfo() -> None:
    spec = HttpServer(
        url="https://user:pass@host.example/mcp?x=1",
        headers={
            "Authorization": "Bearer abc123",
            "X-Token": "${env:TOKEN}",
            "X-Mixed": "Bearer ${env:TOKEN}",
            "X-Plain": "hello",
        },
    )
    out = redact_spec(spec)
    assert out["url"] == "https://***@host.example/mcp?x=1"
    assert out["headers"] == {
        "Authorization": "***",
        "X-Token": "${env:TOKEN}",
        "X-Mixed": "***",
        "X-Plain": "***",
    }
    assert out["transport"] == "streamable-http"
    assert "abc123" not in repr(out) and "pass" not in repr(out)


def test_redact_url_without_userinfo_unchanged() -> None:
    url = "http://localhost:9000/sse?a=b@c"
    assert redact_spec(HttpServer(url=url))["url"] == url


def test_redact_stdio_env_args_unchanged() -> None:
    spec = StdioServer(
        command="npx",
        args=["-y", "pkg", "--token=visible"],
        env={"KEY": "plain-secret", "FROM_ENV": "${env:KEY}", "LOG": "warn"},
        cwd="${workspaceFolder}",
    )
    out = redact_spec(spec)
    assert out["command"] == "npx"
    assert out["args"] == ["-y", "pkg", "--token=visible"]
    assert out["env"] == {"KEY": "***", "FROM_ENV": "${env:KEY}", "LOG": "***"}
    assert out["cwd"] == "${workspaceFolder}"
    assert "plain-secret" not in repr(out)


def test_redact_does_not_mutate_spec() -> None:
    spec = HttpServer(url="http://u:p@h/", headers={"A": "b"})
    redact_spec(spec)
    assert spec.url == "http://u:p@h/" and spec.headers == {"A": "b"}
