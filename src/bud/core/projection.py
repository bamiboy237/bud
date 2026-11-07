"""Fold session events into conversation, approval, queue, and run views."""

from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    UserPromptPart,
)

from bud.core.events import Event, MessageOrigin


class Turn(BaseModel):
    role: str
    text: str


class PendingApproval(BaseModel):
    approval_id: str
    tool: str
    args: dict[str, Any]


class QueuedMessage(BaseModel):
    message_id: str
    text: str
    origin: MessageOrigin | None = None


class RunView(BaseModel):
    run_id: str
    status: str
    reason: str | None = None


class Snapshot(BaseModel):
    session_id: str
    last_seq: int
    run_active: bool
    pending_approvals: list[PendingApproval]
    transcript: list[Turn]
    status: str = "idle"
    queue_paused: bool = False
    queued_messages: list[QueuedMessage] = Field(default_factory=list)
    recent_runs: list[RunView] = Field(default_factory=list)


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
            or length + len(item.text) + 2 > 100_000
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


def runs(events: list[Event]) -> list[RunView]:
    states: dict[str, RunView] = {}
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
            states[run_id] = RunView(run_id=run_id, status=status, reason=event.data.get("reason"))
    return list(states.values())


def snapshot(session_id: str, events: list[Event]) -> Snapshot:
    transcript: list[Turn] = []
    blocks: dict[str, str] = {}
    for event in events:
        match event.type:
            case "user.message":
                transcript.append(Turn(role="user", text=event.data["text"]))
                blocks = {}
            case "text.reset" | "text.delta":
                if not transcript or transcript[-1].role != "assistant":
                    transcript.append(Turn(role="assistant", text=""))
                block = event.data.get("block", "legacy")
                if event.type == "text.reset":
                    request = event.data["request"] + ":"
                    blocks = {
                        key: value for key, value in blocks.items() if not key.startswith(request)
                    }
                else:
                    blocks[block] = blocks.get(block, "") + event.data["text"]
                transcript[-1].text = "".join(blocks.values())
    recent = runs(events)
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
            for request in undecided_approvals(events)
        ],
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
        event.data["approval_id"]: event.type == "approval.granted"
        for event in events
        if event.type in ("approval.granted", "approval.denied")
    }


def undecided_approvals(events: list[Event]) -> list[dict[str, Any]]:
    decided = decisions(events)
    return [req for req in waiting_batch(events) if req["approval_id"] not in decided]


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
        f"Tools that already ran, which must not be repeated:\n{ran}\n"
        f"Tools that received permission with no recorded result. They may have taken effect, "
        f"so tell the user the outcome is unknown and do not repeat them:\n{unsure}"
    )
    return ModelRequest(parts=[UserPromptPart(note)])
