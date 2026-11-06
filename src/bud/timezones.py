"""One UTC/zone validation rule for new inputs and existing stored timestamps."""

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CalendarTimeError(ValueError):
    """A calendar instant cannot be resolved safely."""


def calendar_time(value: str, timezone: str, *, stored: bool = False) -> datetime:
    """Return UTC; new input needs an offset consistent with the named zone.

    Stored aware timestamps already identify instants, independently of display
    zones. Legacy naive stored values require one unambiguous UTC round-trip.
    """
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise CalendarTimeError(f"Unknown IANA timezone: {timezone!r}.") from exc
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
        r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?",
        value,
    ):
        raise CalendarTimeError(
            "Use an ISO date and time with an explicit offset, such as 2026-11-05T14:00:00-06:00."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CalendarTimeError(f"Invalid calendar time: {value!r}.") from exc
    if parsed.tzinfo is not None:
        try:
            utc = parsed.astimezone(UTC)
            local = utc.astimezone(zone)
        except (ValueError, OverflowError) as exc:
            raise CalendarTimeError("Calendar time is outside the supported date range.") from exc
        if not stored and (
            parsed.replace(tzinfo=None) != local.replace(tzinfo=None)
            or parsed.utcoffset() != local.utcoffset()
        ):
            raise CalendarTimeError(
                f"{value} does not match {timezone}. Resolve the local time "
                "and UTC offset before trying again."
            )
        return utc
    if not stored:
        raise CalendarTimeError(
            "Calendar times require an explicit UTC offset. Resolve the date, "
            "timezone, and any daylight-saving ambiguity with the user first."
        )
    candidates = set()
    try:
        for fold in (0, 1):
            utc = parsed.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            if utc.astimezone(zone).replace(tzinfo=None) == parsed:
                candidates.add(utc)
    except (ValueError, OverflowError) as exc:
        raise CalendarTimeError(
            "Stored calendar time is outside the supported date range."
        ) from exc
    if len(candidates) != 1:
        raise CalendarTimeError(
            f"Stored local time {value} is ambiguous or nonexistent in {timezone}; "
            "resolve the stored event before scheduling."
        )
    return candidates.pop()
