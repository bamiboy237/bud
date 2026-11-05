"""Agent tools for the simulated world. Writes use approval IDs as once-only keys."""

from collections.abc import Callable
from typing import Any

from pydantic_ai import RunContext, Tool

from bud.world.store import World, WorldError

READ_TOOLS = {"current_time", "search_email", "read_thread", "list_events", "search_contacts"}


def _action_key(ctx: RunContext[None]) -> str:
    metadata = ctx.tool_call_metadata or {}
    return metadata.get("approval_id") or ctx.tool_call_id or ""


def build_tools(world: World) -> list[Tool]:
    def current_time() -> str:
        """Return the current local date and time for the person."""
        return world.now

    def search_email(
        query: str = "", sender: str = "", unread_only: bool = False, limit: int = 10
    ) -> Any:
        """Search the mailbox, newest first. Results include thread ids and short snippets.

        Args:
            query: Text to find in the subject, body, or sender. Empty matches everything.
            sender: Part of the sender's address.
            unread_only: Return only unread messages.
            limit: Maximum results, up to 25.
        """
        return _safely(lambda: world.search_email(query, sender, unread_only, limit))

    def read_thread(thread_id: str) -> Any:
        """Read every message in a thread without changing its unread state.

        Args:
            thread_id: The thread id from search_email.
        """
        return _safely(lambda: world.read_thread(thread_id))

    def draft_reply(thread_id: str, body: str) -> Any:
        """Save a draft reply to a thread. This does not send anything.

        Args:
            thread_id: The thread to reply to.
            body: Plain text of the reply.
        """
        return _safely(lambda: {"draft_id": world.draft_reply(thread_id, body)})

    def send_email(ctx: RunContext[None], to: list[str], subject: str, body: str) -> Any:
        """Send an email.

        Args:
            to: Recipient email addresses.
            subject: Subject line.
            body: Plain text body.
        """

        def send() -> str:
            if world.send_email(_action_key(ctx), to, subject, body):
                return f"Sent to {', '.join(to)}."
            return "Already sent; nothing new went out."

        return _safely(send)

    def list_events(start: str, end: str) -> Any:
        """List calendar events that overlap a time range.

        Args:
            start: Range start, for example 2026-11-02T00:00:00.
            end: Range end, for example 2026-11-09T00:00:00.
        """
        return _safely(lambda: world.list_events(start, end))

    def create_event(
        ctx: RunContext[None], title: str, start: str, end: str, attendees: list[str]
    ) -> Any:
        """Create a calendar event.

        Args:
            title: Event title.
            start: Start time, for example 2026-11-05T14:00:00.
            end: End time.
            attendees: Attendee email addresses.
        """

        def create() -> str:
            event_id = world.create_event(_action_key(ctx), title, start, end, attendees)
            return f"Created {event_id}." if event_id else "Already created; nothing changed."

        return _safely(create)

    def move_event(ctx: RunContext[None], event_id: str, start: str, end: str) -> Any:
        """Move an existing calendar event to a new time.

        Args:
            event_id: The event id from list_events.
            start: New start time.
            end: New end time.
        """
        return _safely(lambda: {"moved": world.move_event(_action_key(ctx), event_id, start, end)})

    def search_contacts(query: str) -> Any:
        """Find people by name or email address.

        Args:
            query: Part of a name or address.
        """
        return _safely(lambda: world.search_contacts(query))

    return [
        Tool(tool)
        for tool in (
            current_time,
            search_email,
            read_thread,
            draft_reply,
            send_email,
            list_events,
            create_event,
            move_event,
            search_contacts,
        )
    ]


def _safely(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except WorldError as exc:
        return {"error": str(exc)}
