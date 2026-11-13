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
    "read_event",
    "search_contacts",
    "list_tasks",
}

# The profile matrix (bud.core.profiles) treats these as "simulated world
# writes": they ask in strict mode and may auto-run (journaled) in higher
# modes. They stay dangerous-adjacent in one way — send_email delivers inside
# the simulation — but never touch a real account.
WRITE_TOOLS = {
    "draft_reply",
    "send_email",
    "create_event",
    "move_event",
    "delete_event",
    "rsvp_event",
    "archive_email",
    "mark_read",
    "add_task",
    "complete_task",
    "update_contact_note",
}


def _action_key(ctx: RunContext[None]) -> str:
    metadata = ctx.tool_call_metadata or {}
    if not ctx.tool_call_approved or not metadata.get("approval_id"):
        raise WorldError("Writes require an explicit approval.")
    return metadata["approval_id"]


def build_tools(world: World, *, include_ask_user: bool = True) -> list[Tool[None]]:
    """Build the world toolset.

    Evaluated runs pass include_ask_user=False: scripted evals cannot type an
    answer, so the question tool would stall every attempt.
    """

    def current_time() -> str:
        """Return the current local date and time for the person."""
        return world.now

    def search_email(
        query: str = "",
        sender: str = "",
        unread_only: bool = False,
        limit: int = 10,
        include_archived: bool = False,
    ) -> Any:
        """Search the mailbox, newest first.

        Returns the filters that were used, a count, and the matching messages (thread ids
        and short snippets). A count of 0 means nothing matches those filters.

        Archived threads are hidden unless include_archived is true.

        Args:
            query: Text to find in the subject, body, or sender. Empty matches everything.
            sender: Part of the sender's address.
            unread_only: Return only unread messages.
            limit: Maximum results, up to 25.
            include_archived: Also search threads that were archived.
        """

        def searched() -> dict[str, Any]:
            found = world.search_email(query, sender, unread_only, limit, include_archived)
            filters = {
                "query": query,
                "sender": sender,
                "unread_only": unread_only,
                "include_archived": include_archived,
            }
            return {"searched": filters, "count": len(found), "messages": found}

        return _safely(searched)

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
        """Save a draft reply in a thread; nothing is sent.

        Use only when the person asks for a draft or says not to send. When they ask
        you to email, reply to, or tell someone, use send_email instead.

        Returns the saved draft as read back from the mailbox, so report it directly;
        no separate read is needed. Returns the exact saved draft id, thread, recipients,
        and body. Replaying the
        same approval returns the original draft instead of creating another one.

        Args:
            thread_id: The thread to reply to. Read it first to confirm the recipient.
            body: Exact plain text of the reply, from 1 to 20,000 characters.
        """

        def draft() -> Any:
            return world.draft_reply(_action_key(ctx), thread_id, body)

        return _safely(draft)

    def send_email(ctx: RunContext[None], to: list[str], subject: str, body: str) -> Any:
        """Send an email now; the person approves it before it goes out.

        Use when the person asks you to email, send, reply to, or tell someone,
        including a reply on an existing thread (subject "Re: <thread subject>").

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

    def list_events(start: str, end: str, timezone: str | None = None) -> Any:
        """Check calendar events overlapping a range, in UTC order, before proposing a write.

        Resolve unclear dates or timezone with the user first. Times need explicit
        offsets consistent with timezone; DST folds require choosing the intended offset.
        Returns the range and timezone that were searched, a count, and the events (ids,
        local/UTC times, attendees, organizer, rsvp state, description, protected status).
        A count of 0 means that whole range is free.

        Args:
            start: Range start, for example 2026-11-02T00:00:00-06:00.
            end: Range end, for example 2026-11-09T00:00:00-06:00.
            timezone: IANA zone matching the input offsets; defaults to America/Chicago.
                For Z or +00:00 inputs use UTC.
        """

        def covered() -> dict[str, Any]:
            events = world.list_events(start, end, timezone)
            searched = {"start": start, "end": end, "timezone": timezone or world.info["timezone"]}
            return {"searched": searched, "count": len(events), "events": events}

        return _safely(covered)

    def read_event(event_id: str) -> Any:
        """Read the persisted event to verify an approved calendar write.

        Confirm the exact title, attendees, and local/UTC times before reporting success.

        Args:
            event_id: Exact event id from list_events, create_event, or move_event.
        """
        return _safely(lambda: world.read_event(event_id))

    def create_event(
        ctx: RunContext[None],
        title: str,
        start: str,
        end: str,
        attendees: list[str],
        timezone: str | None = None,
    ) -> Any:
        """Create an approved synthetic event.

        Attendees receive an invitation, so an event with attendees is visible to other people.
        Pass an empty attendees list for a personal block only the person sees.
        Check list_events first. Clarify ambiguous dates, attendees, timezones, or
        DST folds with the user. Bookings must fit the fictional seed's weekday
        09:00–17:00 America/Chicago hours and cannot overlap any event, including
        Focus Blocks. Returns applied and the saved event, read back
        from the calendar after the write, so report it directly; a replay has applied=false
        and returns current state without writing again.

        Args:
            title: Event title.
            start: Explicit-offset start, for example 2026-11-05T14:00:00-06:00.
            end: Explicit-offset end, strictly after start.
            attendees: Attendee email addresses.
            timezone: IANA zone matching the input offsets; defaults to America/Chicago.
                For Z or +00:00 inputs use UTC.
        """

        def create() -> Any:
            return world.create_event(_action_key(ctx), title, start, end, attendees, timezone)

        return _safely(create)

    def move_event(
        ctx: RunContext[None], event_id: str, start: str, end: str, timezone: str | None = None
    ) -> Any:
        """Move an approved synthetic event, preserving its title and attendees.

        Check list_events for the target range first; resolve ambiguity with the user.
        Focus Blocks cannot be moved, and no booking may overlap another event or
        fall outside the fictional seed's weekday 09:00–17:00 America/Chicago hours.
        Returns applied and the saved event, read back from the calendar after the
        write, so report it directly. A replay returns current state with
        applied=false without moving again.

        Args:
            event_id: The event id from list_events.
            start: Explicit-offset new start, for example 2026-11-06T11:00:00-06:00.
            end: Explicit-offset new end, strictly after start.
            timezone: IANA zone matching the input offsets; defaults to America/Chicago.
                For Z or +00:00 inputs use UTC.
        """

        def move() -> Any:
            return world.move_event(_action_key(ctx), event_id, start, end, timezone)

        return _safely(move)

    def delete_event(ctx: RunContext[None], event_id: str) -> Any:
        """Permanently delete a calendar event. This cannot be undone.

        Use only when the person asks to delete or cancel a specific event. Never use it to
        decline an invitation (use rsvp_event) or to make room for something else. Attendees
        lose the event too. Focus Block events are protected and cannot be deleted.

        Args:
            event_id: The event id from list_events.
        """
        return _safely(lambda: world.delete_event(_action_key(ctx), event_id))

    def rsvp_event(ctx: RunContext[None], event_id: str, response: str) -> Any:
        """Answer a calendar invitation. The organizer sees your answer right away.

        Use only when the person asks you to accept, decline, or tentatively accept a
        specific invitation. Reading or summarising invitations never needs an answer.
        Invitations show rsvp "needs_action" in list_events; events the person organized
        have rsvp "none" and cannot be answered.

        Args:
            event_id: The invitation's event id from list_events.
            response: One of accepted, declined, tentative.
        """
        return _safely(lambda: world.rsvp_event(_action_key(ctx), event_id, response))

    def archive_email(ctx: RunContext[None], thread_id: str) -> Any:
        """Move a whole thread out of the inbox into the archive. It stays searchable.

        Private and reversible, but the person stops seeing the thread in their inbox, so
        archive only the threads they asked for. Never archive a thread just because it
        looks unimportant, and never one that is waiting for a reply or a payment.

        Args:
            thread_id: The thread id from search_email.
        """
        return _safely(lambda: world.archive_email(_action_key(ctx), thread_id))

    def mark_read(ctx: RunContext[None], thread_id: str) -> Any:
        """Mark every message in a thread as read. Private and reversible.

        Use only when the person asks, or says they have handled the thread. Reading a thread
        with read_thread already leaves its unread state alone on purpose.

        Args:
            thread_id: The thread id from search_email.
        """
        return _safely(lambda: world.mark_read(_action_key(ctx), thread_id))

    def list_tasks(include_done: bool = False) -> Any:
        """List the person's personal to-do list, soonest due first.

        Args:
            include_done: Also list tasks that are already done.
        """
        return _safely(lambda: world.list_tasks(include_done))

    def add_task(ctx: RunContext[None], title: str, due: str = "") -> Any:
        """Add an item to the person's private to-do list. Nobody else sees it.

        Use when the person asks you to remember, track, or add a to-do. Check list_tasks
        first so you do not add a duplicate. Returns the saved task with its id.

        Args:
            title: Short task title, at most 200 characters.
            due: Optional due date such as 2026-11-06; empty for none.
        """
        return _safely(lambda: world.add_task(_action_key(ctx), title, due))

    def complete_task(ctx: RunContext[None], task_id: str) -> Any:
        """Mark a to-do item done. Private and reversible.

        Use only when the person says the task is done or asks you to tick it off.

        Args:
            task_id: The task id from list_tasks.
        """
        return _safely(lambda: world.complete_task(_action_key(ctx), task_id))

    def update_contact_note(ctx: RunContext[None], email: str, note: str) -> Any:
        """Replace the private note kept on a contact. Nobody else sees it.

        The new note replaces the old one, so include any existing note text you want to keep
        (search_contacts shows it). Use when the person asks you to remember something about
        a contact.

        Args:
            email: The contact's exact email address from search_contacts.
            note: The full new note, at most 1,000 characters.
        """
        return _safely(lambda: world.update_contact_note(_action_key(ctx), email, note))

    def search_contacts(query: str) -> Any:
        """Find people by name or email address.

        Args:
            query: Part of a name or address.
        """
        return _safely(lambda: world.search_contacts(query))

    def ask_user(ctx: RunContext[None], question: str, choices: list[str] | None = None) -> str:
        """Ask the person a question and wait for their answer.

        Use this only when a missing preference or decision genuinely blocks the
        task and no available tool can supply the fact: prefer reading data with
        tools first. It pauses and returns the person's exact reply. Offer 2-5
        concrete choices when the options are known; omit choices for a
        free-text answer. Ask one question at a time. For risky actions, still
        call the action tool so it goes through approval.

        Args:
            question: One complete question for the person.
            choices: Exact option labels, 2 to 5 items, when known.
        """
        metadata = ctx.tool_call_metadata or {}
        if ctx.tool_call_approved and metadata.get("question_answer"):
            return str(metadata["question_answer"])
        raise WorldError("ask_user needs an answer from the person through the question flow.")

    fns = [
        current_time,
        search_email,
        read_thread,
        list_drafts,
        read_draft,
        draft_reply,
        send_email,
        list_events,
        read_event,
        create_event,
        move_event,
        delete_event,
        rsvp_event,
        archive_email,
        mark_read,
        list_tasks,
        add_task,
        complete_task,
        update_contact_note,
        search_contacts,
    ]
    if include_ask_user:
        fns.append(ask_user)
    tools: list[Tool[None]] = [Tool(tool) for tool in fns]
    return tools


def _safely(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except WorldError as exc:
        return {"error": str(exc)}
