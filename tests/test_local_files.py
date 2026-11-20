"""Reading the owner's files outside the workspace: every call asks, links cannot disguise
what is read, and credential folders stay closed even with approval."""

import os
from pathlib import Path

import pytest

from bud.core.profiles import apply_mode
from bud.core.server import allow_file_tools
from bud.harness.actions import Policy
from bud.workspace import LOCAL_TOOLS, describe_local, local_blocked, local_tools


@pytest.fixture
def home(tmp_path) -> Path:
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / ".ssh").mkdir()
    (home / ".ssh" / "config").write_text("Host secret\n")
    return home


@pytest.fixture
def tools(home, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    blocked = local_blocked(home, [tmp_path / "data"], workspace)
    found = {tool.name: tool.function for tool in local_tools(blocked)}
    return found["read_local_file"], found["list_local_folder"], blocked


def test_reads_a_named_text_file(home, tools):
    read, _, _ = tools
    (home / "Documents" / "plan.txt").write_text("Ship on Friday.\n")
    result = read(str(home / "Documents" / "plan.txt"))  # type: ignore[call-arg]
    assert result["ok"] and result["content"] == "Ship on Friday.\n"
    assert result["path"] == os.path.realpath(home / "Documents" / "plan.txt")


def test_long_text_comes_in_pieces(home, tools):
    read, _, _ = tools
    (home / "Documents" / "long.txt").write_text("a" * 20_000)
    first = read(str(home / "Documents" / "long.txt"))  # type: ignore[call-arg]
    assert len(first["content"]) == 16_000 and first["next_offset"] == 16_000
    rest = read(str(home / "Documents" / "long.txt"), offset=16_000)  # type: ignore[call-arg]
    assert len(rest["content"]) == 4_000 and "next_offset" not in rest


def test_a_csv_comes_back_as_markdown(home, tools):
    read, _, _ = tools
    (home / "Documents" / "budget.csv").write_text("item,cost\nrent,1200\n")
    result = read(str(home / "Documents" / "budget.csv"))  # type: ignore[call-arg]
    assert result["ok"] and "rent" in result["content"] and "|" in result["content"]


@pytest.mark.parametrize(
    "relative",
    [".ssh/config", "Documents/.env", "Documents/secrets.txt", "Documents/id_rsa", ".zshrc"],
)
def test_credentials_and_hidden_files_are_refused(home, tools, relative):
    read, _, _ = tools
    target = home / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")
    result = read(str(target))  # type: ignore[call-arg]
    assert not result["ok"]


def test_blocked_folders_stay_closed_even_through_a_link(home, tools, tmp_path):
    read, _, blocked = tools
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "notes.txt").write_text("bud's own data")
    link = home / "Documents" / "notes.txt"
    link.symlink_to(tmp_path / "data" / "notes.txt")

    result = read(str(link))  # type: ignore[call-arg]
    assert not result["ok"]
    assert "Refused" in describe_local({"path": str(link)}, blocked)
    # The workspace is read with read_file, not here.
    (tmp_path / "workspace" / "a.txt").write_text("x")
    assert not read(str(tmp_path / "workspace" / "a.txt"))["ok"]  # type: ignore[call-arg]


def test_a_path_through_a_link_is_refused_with_its_real_path(home, tools):
    read, _, blocked = tools
    (home / "Documents" / "real.txt").write_text("x")
    (home / "Documents" / "alias.txt").symlink_to(home / "Documents" / "real.txt")
    result = read(str(home / "Documents" / "alias.txt"))  # type: ignore[call-arg]
    assert not result["ok"]
    assert str(home / "Documents" / "real.txt") in result["error"]
    # What the card shows is exactly what is opened.
    assert describe_local({"path": str(home / "Documents" / "real.txt")}, blocked) == (
        f"Opens {home / 'Documents' / 'real.txt'}"
    )


