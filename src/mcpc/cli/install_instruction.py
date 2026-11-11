"""`mcpc install-instruction`: tell an AI agent how to install the mcpc skill.

The skill file ships inside the package (`mcpc/skill/SKILL.md`), so this works from any install, not
only from a source checkout. Standard library only; cheap to import.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

from mcpc import paths
from mcpc.cli.context import Ctx
from mcpc.errors import ProtocolFailedError

SKILL_NAME = "mcpc"
SKILL_COMMAND = "mcpc install-instruction --skill"
INSTALL_URL = "git+https://github.com/meossistant/mcpc.git"


def skill_text() -> str:
    """The packaged SKILL.md, with LF line endings."""
    try:
        return resources.files("mcpc").joinpath("skill", "SKILL.md").read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError) as exc:  # pragma: no cover - broken install
        raise ProtocolFailedError(f"the mcpc skill file is missing from this installation: {exc}") from exc


def agent_user_skill_path() -> Path:
    """Where AI agents look for user-wide skills: ~/.agents/skills/<name>/SKILL.md."""
    return Path.home() / ".agents" / "skills" / SKILL_NAME / "SKILL.md"


def instruction_text(skill_path: Path, global_config: Path) -> str:
    """Markdown an agent can follow to install the skill. ASCII only."""
    return f"""\
# Install the mcpc skill

The `{SKILL_NAME}` skill teaches you to find and call tools on MCP servers with the `mcpc` command line.
Follow these steps yourself. None of them needs the user, except where a step says to tell them.

1. Check that mcpc runs: `mcpc --version`.
   If the command is not found, stop and tell the user to install it:
   `uv tool install {INSTALL_URL}`

2. Choose where the skill goes. A skill is a folder named `{SKILL_NAME}` that contains one file, `SKILL.md`,
   inside your agent's skills directory.
   - User-wide: {skill_path}
   - Any other agent: its skills directory, which is the folder that already holds other
     `<name>/SKILL.md` skills. If you cannot tell where that is, ask the user.
   If `{SKILL_NAME}/SKILL.md` already exists there, replace it with this version and tell the user.

3. Get the file content by running: `{SKILL_COMMAND}`
   It prints the complete SKILL.md. Write it exactly as printed to the path from step 2, using your
   file-write tool (UTF-8, no edits). Do not redirect with `>` in Windows PowerShell 5.1: it writes
   UTF-16 and breaks the file.

4. Verify: read the file back. Its first line must be `---` and its second line `name: {SKILL_NAME}`.

5. Tell the user the skill is installed and where. Some agents load skills only in a new session.
   Then run `mcpc list`. If it says no servers are configured, tell the user to add MCP servers to
   `{global_config}` (`mcpc config path` prints the locations). Do not create or edit that file unless
   the user asks you to.

Rules:
- Never run `mcpc trust` or set `MCPC_TRUST_ALL`. Only the user may approve a workspace.
- Do not change the skill text. If it seems wrong, tell the user.
"""


async def _body(c: Ctx, skill: bool) -> None:
    skill_path = agent_user_skill_path()
    if skill:
        text = skill_text()
        c.emit(text=text.rstrip("\n"), data={"name": SKILL_NAME, "content": text})
        return
    text = instruction_text(skill_path, paths.global_config_path())
    data: dict[str, Any] = {
        "skill": SKILL_NAME,
        "targets": {"user": str(skill_path)},
        "skill_command": SKILL_COMMAND,
        "instructions": text,
    }
    c.emit(text=text.rstrip("\n"), data=data)
