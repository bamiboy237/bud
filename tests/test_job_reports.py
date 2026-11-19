"""A job that ends gets a bud turn that tells the owner, without waiting for their next message."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo

from bud.core.log import EventLog
from bud.core.projection import JOB_REPORT
from bud.core.runner import job_updates_note
from bud.core.tasks import WorkItem, report_name, save_report
from tests.runner_support import reply, running_bud

BRIEF = {
    "goal": "Find the renewal date of the lease.",
    "inputs": [],
    "expected_result": "The renewal date.",
}


def last_prompt(messages: list[ModelMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    return part.content
    return ""


def bud_with_one_job(prompts: list[str]):
    """The owner asks, bud starts a job, the job finishes, and bud reports when told to."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if any(tool.name == "finish_job" for tool in info.output_tools):
            summary = {"summary": "The lease renews on 3 March."}
            return ModelResponse(parts=[ToolCallPart("finish_job", summary, tool_call_id="f1")])
        prompt = last_prompt(messages)
        prompts.append(prompt)
        if prompt.startswith(JOB_REPORT):
            return reply("Your lease job is done: it renews on 3 March.")
        if not prompt.startswith("When does my lease renew?"):
            return reply("Nothing else is pending.")
        if isinstance(messages[-1], ModelRequest) and any(
            part.part_kind == "tool-return" for part in messages[-1].parts
        ):
            return reply("I started a job to find it.")
        start = {"title": "Find the renewal date", "brief": BRIEF}
        return ModelResponse(parts=[ToolCallPart("start_task", start, tool_call_id="s1")])

    return model


async def test_a_finished_job_is_reported_without_a_new_message(tmp_path):
    prompts: list[str] = []
    async with running_bud(tmp_path, bud_with_one_job(prompts), jobs=True) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "When does my lease renew?", "message-1")
        await bud.settle()
        events = bud.log.read("owner")

    queued = [e.data for e in events if e.type == "run.queued"]
    assert [run["trigger"] for run in queued] == ["message", "job"]
    report = queued[-1]
    assert report["request"] is None
    assert [e.type for e in events if e.type.startswith("task.")][-1] == "task.completed"
    # The report turn sees the job's result in its job updates.
    assert "The lease renews on 3 March." in prompts[-1]
    completed = [e for e in events if e.type == "run.completed"]
    assert completed[-1].data["run_id"] == report["run_id"]
    transcript = [turn.text for turn in bud.runner.snapshot("owner").transcript]
    assert "Your lease job is done: it renews on 3 March." in transcript
    # A job report is not the owner speaking.
    assert [e.data["text"] for e in events if e.type == "user.message"] == [
        "When does my lease renew?"
    ]


@pytest.mark.parametrize("failed", [False, True])
async def test_recovery_reports_job_that_ended_before_notice_was_published(
    tmp_path, monkeypatch, failed
):
    prompts: list[str] = []
    base_model = bud_with_one_job(prompts)

    def model(messages, info):
        if failed and any(tool.name == "finish_job" for tool in info.output_tools):
            raise RuntimeError("Synthetic worker failure")
        return base_model(messages, info)

    async with running_bud(tmp_path, model, jobs=True) as bud:
        monkeypatch.setattr(bud.runner, "_publish_task", lambda _: None)
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "When does my lease renew?", "message-1")
        await bud.settle()
        assert not any(
            event.type in ("task.completed", "task.failed") for event in bud.log.read("owner")
        )

    async with running_bud(tmp_path, model, jobs=True) as bud:
        await bud.runner.recover()
        await asyncio.sleep(0)
        await bud.settle()
        events = bud.log.read("owner")
        expected = "task.failed" if failed else "task.completed"
        assert any(event.type == expected for event in events)
        triggers = [event.data["trigger"] for event in events if event.type == "run.queued"]
        assert triggers == ["message", "job"]
        assert prompts[-1].startswith(JOB_REPORT)


