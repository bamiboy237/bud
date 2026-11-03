"""Folds the event log into views: a `Snapshot` for clients on first load, and the message
history the model sees on its next run.
"""

from typing import Any

from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from bud.core.events import Event


class Turn(BaseModel):
    role: str
    text: str


class Snapshot(BaseModel):
    session_id: str
    last_seq: int
    run_active: bool
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
            case "run.completed" | "run.failed":
                run_active = False
    return Snapshot(
        session_id=session_id,
        last_seq=events[-1].seq if events else 0,
        run_active=run_active,
        transcript=transcript,
    )


def model_history(events: list[Event]) -> list[ModelMessage]:
    history: list[Any] = []
    for event in events:
        if event.type == "run.completed":
            history.extend(event.data["messages"])
    return ModelMessagesTypeAdapter.validate_python(history)
