"""Event schema for bud's session log.
Every fact the system records is one `Event`, and `EventType` lists the kinds it can take.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

EventType = Literal[
    "session.created",
    "user.message",
    "run.started",
    "text.delta",
    "tool.call",
    "tool.result",
    "run.completed",
    "run.failed",
]

TERMINAL_RUN_EVENTS = ("run.completed", "run.failed")


class Event(BaseModel):
    id: str
    session_id: str
    seq: int
    type: EventType
    data: dict[str, Any]
    created_at: datetime