async def test_the_next_message_does_not_repeat_a_reported_job(tmp_path):
    prompts: list[str] = []
    async with running_bud(tmp_path, bud_with_one_job(prompts), jobs=True) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "When does my lease renew?", "message-1")
        await bud.settle()
        bud.runner.start("owner", "Thanks. Anything else?", "message-2")
        await bud.settle()
        events = bud.log.read("owner")

    assert [e.data["trigger"] for e in events if e.type == "run.queued"] == [
        "message",
        "job",
        "message",
    ]
    assert prompts[-1].startswith("Thanks. Anything else?")
    assert "3 March" not in prompts[-1]


async def test_failed_report_does_not_consume_the_job_update(tmp_path):
    prompts: list[str] = []
    base_model = bud_with_one_job(prompts)

    def model(messages, info):
        if last_prompt(messages).startswith(JOB_REPORT):
            raise RuntimeError("Synthetic report provider failure")
        return base_model(messages, info)

    async with running_bud(tmp_path, model, jobs=True) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "When does my lease renew?", "message-1")
        await bud.settle()
        assert any(event.type == "run.failed" for event in bud.log.read("owner"))

        bud.runner.start("owner", "Please retry that report.", "message-2")
        await bud.settle()
        assert prompts[-1].startswith("Please retry that report.")
        assert "The lease renews on 3 March." in prompts[-1]
        assert bud.runner.snapshot("owner").recent_runs[-1].status == "completed"


async def test_a_quiet_job_is_reported_once_per_reason(tmp_path, monkeypatch):
    prompts: list[str] = []
    async with running_bud(tmp_path, bud_with_one_job(prompts), jobs=True) as bud:
        launch = bud.runner._launch

        def owner_runs_only(run, **kwargs):
            # The job's own run never starts, so the job stays queued and quiet.
            if run["session_id"] == "owner":
                launch(run, **kwargs)

        monkeypatch.setattr(bud.runner, "_launch", owner_runs_only)
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "When does my lease renew?", "message-1")
        await bud.settle()
        now = datetime.now(UTC)

        async def watch(minutes: int) -> None:
            bud.runner.watch_jobs(now + timedelta(minutes=minutes))
            await asyncio.sleep(0)  # the drain it schedules runs on the next loop pass
            await bud.settle()

        await watch(5)
        assert not [e for e in bud.log.read("owner") if e.type == "task.stalled"]

        await watch(11)
        await watch(12)
        assert bud.runner.snapshot("owner").tasks[0].stalled == "idle"
        stalls = [e.data for e in bud.log.read("owner") if e.type == "task.stalled"]
        assert [stall["reason"] for stall in stalls] == ["idle"]
        assert prompts[-1].startswith(JOB_REPORT)
        assert "No activity for 11 minutes." in prompts[-1]

        await watch(61)
        events = bud.log.read("owner")

    stalls = [e.data for e in events if e.type == "task.stalled"]
    assert [stall["reason"] for stall in stalls] == ["idle", "overdue"]
    assert [e.data["trigger"] for e in events if e.type == "run.queued"] == [
        "message",
        "job",
        "job",
    ]
    assert "past its deadline" in prompts[-1]


async def test_completion_during_stall_check_does_not_report_a_stale_stall(tmp_path, monkeypatch):
    prompts: list[str] = []
    async with running_bud(tmp_path, bud_with_one_job(prompts), jobs=True) as bud:
        launch = bud.runner._launch

        def owner_runs_only(run, **kwargs):
            if run["session_id"] == "owner":
                launch(run, **kwargs)

        monkeypatch.setattr(bud.runner, "_launch", owner_runs_only)
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "When does my lease renew?", "message-1")
        await bud.settle()
        items = bud.runner.work_items
        assert items is not None
        item = items.items()[0]
        run = next(e.data for e in bud.log.read(item.session_id) if e.type == "run.queued")
        view = items.view

        def finish_after_check(checked_item):
            state = view(checked_item)
            monkeypatch.setattr(items, "view", view)
            bud.runner._finish(run, "run.completed", {"run_id": run["run_id"], "messages": []}, [])
            return state

        monkeypatch.setattr(items, "view", finish_after_check)
        bud.runner.watch_jobs(datetime.now(UTC) + timedelta(minutes=11))
        await asyncio.sleep(0)
        await bud.settle()
        events = bud.log.read("owner")
        assert [e.type for e in events if e.type in {"task.completed", "task.stalled"}] == [
            "task.completed"
        ]
        reports = [e for e in events if e.type == "run.queued" and e.data["trigger"] == "job"]
        assert len(reports) == 1


