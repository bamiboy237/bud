"""Trusted descriptions of what a write's ids point to, for the approval reviewer.

A model that proposes `move_event(event_id="e4", ...)` has told the reviewer nothing about e4.
Asking the model to explain would let a compromised model write its own alibi, so code looks
the target up in bud's own store instead. These strings are data computed here, never model
text. Strangers wrote some of the underlying fields (an email subject, an invitation title),
so each field is shortened and flattened to one line, and the reviewer's rules still treat
the whole block as data, not instructions.

An id that does not exist says so plainly: an invented id is evidence the call is wrong.
"""

from collections.abc import Callable
from typing import Any

from bud.harness.review import Describer
from bud.world.store import World, WorldError

FIELD_CHARS = 120
LIST_ITEMS = 8


def _line(value: object) -> str:
    return " ".join(str(value).split())[:FIELD_CHARS]


def _people(world: World, addresses: list[str]) -> str:
    shown = []
    for address in addresses[:LIST_ITEMS]:
        match = next((c for c in world.search_contacts(address) if c["email"] == address), None)
        shown.append(f"{match['name']} <{address}>" if match else f"{address} (not a contact)")
    extra = len(addresses) - LIST_ITEMS
    return "; ".join(shown) + (f"; and {extra} more" if extra > 0 else "") if shown else "nobody"


def _event(world: World, args: dict[str, Any]) -> str:
    event_id = str(args.get("event_id", ""))
    try:
        event = world.read_event(event_id)
    except WorldError:
        return f"Event {_line(event_id)!r} does not exist."
    organizer = event.get("organizer") or "the owner (their own event)"
    return (
        f"Event {event_id}: title {_line(event['title'])!r}, {event['start']} to {event['end']}, "
        f"organizer {_line(organizer)}, attendees: {_people(world, event['attendees'])}; "
        f"the owner's RSVP is {event['rsvp']}."
        + (" This is a protected Focus Block." if event["protected"] else "")
    )


def _thread(world: World, args: dict[str, Any]) -> str:
    thread_id = str(args.get("thread_id", ""))
    try:
        thread = world.read_thread(thread_id)
    except WorldError:
        return f"Thread {_line(thread_id)!r} does not exist."
    messages = thread["messages"]
    senders = sorted({m["sender"] for m in messages if m["folder"] != "sent"})
    return (
        f"Thread {thread_id}: subject {_line(thread['subject'])!r}, {len(messages)} message(s), "
        f"from {_people(world, senders)}; the last message is from "
        f"{_line(messages[-1]['sender'])}. A reply goes to the other participants."
    )


def _task(world: World, args: dict[str, Any]) -> str:
    task_id = str(args.get("task_id", ""))
    task = next((t for t in world.list_tasks(include_done=True) if t["id"] == task_id), None)
    if task is None:
        return f"Task {_line(task_id)!r} does not exist."
    return (
        f"Task {task_id}: {_line(task['title'])!r}, due {task['due'] or 'no date'}, "
        f"{'already done' if task['done'] else 'not done yet'}."
    )


def _contact(world: World, args: dict[str, Any]) -> str:
    email = str(args.get("email", ""))
    contact = next((c for c in world.search_contacts(email) if c["email"] == email), None)
    if contact is None:
        return f"{_line(email)!r} is not a contact."
    return (
        f"Contact {_line(contact['name'])} <{email}>, {_line(contact['relationship'])}. "
        f"Their current private note is {_line(contact['note'])!r}; the call replaces it."
    )


def _new_event(world: World, args: dict[str, Any]) -> str:
    guests = [str(a) for a in args.get("attendees") or []]
    if not guests:
        return "A personal block: no guests, so nobody else sees it."
    return f"Guests who would receive an invitation: {_people(world, guests)}."


def build_describers(world: World) -> dict[str, Describer]:
    """One describer per simulated-world write that takes an id or an address."""

    def bind(function: Callable[[World, dict[str, Any]], str]) -> Describer:
        return lambda args: function(world, args)

    return {
        "move_event": bind(_event),
        "delete_event": bind(_event),
        "rsvp_event": bind(_event),
        "create_event": bind(_new_event),
        "archive_email": bind(_thread),
        "mark_read": bind(_thread),
        "draft_reply": bind(_thread),
        "complete_task": bind(_task),
        "update_contact_note": bind(_contact),
    }
