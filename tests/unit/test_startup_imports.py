"""Import hygiene: the CLI process must not load the MCP SDK client/server stack unless it connects."""

from __future__ import annotations

import json
import subprocess
import sys

_HEAVY = ["mcp", "mcp.client", "mcp.server", "httpx2", "starlette", "uvicorn", "jsonschema"]


def _loaded_after(statements: str, modules: list[str]) -> list[str]:
    """Run `statements` in a fresh interpreter; return the names from `modules` that ended up imported."""
    report = f"import json\nprint(json.dumps([m for m in {modules!r} if m in sys.modules]))"
    code = f"import sys\n{statements}\n{report}"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_importing_the_cli_app_does_not_load_the_sdk() -> None:
    assert _loaded_after("import mcpc.cli.app", _HEAVY) == []


def test_building_the_command_tree_does_not_load_the_sdk() -> None:
    # Touch the Typer app and render --help, which resolves every command definition.
    code = (
        "import mcpc.cli.app as m\n"
        "from typer.main import get_command\n"
        "cmd = get_command(m.app)\n"
        "assert 'list' in cmd.commands and 'call' in cmd.commands and 'daemon' in cmd.commands\n"
    )
    assert _loaded_after(code, _HEAVY) == []


def test_running_version_does_not_load_the_catalog_code() -> None:
    code = (
        "import sys\n"
        "from typer.testing import CliRunner\n"
        "from mcpc.cli.app import app\n"
        "assert CliRunner().invoke(app, ['--version']).exit_code == 0\n"
    )
    assert _loaded_after(code, [*_HEAVY, "mcp_types", "mcpc.catalog.models", "mcpc.cli.context"]) == []


def test_daemon_client_and_catalog_store_do_not_load_the_server_stack() -> None:
    code = "import mcpc.daemon.client, mcpc.catalog.store"
    assert _loaded_after(code, ["mcp", "mcp.client", "mcp.server", "httpx2", "starlette", "uvicorn"]) == []


def test_direct_executor_still_loads_the_sdk_when_used() -> None:
    # Guards against the lazy imports silently breaking the direct-mode path.
    code = "from mcpc.client.direct import DirectExecutor"
    assert "mcp" in _loaded_after(code, ["mcp"])


def test_jsonschema_loads_only_when_validating() -> None:
    code = (
        "from mcpc.arguments import validate_arguments\n"
        "assert 'jsonschema' not in sys.modules\n"
        "validate_arguments({}, {'type': 'object'}, server='s', tool='t')\n"
    )
    assert _loaded_after(code, ["jsonschema"]) == ["jsonschema"]
