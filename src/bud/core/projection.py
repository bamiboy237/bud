"""Folds the event log into views: a `Snapshot` for clients on first load, the message history
the model sees on its next run, and the approvals a run is waiting on.
"""

from typing import Any

from pydantic import BaseModel
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    UserPromptPart,
)

from bud.core.events import Event


class Turn(BaseModel):
    role: str
    text: str


class PendingApproval(BaseModel):
    approval_id: str
    tool: str
    args: dict[str, Any]


class Snapshot(BaseModel):
    session_id: str
    last_seq: int
    run_active: bool
    pending_approvals: list[PendingApproval]
    transcript: list[Turn]


def snapshot(session_id: str, events: list[Event]) -> Snapshot:
    transcript: list[Turn] = []
    run_active = False
    for event in events:
        match event.type:
            case "user.message":
                transcript.append(Turn(role="user", text=event.data["text"]))
            case "run.started":
                run_active = True
            case "text.delta":
                if not transcript or transcript[-1].role != "assistant":
                    transcript.append(Turn(role="assistant", text=""))
                transcript[-1].text += event.data["text"]
            case "run.waiting" | "run.completed" | "run.failed":
                run_active = False
    return Snapshot(
        session_id=session_id,
        last_seq=events[-1].seq if events else 0,
        run_active=run_active,
        pending_approvals=[
            PendingApproval(**{key: request[key] for key in ("approval_id", "tool", "args")})
            for request in undecided_approvals(events)
        ],
        transcript=transcript,
    )


def waiting_batch(events: list[Event]) -> list[dict[str, Any]]:
    """Approval requests of the run that ended waiting, if no later run has started."""
    batch: list[dict[str, Any]] = []
    waiting = False
    for event in events:
        if event.type == "run.started":
            batch, waiting = [], False
        elif event.type == "approval.requested":
            batch.append(event.data)
        elif event.type == "run.waiting":
            waiting = True
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
    """Finished runs replay verbatim. A failed run becomes a note, because its tool calls may
    have no results, which providers reject, and the model must not repeat what already ran.
    """
    history: list[ModelMessage] = []
    unfinished: list[ModelMessage] = []
    user_text: str | None = None
    tools_ran: list[str] = []
    calls: dict[str, str] = {}

    for event in events:
        match event.type:
            case "user.message":
                user_text = event.data["text"]
            case "tool.call":
                calls[event.data["call_id"]] = f"{event.data['tool']}({event.data['args']})"
            case "tool.result":
                call = calls.pop(event.data["call_id"], "a tool")
                tools_ran.append(f"{call} returned: {event.data['result']}")
            case "run.waiting":
                unfinished.extend(ModelMessagesTypeAdapter.validate_python(event.data["messages"]))
            case "run.completed":
                unfinished.extend(ModelMessagesTypeAdapter.validate_python(event.data["messages"]))
                history += unfinished
                unfinished, tools_ran, user_text, calls = [], [], None, {}
            case "run.failed":
                history.append(
                    _failure_note(event.data["reason"], user_text, tools_ran, list(calls.values()))
                )
                unfinished, tools_ran, user_text, calls = [], [], None, {}
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
        f"Tools that started with no recorded result. They may have taken effect, so tell the "
        f"user the outcome is unknown and do not repeat them:\n{unsure}"
    )
    return ModelRequest(parts=[UserPromptPart(note)])
