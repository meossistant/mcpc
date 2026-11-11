"""Regression: an unreachable server must give a connection error (exit 3), not 'unknown tool'."""

from __future__ import annotations

from typer.testing import CliRunner

from mcpc.cli.app import app

runner = CliRunner()


def _invoke(ws, *args: str):
    return runner.invoke(app, ["--no-daemon", "-w", str(ws), *args])


def test_call_unreachable_server_exits_3_even_after_failed_refresh(write_config) -> None:
    ws = write_config(global_servers={"ghost": {"command": "definitely-not-a-command"}})

    # `refresh` records the failure in the cache as an empty, error-only snapshot.
    assert _invoke(ws, "refresh").exit_code == 3

    first = _invoke(ws, "call", "ghost/anything")
    assert first.exit_code == 3, first.output
    assert "unknown tool" not in first.output

    # Same without a prior refresh (nothing cached at all).
    ws2 = write_config(global_servers={"ghost2": {"command": "definitely-not-a-command"}})
    second = _invoke(ws2, "call", "ghost2/anything")
    assert second.exit_code == 3, second.output


def test_prompt_unreachable_server_exits_3(write_config) -> None:
    ws = write_config(global_servers={"ghost": {"url": "http://127.0.0.1:9/mcp"}})
    assert _invoke(ws, "refresh").exit_code == 3
    result = _invoke(ws, "prompt", "ghost/recommend")
    assert result.exit_code == 3, result.output
