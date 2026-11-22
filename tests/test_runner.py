import asyncio
import threading
import time
from collections import deque

import pytest
from dbos import DBOS
from pydantic_ai.exceptions import ModelRequestTimeout
from pydantic_ai.models.function import FunctionModel

from bud.core.runner import DecisionRejected, UnknownApproval
from tests.runner_support import create_note, reply, running_bud


def responses(*items):
    pending = deque(items)

    def respond(messages, info):
        assert pending, "Bud made an unexpected model request"
        response = pending.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    return respond


@pytest.mark.parametrize("edit", [True, False], ids=["edit", "cancel"])
async def test_queue_mutation_during_dispatch_keeps_the_queue_moving(tmp_path, monkeypatch, edit):
    async with running_bud(tmp_path, responses(reply())) as bud:
        session = "owner"
        bud.log.append(session, "session.created")
        bud.runner.pause_queue(session, True)
        bud.runner.start(session, "Original first message", "first")
        bud.runner.start(session, "Second message", "second")
        dispatch = bud.log.dispatch_message

        def mutate_before_dispatch(session_id, message_id, run):
            monkeypatch.setattr(bud.log, "dispatch_message", dispatch)
            if edit:
                bud.runner.edit_message(session, "first", "Edited first message")
            else:
                assert bud.runner.cancel_message(session, "first")
            assert not dispatch(session_id, message_id, run)
            return False

        monkeypatch.setattr(bud.log, "dispatch_message", mutate_before_dispatch)
        bud.runner.pause_queue(session, False)
        await bud.settle()
        owner_turns = [
            turn.text for turn in bud.runner.snapshot(session).transcript if turn.role == "user"
        ]
        expected = ["Edited first message", "Second message"] if edit else ["Second message"]
        assert owner_turns == expected


@pytest.mark.parametrize("approved", [False, True], ids=["deny", "approve"])
async def test_write_waits_for_exact_decision_and_duplicate_is_noop(tmp_path, approved):
    async with running_bud(tmp_path, responses(create_note(), reply())) as bud:
        bud.log.append("owner", "session.created")
        bud.log.append("other", "session.created")
        bud.runner.start("owner", "Create note.txt", "message-1")
        await bud.settle()
        request = bud.runner.snapshot("owner").pending_approvals[0]
        assert request.args == {"path": "note.txt", "content": "Cedar first draft"}
        assert not (bud.root / "note.txt").exists()
        assert not bud.claims.exists()

        with pytest.raises(UnknownApproval):
            bud.runner.decide("other", request.approval_id, True)
        with pytest.raises(UnknownApproval):
            bud.runner.decide("owner", "not-the-approval", True)
        with pytest.raises(DecisionRejected):
            bud.runner.answer("owner", request.approval_id, "yes")
        assert not (bud.root / "note.txt").exists()

        assert bud.runner.decide("owner", request.approval_id, approved)
        assert not bud.runner.decide("owner", request.approval_id, approved)
        await bud.settle()
        assert not bud.runner.snapshot("owner").pending_approvals
        assert bud.runner.snapshot("owner").recent_runs[-1].status == "completed"
        if approved:
            assert (bud.root / "note.txt").read_text() == "Cedar first draft"
            assert len(bud.claims.read_text().splitlines()) == 1
        else:
            assert not (bud.root / "note.txt").exists()
            assert not bud.claims.exists()


async def test_revision_retires_old_approval_and_executes_only_revised_content(tmp_path):
    model = responses(
        create_note(),
        reply("Old proposal withdrawn"),
        create_note("Cedar revised draft", call_id="revised-note"),
        reply(),
    )
    async with running_bud(tmp_path, model) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Create the first draft", "message-1")
        await bud.settle()
        old = bud.runner.snapshot("owner").pending_approvals[0].approval_id
        assert bud.runner.revise("owner", old, "Use the revised draft", "revision-1", {})
        await bud.settle()
        new = bud.runner.snapshot("owner").pending_approvals[0].approval_id
        assert old != new
        assert not bud.runner.decide("owner", old, True)
        assert not bud.runner.revise("owner", old, "Use the revised draft", "revision-1", {})
        assert not (bud.root / "note.txt").exists()
        assert bud.runner.decide("owner", new, True)
        await bud.settle()
        assert (bud.root / "note.txt").read_text() == "Cedar revised draft"
        assert len(bud.claims.read_text().splitlines()) == 1


