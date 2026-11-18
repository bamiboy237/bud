from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from bud.core.hub import Hub
from bud.core.log import EventLog, QueueFull, StopRequested
from bud.core.projection import parked_approvals, pause_reason, queued_messages, undecided_approvals
from bud.core.tasks import TaskBrief, WorkItems
from bud.limits import MAX_QUEUED_MESSAGES


def pending_write(log, session="owner", run="run-1", approval="approval-1"):
    log.append(session, "run.queued", {"run_id": run, "session_id": session})
    log.append(session, "run.started", {"run_id": run})
    log.finish_run(
        session,
        run,
        "run.waiting",
        {"run_id": run},
        [
            {
                "run_id": run,
                "approval_id": approval,
                "call_id": f"call-{approval}",
                "tool": "workspace_create",
                "args": {"path": "note.txt", "content": "draft"},
            }
        ],
    )


def test_snapshot_cache_reuses_the_fold_and_observes_another_writer(tmp_path, monkeypatch):
    import bud.core.log as log_module

    path = tmp_path / "events.db"
    log = EventLog(path)
    pending_write(log)
    fold = log_module.snapshot
    calls = []

    def counted(*args):
        calls.append(True)
        return fold(*args)

    monkeypatch.setattr(log_module, "snapshot", counted)
    first = log.snapshot("owner", log.read("owner"))
    first.pending_approvals.clear()
    second = log.snapshot("owner", log.read("owner"))
    assert len(second.pending_approvals) == 1
    assert len(calls) == 1
    EventLog(path).append("owner", "approval.denied", {"approval_id": "approval-1"})
    third = log.snapshot("owner", log.read("owner"))
    assert not third.pending_approvals
    assert len(calls) == 2


def test_concurrent_duplicate_message_is_accepted_once_and_survives_reopen(tmp_path):
    path = tmp_path / "events.db"
    logs = [EventLog(path), EventLog(path)]
    barrier = Barrier(2)

    def accept(log):
        barrier.wait(timeout=5)
        return log.accept_message("owner", "Create note.txt", "same-message")

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(accept, logs))
    assert sum(event is not None for event in accepted) == 1
    reopened = EventLog(path)
    assert len(queued_messages(reopened.read("owner"))) == 1
    assert reopened.accept_message("owner", "Create note.txt", "same-message") is None
    with pytest.raises(ValueError, match="different content or origin"):
        reopened.accept_message("owner", "Different content", "same-message")


