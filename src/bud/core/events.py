"""Event schema for bud's session log.
Every fact the system records is one `Event`, and `EventType` lists the kinds it can take.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

EventType = Literal[
    "session.created",
    "user.message",
    "run.queued",
    "run.started",
    "run.recovered",
    "text.reset",
    "text.delta",
    "tool.call",
    "action.decided",
    "tool.result",
    "approval.requested",
    "approval.granted",
    "approval.denied",
    "run.waiting",
    "run.completed",
    "run.failed",
]

RUN_END_EVENTS = ("run.waiting", "run.completed", "run.failed")


class Event(BaseModel):
    id: str
    session_id: str
    seq: int
    type: EventType
    data: dict[str, Any]
    created_at: datetime
