"""Bounded native Calendar preflight and read-back, not an account/approval layer.

The caller must supply account-fenced reads, an approved once-only dispatch, and
a synchronous durable confirmation callback. This module never retries a write.
Availability covers one chosen calendar, not guests' calendars. Google can change
between LIST/GET/PATCH: the Composio PATCH schema has no If-Match option.

Public contracts: https://docs.composio.dev/api/tools/GOOGLECALENDAR_CREATE_EVENT
and GOOGLECALENDAR_{PATCH_EVENT,EVENTS_GET,EVENTS_LIST,GET_CALENDAR} at that base.
CREATE returns data.response_data; PATCH and EVENTS_GET return data directly;
GET_CALENDAR returns data.calendar_data. Keep these distinct, not heuristic.
"""

import inspect
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime, timedelta
from email.headerregistry import Address
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bud.timezones import CalendarTimeError, calendar_time

CREATE = "GOOGLECALENDAR_CREATE_EVENT"
PATCH = "GOOGLECALENDAR_PATCH_EVENT"
GET = "GOOGLECALENDAR_EVENTS_GET"
LIST = "GOOGLECALENDAR_EVENTS_LIST"
GET_CALENDAR = "GOOGLECALENDAR_GET_CALENDAR"

# Supported-operation bounds, not guesses about the person's scheduling preferences.
MAX_DURATION = timedelta(hours=24)
MAX_ATTENDEES = 100
MAX_SUMMARY_CHARS = 500
MAX_PAGES = 4
PAGE_SIZE = 250
MAX_EVENTS = MAX_PAGES * PAGE_SIZE

Call = Callable[[str, dict[str, Any]], Awaitable[Any]]
Confirm = Callable[[dict[str, str]], None]

_COMMON = {"calendar_id", "timezone", "send_updates"}
_CREATE_FIELDS = _COMMON | {
    "summary",
    "start_datetime",
    "end_datetime",
    "attendees",
    "create_meeting_room",
    "exclude_organizer",
}
_PATCH_FIELDS = _COMMON | {"event_id", "start_time", "end_time", "expected_etag"}
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)
_VOLATILE_FIELDS = {"start", "end", "etag", "updated", "sequence", "display_url", "htmlLink"}
_NOTICE = (
    "Only the chosen calendar was checked, not other calendars or attendee availability. "
    "The preflight and write are not atomic at Google."
)


class CalendarSafetyError(ValueError):
    """A safe, bounded explanation, never a raw provider exception."""


def operation_schema(name: str, native_schema: dict[str, Any]) -> dict[str, Any]:
    """Narrow the native operation to the same explicit fields we execute.

    expected_etag is a local precondition, not a claimed Composio parameter.
    Refuse schema drift instead of substituting a broader mutation tool.
    """
    fields = _CREATE_FIELDS if name == CREATE else _PATCH_FIELDS if name == PATCH else None
    if fields is None:
        raise CalendarSafetyError("Unsupported calendar operation.")
    properties = native_schema.get("properties", {})
    if not isinstance(properties, dict) or fields - {"expected_etag"} - properties.keys():
        raise CalendarSafetyError("Required native calendar parameters are unavailable.")
    selected = {key: deepcopy(properties[key]) for key in sorted(fields) if key != "expected_etag"}
    for value in selected.values():
        value.pop("default", None)
    time_fields = (
        ("start_datetime", "end_datetime") if name == CREATE else ("start_time", "end_time")
    )
    for key in time_fields:
        selected[key]["pattern"] = _TIMESTAMP.pattern
        selected[key]["description"] = (
            "Exact approved date/time with seconds and an explicit UTC offset matching timezone. "
            "Both start and end are required; no implicit duration or timezone is used."
        )
    if name == CREATE:
        selected["attendees"]["items"] = {"type": "string"}
        selected["attendees"]["maxItems"] = MAX_ATTENDEES
        selected["summary"]["maxLength"] = MAX_SUMMARY_CHARS
        selected["create_meeting_room"]["const"] = False
        selected["create_meeting_room"]["description"] = (
            "Must be false: Bud does not implicitly create a Google Meet link."
        )
        selected["exclude_organizer"]["const"] = True
        selected["exclude_organizer"]["description"] = (
            "Must be true: prevent automatic self-attendee insertion. Include every desired "
            "attendee explicitly. This does not remove the calendar's organizer identity."
        )
    else:
        selected["expected_etag"] = {
            "type": "string",
            "minLength": 1,
            "description": "Copy etag from the event read before approval. Bud checks this "
            "local precondition against a fresh read and never sends it to Composio.",
        }
    return {
        "type": "object",
        "properties": selected,
        "required": sorted(fields),
        "additionalProperties": False,
    }


