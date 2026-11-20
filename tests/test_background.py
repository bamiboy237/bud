"""Reminders and background commands: run by the server, posted to the chat without a
model turn, and kept across restarts."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from bud.core.background import Background


class Hub:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []
        self.keys: set[str | None] = set()

    def publish_once(self, session_id, type, data=None, *, key=None):
        if key in self.keys:
            return None
        self.keys.add(key)
        self.events.append((session_id, type, data or {}))
        return True


def ctx(session: str = "chat", **metadata: Any) -> RunContext[None]:
    return RunContext(
        deps=None,
        model=TestModel(),
        usage=RunUsage(),
        metadata={"bud_session_id": session, "bud_request": "build it", **metadata},
    )


def background(tmp_path, run_shell=None, now=None) -> tuple[Background, Hub]:
    hub = Hub()
    found = Background(
        tmp_path / "background.db",
        tmp_path / "workspace",
        run_shell,
        **({"now": now} if now else {}),
    )
    (tmp_path / "workspace").mkdir(exist_ok=True)
    found.bind(hub)
    return found, hub


async def until(check, seconds: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_a_reminder_posts_its_text_when_it_is_due(tmp_path):
    found, hub = background(tmp_path)
    found.remind("chat", "stretch", datetime.now(UTC) + timedelta(milliseconds=50))
    assert hub.events == []
    await until(lambda: hub.events)
    [(session, kind, data)] = hub.events
    assert (session, kind) == ("chat", "background.finished")
    assert data["text"] == "Reminder: stretch"
    assert found.items("chat") == []


async def test_a_cancelled_reminder_never_posts(tmp_path):
    found, hub = background(tmp_path)
    item = found.remind("chat", "stretch", datetime.now(UTC) + timedelta(milliseconds=50))
    assert not found.cancel("another-chat", item.id)
    assert found.cancel("chat", item.id)
    await asyncio.sleep(0.15)
    assert hub.events == []
    assert not found.cancel("chat", item.id)


async def test_a_reminder_survives_a_restart_and_says_it_is_late(tmp_path):
    found, _ = background(tmp_path)
    found.remind("chat", "call Sam", datetime.now(UTC) + timedelta(hours=1))
    await found.close()  # Bud stops

    later = datetime.now(UTC) + timedelta(hours=2)
    restarted, hub = background(tmp_path, now=lambda: later)
    await restarted.resume()
    await until(lambda: hub.events)
    [(_, _, data)] = hub.events
    assert data["text"].startswith("Reminder: call Sam (late: Bud was not running at ")


async def test_a_command_reports_its_result_and_saves_its_log(tmp_path):
    seen: list[tuple[str, float, str]] = []

    async def run_shell(command: str, timeout: float, request: str) -> dict[str, Any]:
        seen.append((command, timeout, request))
        return {"exit_code": 0, "stdout": "step 1\nstep 2\nbuilt report.pdf\n", "stderr": ""}

    found, hub = background(tmp_path, run_shell)
    tools = {tool.name: tool for tool in found.tools()}
    started = await tools["run_in_background"].function(  # type: ignore[call-arg]
        ctx(), title="Build the report", command="make report", timeout_minutes=5
    )
    assert started["started"]["status"] == "running"
    await until(lambda: hub.events)

    assert seen == [("make report", 300, "build it")]
    [(_, _, data)] = hub.events
    assert data["status"] == "done"
    assert data["text"].startswith("Finished “Build the report”.")
    assert "built report.pdf" in data["text"]
    log = tmp_path / "workspace" / "background" / f"{started['started']['id']}.log"
    assert data["report_path"] == str(log)
    assert "step 1" in log.read_text()


async def test_a_failing_command_says_so(tmp_path):
    async def run_shell(command: str, timeout: float, request: str) -> dict[str, Any]:
        return {"exit_code": 2, "stdout": "", "stderr": "make: *** no rule\n"}

    found, hub = background(tmp_path, run_shell)
    found.start("chat", "Build", "make nothing", 60, "")
    await until(lambda: hub.events)
    [(_, _, data)] = hub.events
    assert data["status"] == "failed"
    assert "failed with exit code 2" in data["text"]
    assert "no rule" in data["text"]


async def test_a_command_a_restart_cut_off_is_reported_and_not_run_again(tmp_path):
    release = asyncio.Event()
    runs = 0

    async def run_shell(command: str, timeout: float, request: str) -> dict[str, Any]:
        nonlocal runs
        runs += 1
        await release.wait()
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    found, _ = background(tmp_path, run_shell)
    found.start("chat", "Download data", "curl ...", 600, "")
    await asyncio.sleep(0.05)
    await found.close()

    restarted, hub = background(tmp_path, run_shell)
    await restarted.resume()
    [(_, _, data)] = hub.events
    assert data["status"] == "interrupted"
    assert "not run again" in data["text"]
    assert runs == 1


async def test_cancelling_a_command_stops_it_quietly(tmp_path):
    stopped = asyncio.Event()

    async def run_shell(command: str, timeout: float, request: str) -> dict[str, Any]:
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()
        return {}

    found, hub = background(tmp_path, run_shell)
    item = found.start("chat", "Long job", "sleep 10", 600, "")
    await asyncio.sleep(0.05)
    assert found.cancel("chat", item.id)
    await asyncio.wait_for(stopped.wait(), 1)
    assert hub.events == []


async def test_only_three_commands_run_at_once(tmp_path):
    async def run_shell(command: str, timeout: float, request: str) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {}

    found, _ = background(tmp_path, run_shell)
    for number in range(3):
        found.start("chat", f"Job {number}", "sleep 10", 600, "")
    with pytest.raises(ValueError, match="already running"):
        found.start("chat", "One more", "sleep 10", 600, "")
    await found.close()


async def test_remind_me_checks_its_time(tmp_path):
    found, _ = background(tmp_path)
    remind = {tool.name: tool for tool in found.tools()}["remind_me"].function

    assert "exactly one" in (await remind(ctx(), text="x"))["error"]  # type: ignore[call-arg]
    assert (
        "exactly one"
        in (
            await remind(ctx(), text="x", minutes_from_now=5, at="2030-01-01T09:00:00+00:00")  # type: ignore[call-arg]
        )["error"]
    )
    assert "offset" in (await remind(ctx(), text="x", at="2030-01-01T09:00:00"))["error"]  # type: ignore[call-arg]
    assert "passed" in (await remind(ctx(), text="x", at="2020-01-01T09:00:00+00:00"))["error"]  # type: ignore[call-arg]
    assert "30 days" in (await remind(ctx(), text="x", minutes_from_now=60 * 24 * 31))["error"]  # type: ignore[call-arg]
    made = await remind(ctx(), text="stretch", minutes_from_now=2)  # type: ignore[call-arg]
    assert made["scheduled"]["title"] == "stretch"
    listed = {tool.name: tool for tool in found.tools()}["list_background"].function
    items = (await listed(ctx()))["items"]  # type: ignore[call-arg]
    assert [item["title"] for item in items] == ["stretch"]
    await found.close()


async def test_a_job_cannot_post_into_its_own_session(tmp_path):
    found, _ = background(tmp_path)
    remind = {tool.name: tool for tool in found.tools()}["remind_me"].function
    result = await remind(ctx(bud_task_id=1), text="x", minutes_from_now=1)  # type: ignore[call-arg]
    assert "main conversation" in result["error"]


async def test_the_log_is_never_written_through_a_link(tmp_path):
    async def run_shell(command: str, timeout: float, request: str) -> dict[str, Any]:
        return {"exit_code": 0, "stdout": "payload", "stderr": ""}

    found, hub = background(tmp_path, run_shell)
    target = tmp_path / "outside.txt"
    target.write_text("keep me")
    # A sandboxed command could plant this link where the next log goes.
    (tmp_path / "workspace" / "background").mkdir()
    (tmp_path / "workspace" / "background" / "1.log").symlink_to(target)
    found.start("chat", "Build", "make", 60, "build it")
    await until(lambda: hub.events)
    assert target.read_text() == "keep me"
    [(_, _, data)] = hub.events
    assert "report_path" not in data

    # The whole folder swapped for a link is refused too.
    (tmp_path / "workspace" / "background" / "1.log").unlink()
    (tmp_path / "workspace" / "background").rmdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "workspace" / "background").symlink_to(tmp_path / "elsewhere")
    found.start("chat", "Build again", "make", 60, "build it")
    await until(lambda: len(hub.events) == 2)
    assert list((tmp_path / "elsewhere").iterdir()) == []


async def test_only_the_owners_own_turn_can_start_work(tmp_path):
    async def run_shell(command: str, timeout: float, request: str) -> dict[str, Any]:
        return {"exit_code": 0}

    found, _ = background(tmp_path, run_shell)
    tools = {tool.name: tool.function for tool in found.tools()}
    job = ctx(bud_task_id=3)
    assert (
        "main conversation"
        in (
            await tools["run_in_background"](job, title="x", command="x")  # type: ignore[call-arg]
        )["error"]
    )
    # A job report or other turn the owner did not write carries no request.
    report = ctx(bud_request=None)
    assert (
        "Only the person"
        in (
            await tools["run_in_background"](report, title="x", command="x")  # type: ignore[call-arg]
        )["error"]
    )
    assert (
        "Only the person"
        in (
            await tools["remind_me"](report, text="x", minutes_from_now=1)  # type: ignore[call-arg]
        )["error"]
    )
    assert found.items("chat") == []


async def test_a_replayed_call_starts_its_work_once(tmp_path):
    runs = 0

    async def run_shell(command: str, timeout: float, request: str) -> dict[str, Any]:
        nonlocal runs
        runs += 1
        return {"exit_code": 0}

    found, hub = background(tmp_path, run_shell)
    start = {tool.name: tool.function for tool in found.tools()}["run_in_background"]
    call = ctx()  # same run and tool call id, as a crash replay would have
    first = await start(call, title="Build", command="make")  # type: ignore[call-arg]
    again = await start(call, title="Build", command="make")  # type: ignore[call-arg]
    assert first["started"]["id"] == again["started"]["id"]
    await until(lambda: hub.events)
    await asyncio.sleep(0.05)
    assert runs == 1


async def test_the_cancel_tool_stops_a_running_command(tmp_path):
    stopped = asyncio.Event()

    async def run_shell(command: str, timeout: float, request: str) -> dict[str, Any]:
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()
        return {}

    found, _ = background(tmp_path, run_shell)
    tools = {tool.name: tool.function for tool in found.tools()}
    started = await tools["run_in_background"](ctx(), title="Long", command="x")  # type: ignore[call-arg]
    await asyncio.sleep(0.02)
    result = await tools["cancel_background"](ctx(), id=started["started"]["id"])  # type: ignore[call-arg]
    assert result == {"cancelled": started["started"]["id"]}
    await asyncio.wait_for(stopped.wait(), 1)


async def test_stop_ends_commands_and_keeps_reminders(tmp_path):
    async def run_shell(command: str, timeout: float, request: str) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {}

    found, _ = background(tmp_path, run_shell)
    found.start("chat", "Long", "sleep 10", 600, "build it")
    found.remind("chat", "stretch", datetime.now(UTC) + timedelta(hours=1))
    assert found.stop_commands("chat") == 1
    assert [item.kind for item in found.items("chat")] == ["reminder"]
    await found.close()


async def test_a_command_that_cannot_start_frees_its_slot(tmp_path):
    async def run_shell(command: str, timeout: float, request: str) -> dict[str, Any]:
        raise OSError("no file descriptors")

    found, hub = background(tmp_path, run_shell)
    for _ in range(4):  # more than the cap: each failure frees its slot
        found.start("chat", "Build", "make", 60, "build it")
        await until(lambda: hub.events and hub.events[-1][2]["status"] == "failed")
    assert all("could not run" in data["text"] for _, _, data in hub.events)


async def test_a_reminder_follows_the_wall_clock_through_sleep(tmp_path, monkeypatch):
    from bud.core import background as module

    monkeypatch.setattr(module, "CHECK_SECONDS", 0.02)
    start = datetime.now(UTC)
    asleep = {"done": False}

    def now() -> datetime:
        # The Mac slept for two hours: the wall clock jumped, the loop's clock did not.
        return start + timedelta(hours=2) if asleep["done"] else start

    found, hub = background(tmp_path, now=now)
    found.remind("chat", "call Sam", start + timedelta(hours=1))
    await asyncio.sleep(0.05)
    assert hub.events == []
    asleep["done"] = True
    await until(lambda: hub.events, seconds=1)


@pytest.mark.parametrize(
    ("mode", "reviewer", "expected"),
    [
        ("strict", True, "ask"),
        ("auto", False, "ask"),
        ("auto", True, "review"),
    ],
)
def test_starting_work_is_an_approval_like_any_write(mode, reviewer, expected):
    from bud.core.profiles import apply_mode
    from bud.core.server import allow_file_tools
    from bud.harness.actions import Policy

    policy = Policy(tools={})
    allow_file_tools(policy)
    apply_mode(policy, mode, reviewer=reviewer)
    assert policy.tools["remind_me"] == expected
    assert policy.tools["run_in_background"] == expected
    assert policy.tools["list_background"] == "allow"
    assert policy.tools["cancel_background"] == "allow"
