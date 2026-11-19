"""Bud's browser tools come from OpenCode Browse. A stand-in `browse` program plays its part:
an MCP server with Browse's three tools, a tab list, a pairing code, and a status report."""

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from bud import browser
from bud.core.profiles import apply_mode
from bud.harness.actions import Policy
from bud.harness.review import MAX_ARGS_CHARS, too_long_to_review

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
        return f"ran {{len(code)}} characters"

    server.run()
elif sys.argv[1:2] == ["tabs"]:
    print(json.dumps({{"tabs": [{{"tab": 3, "id": "shop", "url": "https://shop.example/checkout"}}]}}))
elif sys.argv[1:2] == ["pair"]:
    if (here / "broken").exists():
        print("Something else first", flush=True)
    else:
        print("Pairing code: AB12-CD34", flush=True)
    import time
    time.sleep(30)
elif sys.argv[1:3] == ["status", "--json"]:
    print(json.dumps({{"extension": {{"connected": (here / "paired").exists()}}}}))
"""


@pytest.fixture
def browse(tmp_path):
    path = tmp_path / "browse"
    path.write_text(STAND_IN.format(python=sys.executable))
    path.chmod(0o755)
    return path


def ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


def test_bud_prefers_its_own_runnable_copy_then_the_installers_then_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(browser.shutil, "which", lambda name: "/usr/local/bin/browse")
    assert browser.executable() == Path("/usr/local/bin/browse")
    installed = tmp_path / ".local" / "bin" / "browse"
    installed.parent.mkdir(parents=True)
    installed.write_text("")
    installed.chmod(0o755)
    assert browser.executable() == installed
    own = tmp_path / ".bud" / "bin" / "browse"
    own.parent.mkdir(parents=True)
    own.write_text("")
    # A copy that cannot run is skipped, so one broken file never breaks every chat.
    own.chmod(0o644)
    assert browser.executable() == installed
    own.chmod(0o755)
    assert browser.executable() == own


def test_status_runs_unasked_and_tabs_and_scripts_go_to_the_reviewer():
    policy = Policy(tools={})
    browser.allow_tools(policy)
    assert policy.decide("browser_status") == "allow"
    assert policy.decide("browser_tabs") == "review"
    assert policy.decide("browser_execute") == "review"
    assert policy.decide("disable_browser") == "allow"
    assert policy.decide("enable_browser") == "ask"
    # No standing grant can skip the check or the caution.
    for tool in ("browser_tabs", "browser_execute", "enable_browser"):
        assert not policy.grantable(tool)
    # A tool a future Browse adds is refused until Bud names it.
    assert policy.decide("browser_purchase") == "deny"
    # Switching modes leaves the browser's check in place.
    apply_mode(policy, "strict", reviewer=True)
    assert policy.decide("browser_execute") == "review"


def test_a_call_the_reviewer_would_see_only_part_of_goes_to_the_owner():
    assert not too_long_to_review({"code": "return 1"})
    assert too_long_to_review({"code": "/* padding */" * (MAX_ARGS_CHARS // 10)})


async def test_browser_tools_appear_only_after_the_owner_turns_the_browser_on(browse, tmp_path):
    consent = browser.Consent(tmp_path / "consent.json")
    toolset = browser.toolset(browse, consent)
    controls: dict[str, Any] = {t.name: t.function for t in browser.control_tools(consent)}
    enable, disable = controls["enable_browser"], controls["disable_browser"]
    async with toolset:
        assert await toolset.get_tools(ctx()) == {}
        enable()
        tools = await toolset.get_tools(ctx())
        ran = await toolset.call_tool(
            "browser_execute", {"code": "return 1"}, ctx(), tools["browser_execute"]
        )
        disable()
        assert await toolset.get_tools(ctx()) == {}
    assert set(tools) == browser.TOOLS
    assert all(tool.tool_def.defer_loading for tool in tools.values())
    assert ran == "ran 8 characters"


def test_the_reviewer_sees_where_a_tab_really_is(browse):
    where = browser.describers(find=lambda: browse)["browser_execute"]
    assert where({"tab": "shop", "code": "x"}) == "Tab shop shows https://shop.example/checkout."
    assert where({"tab": 3, "code": "x"}) == "Tab 3 shows https://shop.example/checkout."
    assert "not one Browse knows" in where({"tab": 9})
    opening = browser.describers(find=lambda: browse)["browser_tabs"]
    assert opening({"action": "new", "url": "https://bank.example/"}) == (
        "Opens https://bank.example/."
    )


def test_the_setup_card_cautions_pairs_and_cleans_up(browse):
    pairing = browser.Pairing()
    entry = browser.setup_entry(pairing, find=lambda: browse)
    started = entry.initiate()
    assert started.steps[0] == browser.CAUTION
    assert browser.EXTENSION_URL in started.steps[1]
    assert "AB12-CD34" in started.steps[2]
    process = pairing._process
    assert process is not None
    assert process.poll() is None
    assert entry.status().state == "waiting"
    (browse.parent / "paired").touch()
    assert entry.status().state == "connected"
    pairing.stop()
    assert process.poll() is not None
    assert pairing._process is None


def test_a_failed_pairing_explains_itself_and_leaves_nothing_running(browse):
    (browse.parent / "broken").touch()
    pairing = browser.Pairing()
    entry = browser.setup_entry(pairing, find=lambda: browse)
    with pytest.raises(ConnectionError):
        entry.initiate()
    assert pairing._process is None


def test_without_browse_the_card_cautions_and_explains_how_to_install_it():
    entry = browser.setup_entry(browser.Pairing(), find=lambda: None)
    steps = entry.initiate().steps
    assert steps[0] == browser.CAUTION
    assert browser.INSTALL in steps[1]
    assert entry.status().state == "waiting"


def test_consent_survives_a_restart_and_a_damaged_file_means_off(tmp_path):
    path = tmp_path / "consent.json"
    browser.Consent(path).set(True)
    assert browser.Consent(path).on()
    path.write_text("[1, 2")
    assert not browser.Consent(path).on()
    path.write_text(json.dumps(["on"]))
    assert not browser.Consent(path).on()


def test_the_mac_approval_card_shows_the_same_caution():
    swift = (Path(__file__).parents[1] / "mac/Sources/BudCompanion/Approval.swift").read_text()
    start = swift.index("static let browserCaution =")
    end = swift.index("\n\n", start)
    assert "".join(re.findall(r'"([^"]*)"', swift[start:end])) == browser.CAUTION