def _text(value: Any, label: str, limit: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > limit
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise CalendarSafetyError(
            f"{label} must be explicit, nonempty, and within supported bounds."
        )
    return value


def _zone(value: Any) -> ZoneInfo:
    name = _text(value, "IANA timezone", 128)
    if name != "UTC" and "/" not in name:
        raise CalendarSafetyError("Use an explicit IANA timezone, not a timezone abbreviation.")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CalendarSafetyError("The IANA timezone could not be verified.") from exc


def _instant(value: Any, zone: ZoneInfo | None = None) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise CalendarSafetyError(
            "Use complete ISO times with explicit offsets, such as 2026-11-05T14:00:00-06:00."
        )
    try:
        return calendar_time(value, zone.key if zone else "UTC", stored=zone is None)
    except CalendarTimeError as exc:
        raise CalendarSafetyError(str(exc)) from exc


def _email(value: Any) -> str:
    email = _text(value, "Attendee email", 254)
    try:
        address = Address(addr_spec=email)
        if (
            not address.username
            or not address.domain
            or address.addr_spec.casefold() != email.casefold()
        ):
            raise ValueError
    except ValueError as exc:
        raise CalendarSafetyError(
            "Attendees must be explicit bare email addresses, not names."
        ) from exc
    return email.casefold()


def _attendees(event: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if event.get("attendeesOmitted") not in (None, False):
        raise CalendarSafetyError(
            "Attendee metadata is incomplete; no safe verification is possible."
        )
    values = event.get("attendees", [])
    if not isinstance(values, list) or len(values) > MAX_ATTENDEES:
        raise CalendarSafetyError("Attendee metadata exceeds the supported scope.")
    result = {}
    for attendee in values:
        if not isinstance(attendee, dict):
            raise CalendarSafetyError("Malformed attendee metadata.")
        email = _email(attendee.get("email"))
        if email in result:
            raise CalendarSafetyError("Duplicate attendee metadata.")
        result[email] = {**attendee, "email": email}
    return result


def _data(result: Any) -> dict[str, Any]:
    if (
        not isinstance(result, dict)
        or result.get("successful") is not True
        or result.get("error")
        or not isinstance(result.get("data"), dict)
    ):
        raise CalendarSafetyError("The provider result could not be verified.")
    return result["data"]


def _validate(
    name: str, args: dict[str, Any]
) -> tuple[dict[str, Any], datetime, datetime, ZoneInfo]:
    if name not in {CREATE, PATCH}:
        raise CalendarSafetyError(
            "Only ordinary timed CREATE_EVENT and time-only PATCH_EVENT are supported."
        )
    fields = _CREATE_FIELDS if name == CREATE else _PATCH_FIELDS
    if not isinstance(args, dict) or args.keys() != fields:
        raise CalendarSafetyError(
            "Supply exactly the supported fields; omitted defaults and unrelated mutations "
            "are refused."
        )
    native = deepcopy(args)
    calendar_id = _text(native["calendar_id"], "Chosen calendar ID")
    if calendar_id.startswith("ca_"):
        raise CalendarSafetyError(
            "Use the chosen Google calendar ID, not a Composio connection ID."
        )
    zone = _zone(native["timezone"])
    if native["send_updates"] not in ("all", "externalOnly", "none"):
        raise CalendarSafetyError("Explicit send_updates must be all, externalOnly, or none.")
    start_field, end_field = (
        ("start_datetime", "end_datetime") if name == CREATE else ("start_time", "end_time")
    )
    begin = _instant(native[start_field], zone)
    finish = _instant(native[end_field], zone)
    if not timedelta(0) < finish - begin <= MAX_DURATION:
        raise CalendarSafetyError(
            "Supported timed events need an explicit duration greater than 0 and at most 24 hours."
        )
    if name == CREATE:
        _text(native["summary"], "Event summary", MAX_SUMMARY_CHARS)
        if _protected({"summary": native["summary"]}):
            raise CalendarSafetyError(
                "Protected event creation is outside the ordinary-event scope."
            )
        if native["create_meeting_room"] is not False or native["exclude_organizer"] is not True:
            raise CalendarSafetyError(
                "Set create_meeting_room=false and exclude_organizer=true. "
                "Include every desired attendee explicitly; no hidden Meet or self-attendee "
                "insertion."
            )
        attendees = native["attendees"]
        if not isinstance(attendees, list) or len(attendees) > MAX_ATTENDEES:
            raise CalendarSafetyError(
                "Supply an explicit attendee list of at most 100 bare emails."
            )
        emails = [_email(value) for value in attendees]
        if len(set(emails)) != len(emails):
            raise CalendarSafetyError("Duplicate attendee emails are not supported.")
    else:
        _text(native["event_id"], "Exact event ID")
        _text(native.pop("expected_etag"), "Approved expected_etag")
    return native, begin, finish, zone


def _protected(event: dict[str, Any]) -> bool:
    title = event.get("summary") or ""
    if not isinstance(title, str):
        raise CalendarSafetyError("Malformed calendar event title.")
    return (
        event.get("eventType") in ("focusTime", "outOfOffice")
        or title.strip().casefold() == "focus block"
    )


def _midnight(value: Any, zone: ZoneInfo) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise CalendarSafetyError("Malformed all-day calendar date; availability is unknown.")
    try:
        return calendar_time(f"{value}T00:00:00", zone.key, stored=True)
    except CalendarTimeError as exc:
        raise CalendarSafetyError(
            "Invalid or ambiguous all-day calendar date; availability is unknown."
        ) from exc


def _event_range(event: dict[str, Any], calendar_zone: ZoneInfo) -> tuple[datetime, datetime]:
    start, end = event.get("start"), event.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        raise CalendarSafetyError("Missing event times; availability is unknown.")
    if event.get("endTimeUnspecified") not in (None, False):
        raise CalendarSafetyError("Unspecified event end; availability is unknown.")
    if start.get("date") is not None or end.get("date") is not None:
        if start.get("dateTime") is not None or end.get("dateTime") is not None:
            raise CalendarSafetyError("Mixed all-day/timed boundaries; availability is unknown.")
        begin, finish = (
            _midnight(start.get("date"), calendar_zone),
            _midnight(end.get("date"), calendar_zone),
        )
    else:
        # Google may format offsets in a requested response zone, independently of
        # the event's stored display timeZone. The offset identifies the instant.
        begin, finish = _instant(start.get("dateTime")), _instant(end.get("dateTime"))
        for boundary in (start, end):
            if boundary.get("timeZone") is not None:
                _zone(boundary["timeZone"])
    if begin >= finish:
        raise CalendarSafetyError("Inverted event times; availability is unknown.")
    return begin, finish


def _ordinary(event: dict[str, Any], event_id: str, calendar_zone: ZoneInfo) -> None:
    if event.get("id") != event_id:
        raise CalendarSafetyError("The returned event ID does not match the requested event.")
    if (
        event.get("eventType", "default") != "default"
        or _protected(event)
        or event.get("status") not in ("confirmed", "tentative")
        or event.get("locked") not in (None, False)
        or event.get("recurrence") not in (None, [])
        or event.get("recurringEventId") is not None
        or event.get("originalStartTime") is not None
        or any(
            event.get(key)
            for key in (
                "focusTimeProperties",
                "outOfOfficeProperties",
                "workingLocationProperties",
                "birthdayProperties",
            )
        )
    ):
        raise CalendarSafetyError(
            "Only ordinary nonrecurring, unlocked, noncancelled events may be moved. "
            "Protected and special events are outside scope."
        )
    if any(event.get(key, {}).get("date") is not None for key in ("start", "end")):
        raise CalendarSafetyError("All-day events are outside the supported move scope.")
    _event_range(event, calendar_zone)
    _attendees(event)


def _metadata(event: dict[str, Any]) -> dict[str, Any]:
    preserved = {
        key: deepcopy(value) for key, value in event.items() if key not in _VOLATILE_FIELDS
    }
    preserved["attendees"] = _attendees(event)
    return preserved


async def _availability(
    read: Call,
    calendar_id: str,
    calendar_zone: ZoneInfo,
    begin: datetime,
    finish: datetime,
    exclude: str | None,
) -> dict[str, Any]:
    # Google ignores milliseconds in LIST bounds. Round outward, then compare
    # exact UTC instants locally so a fractional end cannot hide a real conflict.
    filters = {
        "calendarId": calendar_id,
        "timeMin": begin.replace(microsecond=0).isoformat(),
        "timeMax": (
            finish.replace(microsecond=0) + timedelta(seconds=bool(finish.microsecond))
        ).isoformat(),
        "timeZone": calendar_zone.key,
        "singleEvents": True,
        "showDeleted": False,
        "showHiddenInvitations": True,
        "maxResults": PAGE_SIZE,
    }
    token = None
    seen = set()
    total = 0
    conflicts = []
    for page in range(1, MAX_PAGES + 1):
        request = {**filters, **({"pageToken": token} if token is not None else {})}
        data = _data(await read(LIST, request))
        if data.get("accessRole") not in ("writer", "owner"):
            raise CalendarSafetyError("Full writable-calendar coverage could not be verified.")
        if data.get("timeZone") != calendar_zone.key:
            raise CalendarSafetyError(
                "Calendar timezone changed or is missing; availability is unknown."
            )
        items = data.get("items")
        if not isinstance(items, list) or len(items) > PAGE_SIZE:
            raise CalendarSafetyError("Malformed calendar page; availability is unknown.")
        total += len(items)
        if total > MAX_EVENTS:
            raise CalendarSafetyError(
                "Calendar event coverage limit reached; availability is unknown."
            )
        for event in items:
            if not isinstance(event, dict):
                raise CalendarSafetyError("Malformed calendar event; availability is unknown.")
            event_id = _text(event.get("id"), "Returned event ID")
            if event.get("status") == "cancelled":
                continue
            if event.get("status") not in ("confirmed", "tentative"):
                raise CalendarSafetyError("Unknown event status; availability is unknown.")
            _attendees(event)
            protected = _protected(event)
            transparency = event.get("transparency", "opaque")
            if transparency not in ("opaque", "transparent"):
                raise CalendarSafetyError("Unknown event transparency; availability is unknown.")
            start, end = _event_range(event, calendar_zone)
            if event_id == exclude or (transparency == "transparent" and not protected):
                continue
            if start < finish and end > begin and len(conflicts) < 10:
                conflicts.append({"event_id": event_id, "protected": protected})
        token = data.get("nextPageToken")
        if token is None or token == "":
            return {
                "calendar_id": calendar_id,
                "pages": page,
                "events_checked": total,
                "conflicts": conflicts,
                "scope": "chosen_calendar_only",
            }
        _text(token, "Calendar continuation token", 4096)
        if token in seen:
            raise CalendarSafetyError(
                "Repeated calendar continuation token; availability is unknown."
            )
        seen.add(token)
    raise CalendarSafetyError(
        "Calendar page limit reached with a continuation token; no write is safe."
    )


def _verification(
    event: dict[str, Any],
    event_id: str,
    args: dict[str, Any],
    name: str,
    begin: datetime,
    finish: datetime,
    calendar_zone: ZoneInfo,
    before: dict[str, Any] | None,
) -> dict[str, Any]:
    _ordinary(event, event_id, calendar_zone)
    actual_begin, actual_finish = _event_range(event, calendar_zone)
    mismatches = []
    if (actual_begin, actual_finish) != (begin, finish):
        mismatches.append("times")
    if any(event[key].get("timeZone") != args["timezone"] for key in ("start", "end")):
        mismatches.append("timezone")
    actual_attendees = sorted(_attendees(event))
    if name == CREATE:
        if event.get("summary") != args["summary"]:
            mismatches.append("summary")
        if actual_attendees != sorted(_email(value) for value in args["attendees"]):
            mismatches.append("attendees")
        if event.get("conferenceData") or event.get("hangoutLink"):
            mismatches.append("unexpected_conference")
    elif before != _metadata(event):
        mismatches.append("preserved_metadata")
    return {
        "matched": not mismatches,
        "mismatches": mismatches,
        "read_back": {
            "event_id": event_id,
            "summary": (event.get("summary") or "")[:MAX_SUMMARY_CHARS],
            "start_utc": actual_begin.isoformat(),
            "end_utc": actual_finish.isoformat(),
            "timezone": args["timezone"],
            "attendee_emails": actual_attendees,
        },
    }


async def execute_calendar(
    name: str,
    args: dict[str, Any],
    read: Call,
    write: Call,
    confirm: Confirm,
) -> dict[str, Any]:
    """Preflight one already-approved operation, dispatch once, and verify.

    ``read(name, args)`` must use the caller's account/binding-fenced native read.
    ``write(name, args)`` must use its approved, once-only fenced dispatch; no
    expected_etag is sent to Composio. ``confirm(receipt)`` is synchronous and
    must durably persist {action, calendar_id, event_id} before returning. A
    successful write is recorded before the first read-back await. Cancellation
    propagates; uncertain dispatch recovery belongs to the caller's durable fence.

    Preflight failures have executed=false. Once write is awaited, an exception
    or unconfirmed response is outcome=unknown, never permission to retry.
    Confirmed created/updated receipts remain separate from verification errors.
    No real work-hour policy, multi-calendar free/busy, or atomicity is implied.
    """
    try:
        if inspect.iscoroutinefunction(confirm):
            raise CalendarSafetyError("The durable confirmation callback must be synchronous.")
        native, begin, finish, _ = _validate(name, args)
        metadata = _data(await read(GET_CALENDAR, {"calendar_id": native["calendar_id"]}))
        calendar = metadata.get("calendar_data")
        if not isinstance(calendar, dict):
            raise CalendarSafetyError("Chosen calendar metadata is unavailable.")
        calendar_id = _text(calendar.get("id"), "Resolved calendar ID")
        if calendar_id == "primary" or calendar_id.startswith("ca_"):
            raise CalendarSafetyError("The chosen calendar could not be resolved to a concrete ID.")
        if native["calendar_id"] != "primary" and calendar_id != native["calendar_id"]:
            raise CalendarSafetyError("Calendar metadata does not match the chosen calendar.")
        calendar_zone = _zone(calendar.get("timeZone"))
        native["calendar_id"] = calendar_id
        availability = await _availability(
            read,
            calendar_id,
            calendar_zone,
            begin,
            finish,
            native.get("event_id"),
        )
        if availability["conflicts"]:
            return {
                "successful": False,
                "executed": False,
                "availability": availability,
                "error": "The approved interval overlaps busy/protected time. No write executed.",
            }
        before = None
        if name == PATCH:
            # After potentially slow pagination, read the target as near as possible
            # to dispatch. This catches stale approvals, not a Google-side race.
            current = _data(
                await read(
                    GET,
                    {
                        "calendar_id": calendar_id,
                        "event_id": native["event_id"],
                    },
                )
            )
            _ordinary(current, native["event_id"], calendar_zone)
            etag = _text(current.get("etag"), "Current event etag")
            if etag != args["expected_etag"]:
                raise CalendarSafetyError(
                    "The event changed after approval (etag mismatch). No write executed."
                )
            before = _metadata(current)
    except CalendarSafetyError as exc:
        return {"successful": False, "executed": False, "error": str(exc)}
    except Exception:
        return {
            "successful": False,
            "executed": False,
            "error": "Preflight could not establish complete calendar availability and event "
            "state. No write executed.",
        }

    try:
        written = _data(await write(name, native))
        event = written.get("response_data") if name == CREATE else written
        if not isinstance(event, dict):
            raise CalendarSafetyError("Missing written event resource.")
        event_id = _text(event.get("id"), "Written event ID")
    except Exception:
        return {
            "successful": False,
            "executed": True,
            "outcome": "unknown",
            "error": "The write may have happened, but its outcome could not be confirmed. "
            "Inspect this calendar before any new approval; do not retry automatically.",
            "calendar_id": calendar_id,
        }

    receipt = {
        "action": "created" if name == CREATE else "updated",
        "calendar_id": calendar_id,
        "event_id": event_id,
    }
    result = {
        "successful": True,
        "executed": True,
        receipt["action"]: receipt,
        "availability": availability,
        "limitations": _NOTICE,
    }
    try:
        # No await between successful ID extraction and durable confirmation.
        confirmed = confirm(receipt)
        if inspect.isawaitable(confirmed):
            if inspect.iscoroutine(confirmed):
                confirmed.close()
            raise CalendarSafetyError(
                "The confirmation callback returned an awaitable instead of persisting "
                "synchronously."
            )
    except Exception:
        result["verification"] = {
            "matched": False,
            "error": "The write was confirmed but its local receipt could not be persisted. "
            "Inspect the account; do not repeat the write.",
        }
        return result

    try:
        if name == PATCH and event_id != native["event_id"]:
            raise CalendarSafetyError(
                "The write returned an unexpected event ID. Inspect the account."
            )
        saved = _data(await read(GET, {"calendar_id": calendar_id, "event_id": event_id}))
        result["verification"] = _verification(
            saved,
            event_id,
            native,
            name,
            begin,
            finish,
            calendar_zone,
            before,
        )
    except CalendarSafetyError as exc:
        result["verification"] = {"matched": False, "error": str(exc)}
    except Exception:
        result["verification"] = {
            "matched": False,
            "error": "The write was confirmed but read-back was unavailable. "
            "Inspect the saved event; do not repeat the write.",
        }
    return result
