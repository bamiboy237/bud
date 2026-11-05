"""Agent tools for the simulated world. Writes use approval IDs as once-only keys."""

from collections.abc import Callable
from typing import Any

from pydantic_ai import RunContext, Tool

from bud.world.store import World, WorldError

READ_TOOLS = {
    "current_time",
    "search_email",
    "read_thread",
    "list_drafts",
    "read_draft",
    "list_events",
    "search_contacts",
}


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

    def list_drafts(query: str = "", limit: int = 10, offset: int = 0) -> Any:
        """Find saved drafts without needing a previous conversation.

        Results contain draft ids, thread ids, recipients, subjects, and short snippets.
        Continue with next_offset until it is null. Read a candidate with read_draft.

        Args:
            query: Literal text in the subject, body, or recipient address; at most 500 characters.
            limit: Maximum drafts per page, from 1 to 25.
            offset: Number of matching drafts to skip; use next_offset for the next page.
        """
        return _safely(lambda: world.list_drafts(query, limit, offset))

    def read_draft(draft_id: str, body_offset: int = 0, max_chars: int = 8_000) -> Any:
        """Read a saved draft and its exact thread and recipients without changing it.

        The body is a character page, not a summary. Continue with next_body_offset
        until it is null before claiming the whole body was read.

        Args:
            draft_id: Exact draft id from list_drafts or draft_reply.
            body_offset: Character offset in the body, starting at zero.
            max_chars: Maximum body characters to return, from 1 to 20,000.
        """
        return _safely(lambda: world.read_draft(draft_id, body_offset, max_chars))

    def draft_reply(ctx: RunContext[None], thread_id: str, body: str) -> Any:
        """Save an approved draft reply, without sending it, then use read_draft to verify it.

        Returns the exact saved draft id, thread, recipients, and body. Replaying the
        same approval returns the original draft instead of creating another one.

        Args:
            thread_id: The thread to reply to. Read it first to confirm the recipient.
            body: Exact plain text of the reply, from 1 to 20,000 characters.
        """

        def draft() -> Any:
            if not ctx.tool_call_approved or not (ctx.tool_call_metadata or {}).get("approval_id"):
                raise WorldError("Saving a draft requires an explicit approval.")
            return world.draft_reply(_action_key(ctx), thread_id, body)

        return _safely(draft)

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
            list_drafts,
            read_draft,
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
