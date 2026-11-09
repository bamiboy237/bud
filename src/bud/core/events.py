"""Event schema for bud's session log.
Every fact the system records is one `Event`, and `EventType` lists the kinds it can take.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "session.created",
    "session.primary",
    "user.message",
    "message.queued",
    "message.edited",
    "message.cancelled",
    "message.dispatched",
    "queue.paused",
    "queue.resumed",
    "run.queued",
    "run.started",
    "run.recovered",
    "text.reset",
    "text.delta",
    "tool.call",
    "action.decided",
    "tool.result",
    "context.preparing",
    "context.ready",
    "context.failed",
    "context.discarded",
    "context.compacted",
    "approval.requested",
    "approval.granted",
    "approval.denied",
    "run.waiting",
    "run.completed",
    "run.failed",
    "run.stop_requested",
    "run.cancelled",
    "task.created",
    "task.approval",
    "task.completed",
    "task.failed",
    "task.cancelled",
]

RUN_END_EVENTS = ("run.waiting", "run.completed", "run.failed", "run.cancelled")


class MessageOrigin(BaseModel):
    """Reply routing metadata, not identity or permission to execute an action."""

    channel: Literal["api", "terminal", "web", "imessage"] = "api"
    client_id: str = Field(
        default="default", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )


class Event(BaseModel):
    id: str
    session_id: str
    seq: int
    type: EventType
    data: dict[str, Any]
    created_at: datetime
    # Live events (streamed text) reach open readers only and are never stored.
    live: bool = False