def test_a_blocked_folder_in_another_letter_case_is_refused(tools, tmp_path):
    read, listing, _ = tools
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "notes.txt").write_text("bud's own data")
    if not (tmp_path / "DATA").exists():
        pytest.skip("case-sensitive file system")
    assert not read(str(tmp_path / "DATA" / "notes.txt"))["ok"]  # type: ignore[call-arg]
    assert not listing(str(tmp_path / "DATA"))["ok"]  # type: ignore[call-arg]


def test_a_blocked_folder_through_the_data_volume_is_refused(tools, tmp_path):
    read, _, _ = tools
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "notes.txt").write_text("bud's own data")
    other = Path("/System/Volumes/Data" + str(tmp_path / "data" / "notes.txt"))
    if not other.exists():
        pytest.skip("no /System/Volumes/Data view here")
    assert not read(str(other))["ok"]  # type: ignore[call-arg]


def test_a_hard_link_is_refused(home, tools):
    read, _, _ = tools
    (home / "Documents" / "a.txt").write_text("x")
    os.link(home / "Documents" / "a.txt", home / "Documents" / "b.txt")
    assert not read(str(home / "Documents" / "b.txt"))["ok"]  # type: ignore[call-arg]


def test_relative_paths_are_refused(tools):
    read, _, _ = tools
    assert not read("Documents/plan.txt")["ok"]  # type: ignore[call-arg]


def test_listing_hides_hidden_and_credential_names(home, tools):
    _, listing, _ = tools
    for name in ("plan.pdf", ".DS_Store", "api-key.txt", "token.json"):
        (home / "Documents" / name).write_text("x")
    (home / "Documents" / "Trips").mkdir()
    result = listing(str(home / "Documents"))  # type: ignore[call-arg]
    assert result["entries"] == ["Trips/", "plan.pdf"]


def test_macos_refusal_is_explained(home, tools):
    read, _, _ = tools
    locked = home / "Documents" / "locked.txt"
    locked.write_text("x")
    locked.chmod(0)
    try:
        result = read(str(locked))  # type: ignore[call-arg]
    finally:
        locked.chmod(0o600)
    assert not result["ok"] and "System Settings" in result["error"]


@pytest.mark.parametrize("mode", ["strict", "auto"])
def test_every_local_read_asks_in_every_mode(mode):
    policy = Policy(tools={})
    allow_file_tools(policy)
    apply_mode(policy, mode, reviewer=True)
    for tool in LOCAL_TOOLS:
        assert policy.tools[tool] == "ask"
        assert tool in policy.no_grants


def test_imessage_asks_by_file_name():
    from bud.photon_ui import action_preview

    text = action_preview(
        {
            "approval_id": "a1",
            "tool": "read_local_file",
            "args": {"path": "~/Documents/plan.pdf"},
            "review": {"details": "Opens /Users/me/Documents/plan.pdf"},
        }
    )
    assert text.startswith("Can I read plan.pdf?")
    assert "Opens /Users/me/Documents/plan.pdf" in text


async def test_a_refused_path_is_denied_without_a_card_or_running(home, tools):
    from typing import cast

    from pydantic_ai import RunContext, Tool
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.toolsets import FunctionToolset
    from pydantic_ai.usage import RunUsage

    from bud.harness.actions import ActionGateway
    from bud.workspace import local_refusal

    _, _, blocked = tools
    ran: list[str] = []

    def read_local_file(path: str) -> str:
        ran.append(path)
        return "read"

    policy = Policy(tools={})
    allow_file_tools(policy)
    gateway = ActionGateway(
        FunctionToolset([Tool(read_local_file)]),
        policy=policy,
        record=lambda record: None,
        refuses={"read_local_file": lambda args: local_refusal(args, blocked)},
    )
    ctx = cast(RunContext[None], RunContext(deps=None, model=TestModel(), usage=RunUsage()))
    tools_found = await gateway.get_tools(ctx)
    result = await gateway.call_tool(
        "read_local_file", {"path": "~/.ssh/config"}, ctx, tools_found["read_local_file"]
    )
    assert "hidden or credential" in result
    assert ran == []