def test_owner_message_must_report_new_job_results(tmp_path):
    log = EventLog(tmp_path / "events.db")
    log.append(
        "owner",
        "task.completed",
        {"task_id": 1, "title": "Lease renewal", "text": "Renews on 3 March."},
    )
    note = job_updates_note(log.read("owner"), {"run_id": "next", "trigger": "message"})

    assert "Renews on 3 March." in note
    assert "briefly report any newly completed" in note
    assert "mention a job only if it matters" not in note


def test_stopping_a_report_pauses_its_queue_and_prevents_redispatch(tmp_path):
    log = EventLog(tmp_path / "events.db")
    log.append(
        "owner", "task.completed", {"task_id": 1, "title": "Lease", "text": "Renews in March."}
    )
    log.append("owner", "run.queued", {"run_id": "report", "trigger": "job"})
    log.append("owner", "run.started", {"run_id": "report", "trigger": "job"})

    assert log.request_stop("owner") is not None
    log.acknowledge_stop("owner", "report")

    assert log.snapshot("owner", log.read("owner")).queue_paused
    assert not log.dispatch_job_report("owner", {"run_id": "duplicate", "trigger": "job"})
    assert len([event for event in log.read("owner") if event.type == "run.queued"]) == 1


async def test_queued_job_report_survives_runner_reconstruction(tmp_path, monkeypatch):
    prompts: list[str] = []
    async with running_bud(tmp_path, bud_with_one_job(prompts), jobs=True) as bud:
        launch = bud.runner._launch

        def defer_report(run, **kwargs):
            if run["trigger"] != "job":
                launch(run, **kwargs)

        monkeypatch.setattr(bud.runner, "_launch", defer_report)
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "When does my lease renew?", "message-1")
        await bud.settle()
        pending = bud.log.pending_runs()
        assert len(pending) == 1
        assert pending[0]["trigger"] == "job"
        report_id = pending[0]["run_id"]

    async with running_bud(tmp_path, bud_with_one_job(prompts), jobs=True) as bud:
        await bud.runner.recover()
        await bud.settle()
        events = bud.log.read("owner")
        reports = [e.data for e in events if e.type == "run.queued" and e.data["trigger"] == "job"]
        assert [run["run_id"] for run in reports] == [report_id]
        assert any(e.type == "run.completed" and e.data["run_id"] == report_id for e in events)
        assert "The lease renews on 3 March." in prompts[-1]


def test_a_finished_result_reaches_the_report_with_its_sources(tmp_path):
    log = EventLog(tmp_path / "events.db")
    sources = " Sources: https://modelcontextprotocol.io/docs/getting-started/intro"
    research = "MCP is an open standard. " * 30 + sources
    log.append("owner", "task.completed", {"task_id": 1, "title": "MCP", "text": research})
    log.append("owner", "task.stalled", {"task_id": 2, "title": "Big", "text": "x" * 900})
    note = job_updates_note(log.read("owner"), {"run_id": "next", "trigger": "message"})

    # The result, with the link at its end, arrives whole; a long progress note is cut.
    assert "getting-started/intro" in note
    assert "inspect_task(2) shows all of it" in note
    assert "inspect_task(1)" not in note


def test_a_result_too_long_for_the_report_points_to_the_whole_one(tmp_path):
    log = EventLog(tmp_path / "events.db")
    log.append("owner", "task.completed", {"task_id": 3, "title": "Long", "text": "y" * 5_000})
    note = job_updates_note(log.read("owner"), {"run_id": "next", "trigger": "message"})

    assert "cut at 4,000 of 5,000 characters; inspect_task(3) shows all of it" in note


