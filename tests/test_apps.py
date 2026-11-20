"""Mac app control: an allowlist, fixed scripts with the owner's values as arguments, reads
that run unasked, and changes that ask."""

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


def tools(tmp_path, scripts: Scripts) -> dict[str, Any]:
    return {tool.name: tool.function for tool in apps.build_tools(tmp_path, scripts)}


async def test_the_owners_words_reach_a_script_only_as_arguments(tmp_path):
    scripts = Scripts()
    found = tools(tmp_path, scripts)
    sneaky = 'x" & (do shell script "rm -rf ~") & "'
    await found["notes_create"](title=sneaky, body="body")  # type: ignore[call-arg]
    await found["reminders_add"](title=sneaky)  # type: ignore[call-arg]
    for script, argv in scripts.calls:
        assert sneaky not in script
        assert sneaky in argv
    assert scripts.calls[0][0] == apps.NOTES_CREATE


async def test_only_allowlisted_apps_open(tmp_path):
    found = tools(tmp_path, Scripts())
    result = await found["open_app"](app="Terminal")  # type: ignore[call-arg]
    assert not result["ok"] and "Notes" in result["error"]


async def test_only_workspace_files_open_in_their_app(tmp_path):
    found = tools(tmp_path, Scripts())
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x")
    for path in ("../outside.txt", "missing.pdf", ".hidden"):
        result = await found["open_in_app"](path=path)  # type: ignore[call-arg]
        assert not result["ok"], path


async def test_a_macos_refusal_says_where_to_allow_it(tmp_path):
    scripts = Scripts((1, "", "execution error: Not authorized to send Apple events (-1743)"))
    result = await tools(tmp_path, scripts)["music_now_playing"]()
    assert not result["ok"] and "Automation" in result["error"]


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
