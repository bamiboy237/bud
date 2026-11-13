"""Fold session events into conversation, approval, queue, and run views."""

from contextlib import suppress
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    UserPromptPart,
)

from bud.core.events import Event, MessageOrigin
from bud.limits import MAX_MESSAGE_CHARS


class Turn(BaseModel):
    role: str
    text: str
    task_id: int | None = None


class PendingApproval(BaseModel):
    approval_id: str
    tool: str
    args: dict[str, Any]
    task_id: int | None = None
    reference: str | None = None


class QueuedMessage(BaseModel):
    message_id: str
    text: str
    origin: MessageOrigin | None = None


class PendingSetup(BaseModel):
    """One capability setup card, tracked until it connects or is replaced."""

    capability: str
    label: str
    status: str = "waiting"
    kind: str | None = None
    url: str | None = None
    steps: list[str] = Field(default_factory=list)
    reason: str | None = None


class SavedArtifact(BaseModel):
    path: str
    revision: str
    operation: str


class RunView(BaseModel):
    run_id: str
    status: str
    reason: str | None = None
    loaded_skills: dict[str, str] = Field(default_factory=dict)
    artifacts: list[SavedArtifact] = Field(default_factory=list)


class JobReport(BaseModel):
    """How a job ended. The job writes the summary and notes; code sets everything else."""

    outcome: Literal["delivered", "partly_delivered", "not_delivered"]
    summary: str
    # Expected items the checker found absent, never the job's own claim.
    missing: list[str] = Field(default_factory=list)
    # The job's own account of what it could not do, and why.
    notes: list[str] = Field(default_factory=list)
    # Workspace files this job actually saved, from tool metadata, never from the model.
    saved: list[str] = Field(default_factory=list)
    # False when no separate check ran: the outcome is then the job's own account.
    checked: bool = False


OUTCOME_WORDS = {
    "delivered": "Delivered",
    "partly_delivered": "Partly delivered",
    "not_delivered": "Not delivered",
}


def report_text(report: dict) -> str:
    job = JobReport.model_validate(report)
    label = OUTCOME_WORDS[job.outcome] + ("" if job.checked else " (job's own report, not checked)")
    lines = [f"{label}. {job.summary}"]
    if job.missing:
        lines.append("Missing: " + "; ".join(job.missing))
    if job.notes:
        lines.append("Job notes: " + "; ".join(job.notes))
    if job.saved:
        lines.append("Saved: " + ", ".join(job.saved))
    return "\n".join(lines)


class TaskView(BaseModel):
    task_id: int
    session_id: str
    title: str
    status: str
    pending_approvals: list[PendingApproval]
    artifacts: list[SavedArtifact]
    result: str | None = None
    # The job's filed outcome; "completed" alone only means it stopped working.
    outcome: str | None = None


class Snapshot(BaseModel):
    session_id: str
    last_seq: int
    run_active: bool
    pending_approvals: list[PendingApproval]
    pending_setup: list[PendingSetup] = Field(default_factory=list)
    transcript: list[Turn]
    status: str = "idle"
    queue_paused: bool = False
    queued_messages: list[QueuedMessage] = Field(default_factory=list)
    recent_runs: list[RunView] = Field(default_factory=list)
    tasks: list[TaskView] = Field(default_factory=list)


def queued_messages(events: list[Event]) -> list[QueuedMessage]:
    pending: dict[str, QueuedMessage] = {}
    for event in events:
        if event.type == "message.queued":
            item = QueuedMessage.model_validate(event.data)
            pending[item.message_id] = item
        elif event.type == "message.edited":
            item = pending.get(event.data["message_id"])
            if item is not None:
                item.text = event.data["text"]
        elif event.type in ("message.cancelled", "message.dispatched"):
            pending.pop(event.data["message_id"], None)
    return list(pending.values())


