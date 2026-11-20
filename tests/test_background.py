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
    assert [item["title"] for item in listed(ctx())["items"]] == ["stretch"]  # type: ignore[call-arg]
    await found.close()


async def test_a_job_cannot_post_into_its_own_session(tmp_path):
    found, _ = background(tmp_path)
    remind = {tool.name: tool for tool in found.tools()}["remind_me"].function
    result = await remind(ctx(bud_task_id=1), text="x", minutes_from_now=1)  # type: ignore[call-arg]
    assert "main conversation" in result["error"]