async def test_duplicate_message_is_noop_and_changed_content_is_rejected(tmp_path):
    async with running_bud(tmp_path, responses(create_note())) as bud:
        bud.log.append("owner", "session.created")
        assert bud.runner.start("owner", "Create note.txt", "message-1") is not None
        assert bud.runner.start("owner", "Create note.txt", "message-1") is None
        with pytest.raises(ValueError, match="different content"):
            bud.runner.start("owner", "Write something else", "message-1")
        await bud.settle()
        assert len(bud.runner.snapshot("owner").pending_approvals) == 1
        assert len(bud.runner.snapshot("owner").recent_runs) == 1
        assert not bud.claims.exists()


async def test_stop_retires_pending_permission_and_does_not_write(tmp_path):
    async with running_bud(tmp_path, responses(create_note())) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Create note.txt", "message-1")
        await bud.settle()
        approval = bud.runner.snapshot("owner").pending_approvals[0].approval_id
        result = await bud.runner.stop_session("owner")
        assert result == {"stop_requested": True, "queue_paused": True}
        assert not bud.runner.decide("owner", approval, True)
        await bud.settle()
        assert bud.runner.snapshot("owner").recent_runs[-1].status == "cancelled"
        assert not bud.runner.snapshot("owner").pending_approvals
        assert not (bud.root / "note.txt").exists()
        assert not bud.claims.exists()


async def test_model_failure_pauses_queue_then_new_message_can_recover(tmp_path):
    async with running_bud(
        tmp_path, responses(ModelRequestTimeout("scripted", 1), reply("Recovered"))
    ) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Read my workspace", "message-1")
        await bud.settle()
        state = bud.runner.snapshot("owner")
        assert state.recent_runs[-1].status == "failed"
        assert state.queue_paused
        assert not bud.claims.exists()
        bud.runner.start("owner", "Try again", "message-2")
        await bud.settle()
        state = bud.runner.snapshot("owner")
        assert state.recent_runs[-1].status == "completed"
        assert not state.queue_paused


async def test_failed_run_does_not_silently_dispatch_waiting_messages(tmp_path):
    async with running_bud(tmp_path, responses(ModelRequestTimeout("scripted", 1))) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "First request", "message-1")
        bud.runner.start("owner", "Waiting request", "message-2")
        await bud.settle()
        state = bud.runner.snapshot("owner")
        assert state.recent_runs[-1].status == "failed"
        assert state.queue_paused
        assert [item.message_id for item in state.queued_messages] == ["message-2"]
        assert not bud.claims.exists()


async def test_waiting_approval_survives_runner_reconstruction(tmp_path):
    async with running_bud(tmp_path, responses(create_note())) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Create note.txt", "message-1")
        await bud.settle()
        approval = bud.runner.snapshot("owner").pending_approvals[0]

    async with running_bud(tmp_path, responses(reply())) as bud:
        await bud.runner.recover()
        assert bud.runner.snapshot("owner").pending_approvals == [approval]
        assert not (bud.root / "note.txt").exists()
        assert bud.runner.decide("owner", approval.approval_id, True)
        await bud.settle()
        assert (bud.root / "note.txt").read_text() == "Cedar first draft"
        assert len(bud.claims.read_text().splitlines()) == 1


async def test_legacy_interrupted_run_without_replay_inputs_fails_closed(tmp_path):
    async with running_bud(tmp_path, responses()) as bud:
        bud.log.append("owner", "session.created")
        bud.log.append("owner", "run.started", {"run_id": "old-run"})
        await bud.runner.recover()
        await bud.settle()
        state = bud.runner.snapshot("owner")
        assert state.recent_runs[-1].status == "failed"
        assert state.recent_runs[-1].reason == "interrupted before durable execution was enabled"
        assert state.queue_paused
        assert not bud.claims.exists()


