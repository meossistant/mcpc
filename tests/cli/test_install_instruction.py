"""`mcpc install-instruction`: guide an agent through installing the packaged skill."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mcpc.cli.app import app

runner = CliRunner()


def _packaged_skill() -> str:
    return resources.files("mcpc").joinpath("skill", "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def test_packaged_skill_is_valid_and_plain_ascii() -> None:
    text = _packaged_skill()
    lines = text.splitlines()
    assert lines[0] == "---"
    assert lines[1] == "name: mcpc"
    assert lines[2].startswith("description: ")
    assert lines[3] == "---"
    text.encode("ascii")  # agents write this file; keep it free of encoding surprises


def test_default_output_guides_the_agent(fake_home: Path) -> None:
    result = runner.invoke(app, ["install-instruction"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "mcpc install-instruction --skill" in out
    assert str(fake_home / ".agents" / "skills" / "mcpc" / "SKILL.md") in out
    assert "mcpc --version" in out
    assert "mcpc list" in out
    assert "Never run `mcpc trust`" in out
    out.encode("ascii")


def test_skill_flag_prints_the_exact_file() -> None:
    result = runner.invoke(app, ["install-instruction", "--skill"])
    assert result.exit_code == 0, result.output
    assert result.stdout.rstrip("\n") == _packaged_skill().rstrip("\n")
    assert result.stdout.startswith("---\nname: mcpc\n")


def test_json_outputs() -> None:
    guide = json.loads(runner.invoke(app, ["--json", "install-instruction"]).stdout)
    assert guide["skill"] == "mcpc"
    assert guide["skill_command"] == "mcpc install-instruction --skill"
    assert guide["targets"]["user"].endswith("SKILL.md")
    assert "Install the mcpc skill" in guide["instructions"]

    raw = json.loads(runner.invoke(app, ["--json", "install-instruction", "--skill"]).stdout)
    assert raw == {"name": "mcpc", "content": _packaged_skill()}


def test_works_without_any_config_and_with_a_broken_one(mcpc_home: Path) -> None:
    assert runner.invoke(app, ["install-instruction"]).exit_code == 0
    (mcpc_home / "mcp.json").write_text("{ not json", encoding="utf-8")
    assert runner.invoke(app, ["install-instruction"]).exit_code == 0


def test_following_the_steps_produces_an_identical_installed_file(fake_home: Path) -> None:
    """What an agent does: read --skill output, write it to the agent path, verify the front matter."""
    target = fake_home / ".agents" / "skills" / "mcpc" / "SKILL.md"
    content = runner.invoke(app, ["install-instruction", "--skill"]).stdout
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")
    first_lines = target.read_text(encoding="utf-8").splitlines()[:2]
    assert first_lines == ["---", "name: mcpc"]
