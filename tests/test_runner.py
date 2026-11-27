import asyncio
from collections import deque

import pytest
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.exceptions import ModelRequestTimeout, ModelServerError, UserError
from pydantic_ai.messages import (
    CompactionPart,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage

from bud.core.runner import DecisionRejected, UnknownApproval
from bud.gateway.errors import NEXT_STEP
from bud.gateway.registry import Registry
from bud.gateway.router import Gateway
from tests.runner_support import create_note, reply, running_bud, scripted_model


def responses(*items):
    pending = deque(items)

    def respond(messages, info):
        assert pending, "Bud made an unexpected model request"
        response = pending.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    return respond


def configure_model_choices(bud, *, alternate=True, primary_offline=False, pin_primary=False):
    old = bud.runner.gateway
    primary = old.registry.models[0]
    secondary = primary.model_copy(update={"name": "alternate"})

    def respond(messages, info):
        if primary_offline:
            raise ModelServerError(primary.name, "Unavailable")
        return reply("Primary model")

    models = {
        primary.id: scripted_model(respond),
        secondary.id: scripted_model(lambda *_: reply("Alternate model")) if alternate else None,
    }
    bud.runner.reload_gateway(
        Gateway(
            Registry(models=[primary, secondary]),
            old.ledger,
            lambda spec: models[spec.id],
            selected_model=primary.id if pin_primary else None,
        )
    )
    return primary.id, secondary.id


@pytest.mark.parametrize("alternate_available", [True, False])
async def test_session_model_survives_restart_or_uses_default(tmp_path, alternate_available):
    async with running_bud(tmp_path, responses()) as bud:
        primary, alternate = configure_model_choices(bud)
        bud.log.append("owner", "session.created")
        bud.runner.set_model("owner", alternate)

    async with running_bud(tmp_path, responses()) as bud:
        configure_model_choices(bud, alternate=alternate_available)
        await bud.runner.recover()
        assert bud.runner.snapshot("owner").model == alternate
        bud.runner.start("owner", "Which model is running?", "first")
        await bud.settle()
        state = bud.runner.snapshot("owner")
        assert state.transcript[-1].text == (
            "Alternate model" if alternate_available else "Primary model"
        )
        assert state.recent_runs[-1].model == (alternate if alternate_available else primary)
        bud.runner.set_model("owner", None)

    async with running_bud(tmp_path, responses()) as bud:
        configure_model_choices(bud)
        await bud.runner.recover()
        assert bud.runner.snapshot("owner").model is None
        bud.runner.start("owner", "Which model is running after reset?", "second")
        await bud.settle()
        assert bud.runner.snapshot("owner").transcript[-1].text == "Primary model"


async def test_new_runs_prefer_session_then_settings_over_gateway_default(tmp_path):
    async with running_bud(tmp_path, responses()) as bud:
        primary, alternate = configure_model_choices(bud, pin_primary=True)
        bud.log.append("owner", "session.created")
        bud.runner.owner_model = alternate
        bud.runner.set_model("owner", primary)

        async def assert_run(message_id, model, reply_text):
            bud.runner.start("owner", "Which model is running?", message_id)
            await bud.settle()
            state = bud.runner.snapshot("owner")
            assert state.recent_runs[-1].model == model
            assert state.transcript[-1].text == reply_text

        await assert_run("first", primary, "Primary model")
        bud.runner.set_model("owner", None)
        await assert_run("second", alternate, "Alternate model")
        bud.runner.owner_model = None
        await assert_run("third", primary, "Primary model")


@pytest.mark.parametrize("selection", ["automatic", "session", "default"])
async def test_enqueued_run_keeps_its_model_after_a_switch(tmp_path, selection):
    async with running_bud(tmp_path, responses()) as bud:
        primary, alternate = configure_model_choices(bud)
        bud.log.append("owner", "session.created")
        if selection == "session":
            bud.runner.set_model("owner", alternate)
        elif selection == "default":
            bud.runner.owner_model = alternate
        pinned = primary if selection == "automatic" else alternate
        replacement = alternate if pinned == primary else primary

        bud.runner.start("owner", "Use the queued model", "first")
        assert bud.log.pending_runs()[0]["model"] == pinned
        bud.runner.owner_model = replacement
        bud.runner.set_model("owner", replacement)
        await bud.settle()

        state = bud.runner.snapshot("owner")
        assert state.recent_runs[-1].model == pinned
        assert state.transcript[-1].text == (
            "Primary model" if pinned == primary else "Alternate model"
        )


async def test_enqueued_automatic_run_keeps_its_fallback(tmp_path):
    async with running_bud(tmp_path, responses()) as bud:
        primary, alternate = configure_model_choices(bud, primary_offline=True)
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Try the automatic route", "first")
        assert bud.log.pending_runs()[0]["model"] == primary
        await bud.settle()
        assert bud.runner.snapshot("owner").transcript[-1].text == "Alternate model"
        assert {entry["model"] for entry in bud.runner.gateway.ledger.session_models("owner")} == {
            primary,
            alternate,
        }


@pytest.mark.parametrize("pinned_available", [True, False])
async def test_recovery_keeps_the_enqueued_model_or_fails_safely(
    tmp_path, monkeypatch, pinned_available
):
    async with running_bud(tmp_path, responses()) as bud:
        primary, alternate = configure_model_choices(bud)
        bud.log.append("owner", "session.created")
        bud.runner.set_model("owner", alternate)
        with monkeypatch.context() as patch:
            patch.setattr(bud.runner, "_launch", lambda run: None)
            bud.runner.start("owner", "Recover on the queued model", "first")
        assert bud.log.pending_runs()[0]["model"] == alternate
        bud.runner.set_model("owner", primary)

    async with running_bud(tmp_path, responses()) as bud:
        configure_model_choices(bud, alternate=pinned_available)
        await bud.runner.recover()
        await bud.settle()
        state = bud.runner.snapshot("owner")
        assert state.model == primary
        if pinned_available:
            assert state.recent_runs[-1].model == alternate
            assert state.transcript[-1].text == "Alternate model"
        else:
            assert state.recent_runs[-1].status == "failed"
            assert state.recent_runs[-1].kind == "model_unavailable"
            assert not bud.runner.gateway.ledger.session_models("owner")


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
    async with running_bud(tmp_path, responses(create_note(), reply("Recovered"))) as bud:
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
        bud.runner.start("owner", "Leave the file as it is", "message-2")
        await bud.settle()
        assert bud.runner.snapshot("owner").recent_runs[-1].status == "completed"
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
        assert state.recent_runs[-1].kind == "timeout"
        assert state.recent_runs[-1].next == NEXT_STEP["timeout"]
        assert state.queue_paused
        assert not bud.claims.exists()
        bud.runner.start("owner", "Try again", "message-2")
        await bud.settle()
        state = bud.runner.snapshot("owner")
        assert state.recent_runs[-1].status == "completed"
        assert state.recent_runs[-1].kind is None
        assert state.recent_runs[-1].next is None
        assert not state.queue_paused


async def test_failed_run_snapshot_keeps_safe_structured_fields_without_leaking_message(tmp_path):
    secret = "raw internal detail sk-secret-12345"
    async with running_bud(tmp_path, responses(UserError(secret))) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Create a routine every Monday", "message-1")
        await bud.settle()
        state = bud.runner.snapshot("owner")
        latest = state.recent_runs[-1]
        assert state.status == "paused"
        assert state.queue_paused
        assert latest.status == "failed"
        assert latest.reason == "UserError"
        assert latest.kind == "unknown"
        assert latest.next == NEXT_STEP["unknown"]
        assert secret not in (latest.reason or "")
        assert secret not in (latest.next or "")


@pytest.mark.parametrize(("terminal", "inline"), [("run.failed", False), ("run.cancelled", True)])
async def test_retry_continues_from_the_saved_conversation(tmp_path, terminal, inline):
    received = []

    def respond(messages, info):
        received.extend(part for message in messages for part in message.parts)
        return reply("Recovered")

    session = "owner"
    summary = ModelRequest(parts=[CompactionPart(content="Compacted inbox history")])
    call = ModelResponse(parts=[ToolCallPart("workspace_create", {}, tool_call_id="late-approved")])
    result = ToolReturnPart("workspace_create", "Created the note", tool_call_id="late-approved")
    late_result = ModelRequest(parts=[result])
    saved = Conversation(
        conversation_id=session,
        messages=[summary, call, late_result, reply("Note created")],
        usage=RunUsage(tool_calls=4),
    )
    async with running_bud(tmp_path, respond) as bud:
        bud.log.append(session, "session.created")
        bud.log.finish_run(
            session,
            "old",
            "run.completed",
            {
                "run_id": "old",
                "messages": ModelMessagesTypeAdapter.dump_python([reply("Old body")], mode="json"),
            },
            [],
        )
        data = {
            "run_id": "late",
            "messages": ModelMessagesTypeAdapter.dump_python(
                [late_result, reply("Note created")], mode="json"
            ),
            "conversation": ConversationTypeAdapter.dump_json(saved).decode(),
        }
        if inline:
            bud.log.append_once(session, "run.completed", data, key="late:terminal")
        else:
            bud.log.finish_run(session, "late", "run.completed", data, [])
        for index, kind in enumerate(("run.failed", terminal)):
            run_id = f"attempt-{index}"
            bud.log.append(session, "run.queued", {"run_id": run_id, "text": run_id})
            bud.log.finish_run(session, run_id, kind, {"run_id": run_id, "reason": run_id}, [])

        bud.runner.start(session, "Retry", "retry")
        await bud.settle()
        latest = bud.runner.snapshot(session).recent_runs[-1]
        assert latest.status == "completed"
        assert summary.parts[0] in received
        assert call.parts[0] in received
        assert received.count(result) == 1
        assert reply("Old body").parts[0] not in received
        notes = [
            part.content
            for part in received
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        ]
        for index in range(2):
            assert sum(f"The user had said: attempt-{index}" in note for note in notes) == 1
        checkpoint = bud.log.conversation(session, latest.run_id)
        assert checkpoint is not None
        assert ConversationTypeAdapter.validate_json(checkpoint).usage.tool_calls == 4


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
