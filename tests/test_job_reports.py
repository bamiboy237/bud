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
from tests.runner_support import reply, running_bud

BRIEF = {
    "goal": "Find the renewal date of the lease.",
    "inputs": [],
    "expected_result": "The renewal date.",
}


def last_prompt(messages: list[ModelMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in message.parts:
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