async def test_recovery_refuses_changed_configuration_before_tool_dispatch(tmp_path, monkeypatch):
    async with running_bud(tmp_path, responses()) as bud:
        bud.log.append("owner", "session.created")
        with monkeypatch.context() as patch:
            patch.setattr(bud.runner, "_launch", lambda run: None)
            bud.runner.start("owner", "Create note.txt", "message-1")
        assert len(bud.log.pending_runs()) == 1

    async with running_bud(tmp_path, responses(), jobs=True) as bud:
        await bud.runner.recover()
        await bud.settle()
        state = bud.runner.snapshot("owner")
        assert state.recent_runs[-1].status == "failed"
        assert state.recent_runs[-1].reason is not None
        assert "configuration changed" in state.recent_runs[-1].reason.lower()
        assert not (bud.root / "note.txt").exists()
        assert not bud.claims.exists()


async def test_stop_cancels_inflight_model_without_executing_tools(tmp_path):
    requested = asyncio.Event()

    async with running_bud(tmp_path, responses(reply())) as bud:

        async def stalled(messages, info):
            requested.set()
            await asyncio.Event().wait()
            yield "unreachable"

        with bud.runner.agent.override(model=FunctionModel(stream_function=stalled)):
            bud.log.append("owner", "session.created")
            bud.runner.start("owner", "Create note.txt", "message-1")
            await asyncio.wait_for(requested.wait(), 5)
            await bud.runner.stop_session("owner")
            async with asyncio.timeout(5):
                while bud.runner.tasks:
                    await asyncio.sleep(0)
        assert bud.runner.snapshot("owner").recent_runs[-1].status == "cancelled"
        assert not (bud.root / "note.txt").exists()


async def test_a_late_approval_does_not_answer_the_newer_message_again(tmp_path):
    """The owner sends another message before approving a card. When they approve it later,
    Bud reports on that action and does not answer the newer message a second time."""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    def respond(messages, info):
        prompts = [
            part.content
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        ]
        latest = prompts[-1]
        if latest.startswith("Create note.txt") and len(messages) == 1:
            return create_note()
        if "has now approved workspace_create" in latest:
            return reply("Created note.txt.")
        if latest.startswith("What is 2+2?"):
            return reply("4")
        return reply("Okay.")

    async with running_bud(tmp_path, respond) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Create note.txt", "message-1")
        await bud.settle()
        card = bud.runner.snapshot("owner").pending_approvals[0].approval_id
        bud.runner.start("owner", "What is 2+2?", "message-2")
        await bud.settle()
        assert bud.runner.decide("owner", card, True)
        await bud.settle()
        said = [t.text for t in bud.runner.snapshot("owner").transcript if t.role == "assistant"]
        assert said.count("4") == 1, said
        assert said[-1] == "Created note.txt."
        assert (bud.root / "note.txt").read_text() == "Cedar first draft"


async def test_durable_shutdown_stops_fast_and_leaves_workflow_recoverable(tmp_path):
    """THE-182: Stopping a durable runner mid-run finishes near SHUTDOWN_WAIT_SECONDS (< 10s)
    and leaves the DBOS workflow PENDING (recoverable) rather than ERROR."""
    started = threading.Event()
    release = threading.Event()

    async def stalled(messages, info):
        started.set()
        await asyncio.to_thread(release.wait, 30)
        yield "unreachable"

    model = FunctionModel(
        lambda messages, info: reply("done"),
        stream_function=stalled,
        model_name="scripted",
    )
    async with running_bud(tmp_path, responses(), durable=True, model=model) as bud:
        DBOS(
            config={
                "name": "bud",
                "system_database_url": "sqlite:///" + str((tmp_path / "runtime.db").resolve()),
                "executor_id": "bud-core",
                "application_version": bud.runner.version,
                "enable_otlp": False,
                "log_level": "WARNING",
            }
        )
        DBOS.launch()
        try:
            bud.log.append("owner", "session.created")
            bud.runner.start("owner", "Hold this run open", "message-1")
            run_id = bud.log.pending_runs()[0]["run_id"]
            assert await asyncio.to_thread(started.wait, 5), "durable run did not enter model"
            t0 = time.monotonic()
            await bud.runner.stop()
            elapsed = time.monotonic() - t0
            assert elapsed < 8.0
            status = await DBOS.get_workflow_status_async(run_id)
            assert status is not None and status.status == "PENDING"
        finally:
            release.set()
            await asyncio.to_thread(DBOS.destroy, workflow_completion_timeout_sec=0)
