"""CLI tests for mcpc agents, mcpc plugins, and mcpc skills."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mcpc.cli.app import app

runner = CliRunner()


def test_mcpc_agents_cli() -> None:
    result = runner.invoke(app, ["agents"])
    assert result.exit_code == 0, result.output
    assert "Claude Code" in result.stdout
    assert "Antigravity CLI" in result.stdout


def test_mcpc_agents_json() -> None:
    result = runner.invoke(app, ["--json", "agents"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert "agents" in data
    assert any(a["agent"] == "antigravity" for a in data["agents"])


def test_mcpc_plugins_cli() -> None:
    result = runner.invoke(app, ["plugins"])
    assert result.exit_code == 0, result.output
    assert "NAME" in result.stdout
    assert "STATUS" in result.stdout


def test_mcpc_plugins_json() -> None:
    result = runner.invoke(app, ["--json", "plugins"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert "plugins" in data


def test_mcpc_skills_cli() -> None:
    result = runner.invoke(app, ["skills"])
    assert result.exit_code == 0, result.output
    assert "NAME" in result.stdout
    assert "DESCRIPTION" in result.stdout


def test_mcpc_skills_json() -> None:
    result = runner.invoke(app, ["--json", "skills"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert "skills" in data


def test_mcpc_server_import_agent_mode(tmp_path: Path) -> None:
    # Setup mock workspace with .mcp.json
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text(
        json.dumps({
            "mcpServers": {
                "mock-server": {
                    "command": "python",
                    "args": ["-c", "print('ok')"],
                }
            }
        }),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["--workspace", str(tmp_path), "--agent", "claude-code", "--json", "list"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert any(s["name"] == "mock-server" for s in data["servers"])


def test_mcpc_skills_raw_flag() -> None:
    res_dedup = runner.invoke(app, ["--json", "skills"])
    assert res_dedup.exit_code == 0
    data_dedup = json.loads(res_dedup.stdout)

    res_raw = runner.invoke(app, ["--json", "skills", "--raw"])
    assert res_raw.exit_code == 0
    data_raw = json.loads(res_raw.stdout)

    # Raw count should be greater than or equal to deduplicated count
    assert len(data_raw["skills"]) >= len(data_dedup["skills"])


def test_mcpc_plugins_raw_flag() -> None:
    res_dedup = runner.invoke(app, ["--json", "plugins"])
    assert res_dedup.exit_code == 0
    data_dedup = json.loads(res_dedup.stdout)

    res_raw = runner.invoke(app, ["--json", "plugins", "--raw"])
    assert res_raw.exit_code == 0
    data_raw = json.loads(res_raw.stdout)

    assert len(data_raw["plugins"]) >= len(data_dedup["plugins"])