def message_batch(events: list[Event]) -> list[QueuedMessage]:
    """Coalesce only adjacent iMessage inputs from one route within two seconds."""
    pending = queued_messages(events)
    if not pending:
        return []
    first = pending[0]
    if first.origin is None or first.origin.channel != "imessage":
        return [first]
    times = {
        event.data["message_id"]: event.created_at
        for event in events
        if event.type == "message.queued"
    }
    batch = []
    length = 0
    for item in pending:
        if (
            item.origin != first.origin
            or (times[item.message_id] - times[first.message_id]).total_seconds() > 2
            or length + len(item.text) + 2 > MAX_MESSAGE_CHARS
        ):
            break
        batch.append(item)
        length += len(item.text) + 2
    return batch or [first]


def queue_paused(events: list[Event]) -> bool:
    return next(
        (
            event.type == "queue.paused"
            for event in reversed(events)
            if event.type in ("queue.paused", "queue.resumed")
        ),
        False,
    )


# Pauses bud made itself; the owner's next message lifts them. An owner /pause and a
# stop that left an action's outcome unknown stay until the owner resumes.
AUTO_PAUSES = ("failed", "stopped", "looping")


def pause_reason(events: list[Event]) -> str | None:
    """Why the queue is paused, or None when it is open. Old pauses had no reason and
    are treated as the owner's own."""
    last = next((e for e in reversed(events) if e.type in ("queue.paused", "queue.resumed")), None)
    if last is None or last.type == "queue.resumed":
        return None
    return last.data.get("reason", "owner")


def pending_setups(events: list[Event]) -> list[PendingSetup]:
    """Fold capability cards: one per capability, kept until it connects.

    A `capability.setup` event replaces a capability's card wholesale; a
    `capability.status` recheck only moves its state, so the sign-in link or
    steps the card already showed stay with it. A connected card has settled
    and stops being pending, like a decided approval; a failed card stays —
    its reason sentence is the recovery move the person still needs.
    """
    cards: dict[str, PendingSetup] = {}
    for event in events:
        if event.type not in ("capability.setup", "capability.status"):
            continue
        try:
            incoming = PendingSetup.model_validate(event.data)
        except ValidationError:
            continue
        card = cards.get(incoming.capability)
        if event.type == "capability.status" and card is not None:
            incoming = card.model_copy(
                update={"status": incoming.status, "reason": incoming.reason}
            )
        cards[incoming.capability] = incoming
    return [card for card in cards.values() if card.status != "connected"]


def runs(events: list[Event], *, summaries: bool = False) -> list[RunView]:
    """Fold run status. `summaries` adds loaded skills and saved files. That is display
    data, so locking paths leave it off and a malformed summary never blocks a run."""
    states: dict[str, RunView] = {}
    offered: dict[str, dict[str, str]] = {}
    loads: dict[tuple[str, str], str] = {}
    names = {
        "run.queued": "queued",
        "run.started": "running",
        "run.waiting": "awaiting_approval",
        "run.completed": "completed",
        "run.failed": "failed",
        "run.stop_requested": "stopping",
        "run.cancelled": "cancelled",
    }
    for event in events:
        if event.type in names:
            run_id = event.data["run_id"]
            status = names[event.type]
            if run_id in states and states[run_id].status == "stopping" and status != "cancelled":
                continue
            if event.type == "run.cancelled" and event.data.get("uncertain_tools"):
                status = "needs_review"
            view = states.get(run_id)
            if view is None:
                view = states[run_id] = RunView(run_id=run_id, status=status)
                if summaries and event.data.get("trigger") == "approval" and len(states) > 1:
                    # An approved action executes in a new run that continues the previous
                    # run's work, so it inherits the methods that run had loaded.
                    previous = list(states.values())[-2]
                    view.loaded_skills = dict(previous.loaded_skills)
            view.status = status
            view.reason = event.data.get("reason")
        if not summaries:
            continue
        run_id = event.data.get("run_id")
        if not isinstance(run_id, str):
            continue
        if event.type == "run.started":
            offered[run_id] = event.data.get("skills") or {}
        elif event.type == "tool.call" and event.data.get("tool") == "load_capability":
            name = (event.data.get("args") or {}).get("id")
            if isinstance(name, str):
                loads[run_id, event.data["call_id"]] = name
        elif event.type == "tool.result":
            name = loads.pop((run_id, event.data["call_id"]), None)
            view = states.get(run_id)
            if view is None or event.data.get("outcome") != "success":
                continue
            if name is not None and name in offered.get(run_id, {}):
                view.loaded_skills[name] = offered[run_id][name]
            artifact = event.data.get("artifact")
            if artifact is not None:
                with suppress(ValidationError):
                    view.artifacts.append(SavedArtifact.model_validate(artifact))
    return list(states.values())