async def test_a_finished_job_saves_its_report_in_the_workspace(tmp_path):
    prompts: list[str] = []
    async with running_bud(tmp_path, bud_with_one_job(prompts), jobs=True) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "When does my lease renew?", "message-1")
        await bud.settle()
        events = bud.log.read("owner")
        [task] = bud.runner.snapshot("owner").tasks

    [done] = [e.data for e in events if e.type == "task.completed"]
    assert done["report"].startswith("reports/") and done["report"].endswith(
        "-job-1-find-the-renewal-date.md"
    )
    saved = bud.root / done["report"]
    assert done["report_path"] == str(saved)
    text = saved.read_text()
    assert text.startswith("# Find the renewal date\n\nJob 1, finished ")
    assert "The lease renews on 3 March." in text
    # The report turn tells bud where it is, and clients can open it.
    assert f"Saved in your workspace as {done['report']}." in prompts[-1]
    assert task.report == str(saved)


def test_report_names_carry_the_finish_time_and_no_private_words():
    item = WorkItem.model_construct(task_id=7, title="Claude token usage: Q3 / Q4 numbers!")
    finished = datetime(2026, 11, 19, 15, 30, 5, tzinfo=UTC)
    assert report_name(item, finished) == "reports/2026-11-19-153005-job-7-usage-q3-q4-numbers.md"
    # A job revised and finished again the same day gets its own report.
    assert report_name(item, finished + timedelta(hours=2)) != report_name(item, finished)
    secret = WorkItem.model_construct(task_id=8, title="Private keys")
    assert report_name(secret, finished).endswith("-job-8-keys.md")


def test_a_saved_report_is_private_never_overwritten_and_never_written_through_a_link(
    tmp_path,
):
    item = WorkItem.model_construct(task_id=7, title="Weekly numbers")
    finished = datetime(2026, 11, 19, 15, 30, tzinfo=UTC)
    name = report_name(item, finished)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert save_report(workspace, name, item, finished, "First version.")
    assert oct((workspace / name).stat().st_mode & 0o777) == "0o600"
    (workspace / name).write_text("The owner's edits.")
    assert save_report(workspace, name, item, finished, "Replayed version.")
    assert (workspace / name).read_text() == "The owner's edits."

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "reports").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(OSError):
        save_report(linked, name, item, finished, "Should not escape.")
    target = tmp_path / "target.md"
    plain = tmp_path / "plain"
    (plain / "reports").mkdir(parents=True)
    (plain / name).symlink_to(target)
    assert not save_report(plain, name, item, finished, "Should not follow.")
    assert list(elsewhere.iterdir()) == []
    assert not target.exists()


async def test_a_refused_report_is_not_claimed_and_a_deleted_one_stays_deleted(tmp_path):
    prompts: list[str] = []
    async with running_bud(tmp_path / "linked", bud_with_one_job(prompts), jobs=True) as bud:
        (bud.root / "reports").symlink_to(tmp_path, target_is_directory=True)
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "When does my lease renew?", "message-1")
        await bud.settle()
        [done] = [e.data for e in bud.log.read("owner") if e.type == "task.completed"]
        assert "report" not in done
        assert "report_path" not in done
        assert "Saved in your workspace" not in prompts[-1]
        assert bud.runner.snapshot("owner").tasks[0].report is None

    async with running_bud(tmp_path / "deleted", bud_with_one_job([]), jobs=True) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "When does my lease renew?", "message-1")
        await bud.settle()
        [done] = [e.data for e in bud.log.read("owner") if e.type == "task.completed"]
        (bud.root / done["report"]).unlink()
        # A restart publishes finished jobs again; the deleted report is not recreated.
        assert bud.runner.work_items is not None
        bud.runner.work_items.publish(bud.runner.snapshot("owner").tasks[0].session_id)
        assert not (bud.root / done["report"]).exists()