def test_duplicate_decisions_have_one_winner(tmp_path):
    path = tmp_path / "events.db"
    logs = [EventLog(path), EventLog(path)]
    pending_write(logs[0])
    barrier = Barrier(2)

    def decide(args):
        log, event_type = args
        barrier.wait(timeout=5)
        return log.append_once("owner", event_type, {"approval_id": "approval-1"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(decide, zip(logs, ["approval.granted", "approval.denied"], strict=True))
        )
    assert sum(event is not None for event in results) == 1
    decisions = [
        event
        for event in EventLog(path).read("owner")
        if event.type in {"approval.granted", "approval.denied"}
    ]
    assert len(decisions) == 1
    assert not undecided_approvals(EventLog(path).read("owner"))


def test_queue_limit_rejects_only_new_messages_and_preserves_existing_queue(tmp_path):
    log = EventLog(tmp_path / "events.db")
    for number in range(MAX_QUEUED_MESSAGES):
        assert log.accept_message("owner", f"message {number}", f"message-{number}") is not None
    assert log.accept_message("owner", "message 0", "message-0") is None
    with pytest.raises(QueueFull):
        log.accept_message("owner", "one too many", "overflow")
    assert len(queued_messages(log.read("owner"))) == MAX_QUEUED_MESSAGES
    assert log.cancel_message("owner", "message-0")
    assert log.accept_message("owner", "now there is room", "next") is not None


def test_invalid_revision_rolls_back_both_message_and_decision(tmp_path):
    log = EventLog(tmp_path / "events.db")
    pending_write(log)
    before = log.read("owner")
    with pytest.raises(ValueError, match="no longer waiting"):
        log.revise_approval("owner", "wrong-approval", "replace this", "correction", {})
    assert log.read("owner") == before
    assert not queued_messages(log.read("owner"))
    assert log.revise_approval("owner", "approval-1", "replace this", "correction", {})
    assert not log.revise_approval("owner", "approval-1", "replace this", "correction", {})
    assert len(queued_messages(log.read("owner"))) == 1
    assert not undecided_approvals(log.read("owner"))


def test_stop_blocks_new_tool_permission(tmp_path):
    log = EventLog(tmp_path / "events.db")
    log.append("owner", "run.queued", {"run_id": "run-1"})
    log.append("owner", "run.started", {"run_id": "run-1"})
    assert log.request_stop("owner") is not None
    with pytest.raises(StopRequested):
        log.record_action(
            "owner",
            "run-1",
            {"call_id": "call-1", "tool": "workspace_create", "outcome": "approved"},
        )
    assert not any(event.type == "action.decided" for event in log.read("owner"))


@pytest.mark.parametrize("result_recorded", [False, True])
def test_stop_after_dispatch_exposes_uncertain_outcome_until_checked(tmp_path, result_recorded):
    path = tmp_path / "events.db"
    log = EventLog(path)
    log.append("owner", "run.queued", {"run_id": "run-1"})
    log.append("owner", "run.started", {"run_id": "run-1"})
    log.record_action(
        "owner",
        "run-1",
        {"call_id": "call-1", "tool": "send_email", "outcome": "approved"},
    )
    if result_recorded:
        log.append("owner", "tool.result", {"run_id": "run-1", "call_id": "call-1"})
    log.request_stop("owner")
    log.acknowledge_stop("owner", "run-1")
    log.acknowledge_stop("owner", "run-1")
    events = EventLog(path).read("owner")
    cancelled = [event for event in events if event.type == "run.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0].data["uncertain_tools"] == ([] if result_recorded else ["send_email"])
    assert pause_reason(events) == ("stopped" if result_recorded else "uncertain")
    log.accept_message("owner", "Continue", "new-message")
    assert pause_reason(log.read("owner")) == (None if result_recorded else "uncertain")


def test_job_correction_retires_only_its_approval_while_chat_preserves_actions(tmp_path):
    log = EventLog(tmp_path / "events.db")
    log.append("owner", "session.created")
    work = WorkItems(Hub(log))
    brief = TaskBrief(goal="Write note", inputs=[], expected_result="Saved note")
    first = work.create("owner", "First job", brief, "source-1", None, None)
    second = work.create("owner", "Second job", brief, "source-2", None, None)
    pending_write(log, first.session_id, "run-first", "approval-first")
    pending_write(log, second.session_id, "run-second", "approval-second")
    pending_write(log, "owner", "run-chat", "approval-chat")
    log.accept_message("owner", "What is 2 + 2?", "chat-question")
    assert [item["approval_id"] for item in parked_approvals(log.read("owner"))] == [
        "approval-chat"
    ]
    work.update(first, "Use revised content", "revision", None, None)
    assert not undecided_approvals(log.read(first.session_id))
    assert [item["approval_id"] for item in undecided_approvals(log.read(second.session_id))] == [
        "approval-second"
    ]


def test_snapshot_is_reused_until_an_event_arrives_and_never_shared_by_callers(tmp_path):
    log = EventLog(tmp_path / "events.db")
    log.append("owner", "user.message", {"text": "first", "message_id": "m1"})
    first = log.snapshot("owner", log.read("owner"))
    first.pending_approvals.append("mutation")  # ty: ignore[invalid-argument-type]
    again = log.snapshot("owner", log.read("owner"))
    assert again.pending_approvals == []
    assert [turn.text for turn in again.transcript] == ["first"]
    log.append("owner", "user.message", {"text": "second", "message_id": "m2"})
    latest = log.snapshot("owner", log.read("owner"))
    assert [turn.text for turn in latest.transcript] == ["first", "second"]
    assert latest.last_seq == again.last_seq + 1