def reply_text(messages: list[dict]) -> str:
    return "".join(
        part.get("content", "")
        for message in messages
        if message.get("kind") == "response"
        for part in message.get("parts", [])
        # bud's self-check notes are for the model, not the owner.
        if part.get("part_kind") == "text" and part.get("content") not in spiral_notes(message)
    )


def spiral_notes(message: dict) -> set[str]:
    mark = (message.get("metadata") or {}).get("bud_spiral") or {}
    return {note["text"] for note in mark.get("notes", [])}


def snapshot(session_id: str, events: list[Event]) -> Snapshot:
    transcript: list[Turn] = []
    blocks: dict[str, str] = {}
    run_turns: dict[str, Turn] = {}
    for event in events:
        match event.type:
            case "user.message":
                transcript.append(Turn(role="user", text=event.data["text"]))
                blocks = {}
            case "run.waiting" | "run.completed":
                report = event.data.get("report")
                text = report_text(report) if report else reply_text(event.data.get("messages", []))
                if not text:
                    continue
                run_id = event.data["run_id"]
                if run_id not in run_turns:
                    run_turns[run_id] = Turn(role="assistant", text="")
                    transcript.append(run_turns[run_id])
                run_turns[run_id].text = text
            case "task.completed" | "task.failed" | "task.paused" | "task.cancelled":
                transcript.append(
                    Turn(
                        role="assistant",
                        text=f"Task {event.data['task_id']}: {event.data['text']}",
                        task_id=event.data["task_id"],
                    )
                )
            case "text.reset" | "text.delta":
                # Logs written before text became live-only stored each streamed piece.
                run_id = event.data["run_id"]
                if run_id not in run_turns:
                    run_turns[run_id] = Turn(role="assistant", text="")
                    transcript.append(run_turns[run_id])
                block = event.data.get("block", "legacy")
                if event.type == "text.reset":
                    request = event.data["request"] + ":"
                    blocks = {
                        key: value for key, value in blocks.items() if not key.startswith(request)
                    }
                else:
                    blocks[block] = blocks.get(block, "") + event.data["text"]
                run_turns[run_id].text = "".join(blocks.values())
    recent = runs(events, summaries=True)
    state = recent[-1].status if recent else "idle"
    paused = queue_paused(events)
    active = state in ("queued", "running", "stopping")
    if paused and state not in (
        "queued",
        "running",
        "stopping",
        "awaiting_approval",
        "needs_review",
    ):
        state = "paused"
    return Snapshot(
        session_id=session_id,
        last_seq=events[-1].seq if events else 0,
        run_active=active,
        status=state,
        queue_paused=paused,
        queued_messages=queued_messages(events),
        recent_runs=recent[-20:],
        pending_approvals=[
            PendingApproval(**{key: request[key] for key in ("approval_id", "tool", "args")})
            for request in [*undecided_approvals(events), *parked_approvals(events)]
        ],
        pending_setup=pending_setups(events),
        transcript=transcript,
    )


