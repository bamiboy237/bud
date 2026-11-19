"""Bud's browser tools come from OpenCode Browse. A stand-in `browse` program plays its part:
an MCP server with two tools, a pairing code, and a status report."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from bud import browser
from bud.core.profiles import apply_mode
from bud.harness.actions import Policy

STAND_IN = """\
#!{python}
import json, sys
from pathlib import Path
here = Path(__file__).parent
if sys.argv[1:2] == ["mcp"]:
    from mcp.server.mcpserver import MCPServer
    server = MCPServer("browse")

    @server.tool()
    def status() -> str:
        "Report the relay"
        return "connected"

    @server.tool()
    def tabs(action: str) -> str:
        "List, open or close tabs"
        return f"tabs {{action}}"

    @server.tool()
    def execute(code: str) -> str:
        "Run JavaScript that drives the browser"
        (here / "scripts").open("a").write(code + "\\n")
        return f"ran {{len(code)}} characters"

    server.run()
elif sys.argv[1:2] == ["pair"]:
    print("Pairing code: AB12-CD34", flush=True)
elif sys.argv[1:3] == ["status", "--json"]:
    print(json.dumps({{"extension": {{"connected": (here / "paired").exists()}}}}))
"""


@pytest.fixture
def browse(tmp_path):
    path = tmp_path / "browse"
    path.write_text(STAND_IN.format(python=sys.executable))
    path.chmod(0o755)
    return path


def test_bud_prefers_its_own_copy_then_the_owners(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(browser.shutil, "which", lambda name: "/usr/local/bin/browse")
    assert browser.executable() == Path("/usr/local/bin/browse")
    own = tmp_path / ".bud" / "bin" / "browse"
    own.parent.mkdir(parents=True)
    own.write_text("")
    assert browser.executable() == own
    own.unlink()
    monkeypatch.setattr(browser.shutil, "which", lambda name: None)
    assert browser.executable() is None


def test_status_and_tabs_run_unasked_and_scripts_are_checked():
    policy = Policy(tools={})
    browser.allow_tools(policy)
    assert policy.decide("browser_status") == "allow"
    assert policy.decide("browser_tabs") == "allow"
    assert policy.decide("browser_execute") == "ask"
    # A tool a future Browse adds is refused until Bud names it.
    assert policy.decide("browser_purchase") == "deny"
    # In auto mode the approval reviewer judges each script instead of asking.
    apply_mode(policy, "auto", reviewer=True)
    assert policy.decide("browser_execute") == "review"
    apply_mode(policy, "strict", reviewer=True)
    assert policy.decide("browser_execute") == "ask"


async def test_browse_tools_load_on_demand_with_the_browser_prefix(browse):
    toolset = browser.toolset(browse)
    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    async with toolset:
        tools = await toolset.get_tools(ctx)
        ran = await toolset.call_tool(
            "browser_execute", {"code": "return 1"}, ctx, tools["browser_execute"]
        )
    assert set(tools) == {"browser_status", "browser_tabs", "browser_execute"}
    assert set(tools) == browser.READ_TOOLS | browser.ACTION_TOOLS
    assert all(tool.tool_def.defer_loading for tool in tools.values())
    assert ran == "ran 8 characters"


def test_the_setup_card_shows_a_pairing_code_and_waits_for_the_extension(browse):
    entry = browser.setup_entry(find=lambda: browse)
    started = entry.initiate()
    assert started.kind == "steps"
    assert browser.EXTENSION_URL in started.steps[0]
    assert "AB12-CD34" in started.steps[1]
    assert entry.status().state == "waiting"
    (browse.parent / "paired").touch()
    assert entry.status().state == "connected"


def test_without_browse_the_card_explains_how_to_install_it():
    entry = browser.setup_entry(find=lambda: None)
    assert browser.INSTALL in entry.initiate().steps[0]
    assert entry.status().state == "waiting"


def test_the_stand_in_answers_status(browse):
    result = subprocess.run([str(browse), "status", "--json"], capture_output=True, text=True)
    assert json.loads(result.stdout) == {"extension": {"connected": False}}