def waiting_batch(events: list[Event]) -> list[dict[str, Any]]:
    """Approval requests for the latest waiting run, unless that run was cancelled."""
    batch: list[dict[str, Any]] = []
    waiting = False
    for event in events:
        if event.type in ("run.queued", "run.started", "run.cancelled"):
            batch, waiting = [], False
        elif event.type == "approval.requested":
            batch.append(event.data)
        elif event.type == "run.waiting":
            waiting = True
        elif event.type == "run.stop_requested":
            batch, waiting = [], False
    return batch if waiting else []


def decisions(events: list[Event]) -> dict[str, bool]:
    return {
        event.data["approval_id"]: event.type in ("approval.granted", "question.answered")
        for event in events
        if event.type in ("approval.granted", "approval.denied", "question.answered")
    }


def parked_ids(events: list[Event]) -> set[str]:
    return {event.data["approval_id"] for event in events if event.type == "approval.parked"}


def undecided_approvals(events: list[Event]) -> list[dict[str, Any]]:
    """Requests the latest waiting run still needs an answer for before it can resume.

    A parked request is not one of them: its run moved on, and it waits on its own."""
    decided, parked = decisions(events), parked_ids(events)
    return [
        req
        for req in waiting_batch(events)
        if req["approval_id"] not in decided and req["approval_id"] not in parked
    ]


def parked_approvals(events: list[Event]) -> list[dict[str, Any]]:
    """Requests set aside when the owner sent another message first. Each stays open and
    approvable until the owner decides it or bud withdraws it (THE-169)."""
    decided, parked = decisions(events), parked_ids(events)
    return [
        event.data
        for event in events
        if event.type == "approval.requested"
        and event.data["approval_id"] in parked
        and event.data["approval_id"] not in decided
    ]


def model_history(events: list[Event]) -> list[ModelMessage]:
    history: list[ModelMessage] = []
    unfinished: list[ModelMessage] = []
    user_text: str | None = None
    tools_ran: list[str] = []
    calls: dict[str, str] = {}
    permitted: set[str] = set()
    for event in events:
        match event.type:
            case "user.message":
                user_text = event.data["text"]
            case "run.queued":
                if event.data.get("text") is not None:
                    user_text = event.data["text"]
            case "tool.call":
                calls[event.data["call_id"]] = f"{event.data['tool']}({event.data['args']})"
            case "action.decided":
                if event.data["outcome"] in ("allowed", "approved"):
                    permitted.add(event.data["call_id"])
            case "tool.result":
                call = calls.pop(event.data["call_id"], "a tool")
                tools_ran.append(f"{call} returned: {event.data['result']}")
            case "run.waiting":
                unfinished.extend(ModelMessagesTypeAdapter.validate_python(event.data["messages"]))
            case "run.completed":
                unfinished.extend(ModelMessagesTypeAdapter.validate_python(event.data["messages"]))
                history += unfinished
                unfinished, tools_ran, user_text, calls, permitted = [], [], None, {}, set()
            case "run.failed" | "run.cancelled":
                history.append(
                    _failure_note(
                        event.data["reason"],
                        user_text,
                        tools_ran,
                        [value for key, value in calls.items() if key in permitted],
                    )
                )
                unfinished, tools_ran, user_text, calls, permitted = [], [], None, {}, set()
    return history + unfinished


def _failure_note(
    reason: str, user_text: str | None, tools_ran: list[str], unknown: list[str]
) -> ModelRequest:
    ran = "\n".join(f"- {line}" for line in tools_ran) or "none"
    unsure = "\n".join(f"- {line}" for line in unknown) or "none"
    note = (
        f"[bud system note] An earlier run stopped before finishing. Reason: {reason}.\n"
        f"The user had said: {user_text}\n"
        f"Tools that already executed in this attempt; do not re-execute them:\n{ran}\n"
        f"Tools granted permission without a confirmed result. Outcomes remain unverified; "
        f"report them as unknown without re-executing:\n{unsure}"
    )
    return ModelRequest(parts=[UserPromptPart(note)])
