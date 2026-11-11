"""A fictional inbox, calendar, and contacts stored in SQLite.

`reset` restores the seed, and `snapshot` returns the state used by eval checkers.
The simulated clock uses the seed's `now`; generated IDs are not fixed.
"""

import json
import sqlite3
import tomllib
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, time
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from bud.timezones import CalendarTimeError
from bud.timezones import calendar_time as resolve_calendar_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, subject TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads (id),
    folder TEXT NOT NULL CHECK (folder IN ('inbox', 'sent', 'draft')),
    sender TEXT NOT NULL,
    recipients TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    body TEXT NOT NULL,
    unread INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    attendees TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    email TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    relationship TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applied_actions (key TEXT PRIMARY KEY);
"""

TABLES = ("threads", "messages", "events", "contacts")
MAX_DRAFT_BODY_CHARS = 20_000


def calendar_time(value: str, timezone: str, *, legacy: bool = False) -> datetime:
    """Use the shared instant rule, retaining the World's domain error."""
    try:
        return resolve_calendar_time(value, timezone, stored=legacy)
    except CalendarTimeError as exc:
        raise WorldError(str(exc)) from exc


def load_seed() -> dict[str, Any]:
    return tomllib.loads(resources.files("bud.world").joinpath("seed.toml").read_text())


class WorldError(Exception):
    """An invalid operation on the simulated world."""


class World:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.seed = load_seed()
        self.info = self.seed["world"]
        self.now: str = self.info["now"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(applied_actions)")}
            if "draft_id" not in columns:
                db.execute("ALTER TABLE applied_actions ADD COLUMN draft_id TEXT")
            for column in ("calendar_payload", "event_id", "send_payload"):
                if column not in columns:
                    db.execute(f"ALTER TABLE applied_actions ADD COLUMN {column} TEXT")
            empty = all(
                db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
                for table in (*TABLES, "applied_actions")
            )
            if empty:
                self._reset(db)

    def profile(self) -> str:
        info = self.info
        return (
            f"The person is {info['person']} <{info['email']}>, timezone {info['timezone']}.\n"
            f"Synthetic scheduling hours only: {info['working_hours']} in {info['timezone']}.\n"
            f"{info['protected_blocks']}\n"
            "Resolve unclear dates, people, timezone, or DST folds with the user; do not guess. "
            "Calendar input times need explicit offsets matching the supplied IANA timezone "
            "(default: the person's timezone; use timezone='UTC' for UTC inputs). "
            "Check list_events before proposing a change. Calendar writes require approval; "
            "read_event after each approved write before claiming it is verified. "
            "Save reply drafts only after approval, then read_draft to verify the saved result. "
            "Use list_drafts and read_draft to find drafts in new conversations. "
            "A denied draft must not be saved or sent."
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        with closing(db), db:
            yield db

    def reset(self) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reset(db)

    def _reset(self, db: sqlite3.Connection) -> None:
        for table in (*TABLES, "applied_actions"):
            db.execute(f"DELETE FROM {table}")
        for thread in self.seed["threads"]:
            db.execute("INSERT INTO threads VALUES (?, ?)", (thread["id"], thread["subject"]))
            for index, message in enumerate(thread["messages"], start=1):
                is_own = message["sender"] == self.info["email"]
                db.execute(
                    "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{thread['id']}-m{index}",
                        thread["id"],
                        "sent" if is_own else "inbox",
                        message["sender"],
                        json.dumps(message["to"]),
                        message["sent_at"],
                        message["body"],
                        int(message.get("unread", False)),
                    ),
                )
        for event in self.seed["events"]:
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (
                    event["id"],
                    event["title"],
                    event["start"],
                    event["end"],
                    json.dumps(event["attendees"]),
                ),
            )
        for contact in self.seed["contacts"]:
            db.execute(
                "INSERT INTO contacts VALUES (?, ?, ?)",
                (contact["email"], contact["name"], contact["relationship"]),
            )

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as db:
            return {
                table: [
                    dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                ]
                for table in TABLES
            }

    def _apply_once(self, db: sqlite3.Connection, key: str) -> bool:
        """The key commits with the write itself, so a replay can't apply it twice."""
        cursor = db.execute("INSERT OR IGNORE INTO applied_actions (key) VALUES (?)", (key,))
        return cursor.rowcount == 1

    def search_email(
        self, query: str = "", sender: str = "", unread_only: bool = False, limit: int = 10
    ) -> list[dict[str, Any]]:
        # Like Gmail, every word must appear somewhere (subject, body, or sender), not
        # the query as one exact phrase.
        sql = (
            "SELECT m.id, m.thread_id, t.subject, m.sender, m.sent_at, m.unread, m.body"
            " FROM messages m JOIN threads t ON t.id = m.thread_id"
            " WHERE m.folder != 'draft' AND m.sender LIKE ?"
        )
        params: list[Any] = [f"%{sender}%"]
        for word in query.split():
            sql += " AND (t.subject LIKE ? OR m.body LIKE ? OR m.sender LIKE ?)"
            params += [f"%{word}%"] * 3
        if unread_only:
            sql += " AND m.unread = 1"
        sql += " ORDER BY m.sent_at DESC LIMIT ?"
        params.append(min(limit, 25))
        with self._connect() as db:
            rows = [dict(row) for row in db.execute(sql, params).fetchall()]
        for row in rows:
            row["snippet"] = row.pop("body")[:160]
        return rows

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        with self._connect() as db:
            thread = db.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if thread is None:
                raise WorldError(f"No thread {thread_id}. Use search_email to find thread ids.")
            messages = db.execute(
                "SELECT id, folder, sender, recipients, sent_at, body FROM messages"
                " WHERE thread_id = ? ORDER BY sent_at",
                (thread_id,),
            ).fetchall()

        return {"subject": thread["subject"], "messages": [dict(row) for row in messages]}

    def list_drafts(self, query: str = "", limit: int = 10, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= 25 or offset < 0:
            raise WorldError("Use a draft limit from 1 to 25 and a nonnegative offset.")
        if len(query) > 500:
            raise WorldError("Draft queries must be at most 500 characters.")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with self._connect() as db:
            rows = db.execute(
                "SELECT m.id AS draft_id, m.thread_id, t.subject, m.sender, m.recipients,"
                " m.sent_at, substr(m.body, 1, 160) AS snippet"
                " FROM messages m JOIN threads t ON t.id = m.thread_id"
                " WHERE m.folder = 'draft' AND"
                " (t.subject LIKE ? ESCAPE '\\' OR m.body LIKE ? ESCAPE '\\'"
                " OR m.recipients LIKE ? ESCAPE '\\')"
                " ORDER BY m.sent_at DESC, m.id DESC LIMIT ? OFFSET ?",
                (pattern, pattern, pattern, limit + 1, offset),
            ).fetchall()
        return {
            "drafts": [
                {**dict(row), "recipients": json.loads(row["recipients"])} for row in rows[:limit]
            ],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def _draft(self, db: sqlite3.Connection, draft_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT m.id AS draft_id, m.thread_id, t.subject, m.sender, m.recipients,"
            " m.sent_at, m.body FROM messages m JOIN threads t ON t.id = m.thread_id"
            " WHERE m.id = ? AND m.folder = 'draft'",
            (draft_id,),
        ).fetchone()
        if row is None:
            raise WorldError(f"No draft {draft_id}. Use list_drafts to find draft ids.")
        return {**dict(row), "recipients": json.loads(row["recipients"])}

    def read_draft(
        self, draft_id: str, body_offset: int = 0, max_chars: int = 8_000
    ) -> dict[str, Any]:
        if body_offset < 0 or not 1 <= max_chars <= MAX_DRAFT_BODY_CHARS:
            raise WorldError("Use a nonnegative body offset and 1 to 20,000 characters.")
        with self._connect() as db:
            draft = self._draft(db, draft_id)
        body = draft["body"]
        if body_offset > len(body):
            raise WorldError("The body offset is past the end of the draft.")
        end = min(body_offset + max_chars, len(body))
        return {
            **draft,
            "body": body[body_offset:end],
            "body_offset": body_offset,
            "body_total_chars": len(body),
            "next_body_offset": end if end < len(body) else None,
        }

    def draft_reply(self, key: str, thread_id: str, body: str) -> dict[str, Any]:
        if not key.strip():
            raise WorldError("Saving a draft requires an action key.")
        if not body.strip() or len(body) > MAX_DRAFT_BODY_CHARS:
            raise WorldError("Draft bodies must contain text and be at most 20,000 characters.")
        with self._connect() as db:
            if not self._apply_once(db, key):
                saved = db.execute(
                    "SELECT draft_id FROM applied_actions WHERE key = ?", (key,)
                ).fetchone()
                if saved["draft_id"] is None:
                    raise WorldError("This action key was already used for another write.")
                draft = self._draft(db, saved["draft_id"])
                if draft["thread_id"] != thread_id or draft["body"] != body:
                    raise WorldError("This action key already saved a different draft.")
                return draft
            last = db.execute(
                "SELECT sender FROM messages WHERE thread_id = ? AND folder = 'inbox'"
                " ORDER BY sent_at DESC, id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            if last is None:
                raise WorldError(f"No thread {thread_id} to reply to.")
            draft_id = f"draft-{uuid4().hex[:8]}"
            db.execute(
                "INSERT INTO messages VALUES (?, ?, 'draft', ?, ?, ?, ?, 0)",
                (
                    draft_id,
                    thread_id,
                    self.info["email"],
                    json.dumps([last["sender"]]),
                    self.now,
                    body,
                ),
            )
            db.execute("UPDATE applied_actions SET draft_id = ? WHERE key = ?", (draft_id, key))
            return self._draft(db, draft_id)

    def send_email(self, key: str, to: list[str], subject: str, body: str) -> bool:
        """Replay only the same known send; other or legacy unknown keys are refused."""
        if not key.strip():
            raise WorldError("Sending requires an action key.")
        payload = json.dumps({"to": to, "subject": subject, "body": body}, sort_keys=True)
        with self._connect() as db:
            if not self._apply_once(db, key):
                saved = db.execute(
                    "SELECT send_payload FROM applied_actions WHERE key = ?", (key,)
                ).fetchone()
                if saved["send_payload"] != payload:
                    raise WorldError(
                        "This action key was already used for a different or unverified write."
                    )
                return False
            db.execute("UPDATE applied_actions SET send_payload = ? WHERE key = ?", (payload, key))
            thread_id = f"t-{uuid4().hex[:8]}"
            db.execute("INSERT INTO threads VALUES (?, ?)", (thread_id, subject))
            db.execute(
                "INSERT INTO messages VALUES (?, ?, 'sent', ?, ?, ?, ?, 0)",
                (
                    f"{thread_id}-m1",
                    thread_id,
                    self.info["email"],
                    json.dumps(to),
                    self.now,
                    body,
                ),
            )
        return True

    def _calendar_range(
        self, start: str, end: str, timezone: str | None = None, *, legacy: bool = False
    ) -> tuple[datetime, datetime]:
        zone = self.info["timezone"] if timezone is None else timezone
        begin = calendar_time(start, zone, legacy=legacy)
        finish = calendar_time(end, zone, legacy=legacy)
        if begin >= finish:
            raise WorldError("Calendar end must be after start.")
        return begin, finish

    def _event(self, row: sqlite3.Row) -> dict[str, Any]:
        begin, finish = self._calendar_range(row["start"], row["end"], legacy=True)
        zone = ZoneInfo(self.info["timezone"])
        return {
            **dict(row),
            "start": begin.astimezone(zone).isoformat(),
            "end": finish.astimezone(zone).isoformat(),
            "timezone": self.info["timezone"],
            "start_utc": begin.isoformat(),
            "end_utc": finish.isoformat(),
            "attendees": json.loads(row["attendees"]),
            "protected": row["title"].strip().casefold() == "focus block",
        }

    def _read_event(self, db: sqlite3.Connection, event_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise WorldError(f"No event {event_id}. Use list_events to find event ids.")
        return self._event(row)

    def read_event(self, event_id: str) -> dict[str, Any]:
        with self._connect() as db:
            return self._read_event(db, event_id)

    def _overlapping_events(
        self, db: sqlite3.Connection, begin: datetime, finish: datetime, exclude: str = ""
    ) -> list[dict[str, Any]]:
        matches = []
        for row in db.execute("SELECT * FROM events WHERE id != ?", (exclude,)):
            start, end = self._calendar_range(row["start"], row["end"], legacy=True)
            if start < finish and end > begin:
                matches.append((start, row["id"], self._event(row)))
        return [event for _, _, event in sorted(matches)]

    def list_events(
        self, start: str, end: str, timezone: str | None = None
    ) -> list[dict[str, Any]]:
        begin, finish = self._calendar_range(start, end, timezone)
        with self._connect() as db:
            return self._overlapping_events(db, begin, finish)

    def _check_booking(
        self, db: sqlite3.Connection, begin: datetime, finish: datetime, exclude: str = ""
    ) -> None:
        # This is the fictional seed's work calendar, not a real user's personal policy.
        zone = ZoneInfo(self.info["timezone"])
        start, end = begin.astimezone(zone), finish.astimezone(zone)
        if (
            start.weekday() >= 5
            or start.date() != end.date()
            or start.time() < time(9)
            or end.time() > time(17)
        ):
            raise WorldError(
                "Synthetic work calendar bookings must fit Monday–Friday "
                f"09:00–17:00 in {self.info['timezone']}."
            )
        conflicts = self._overlapping_events(db, begin, finish, exclude)
        if conflicts:
            details = ", ".join(f"{event['id']} ({event['title']})" for event in conflicts)
            raise WorldError(
                f"Calendar conflict with {details}. Check list_events and choose "
                "a free time; Focus Block events cannot be moved or booked over."
            )

    def _calendar_replay(
        self, db: sqlite3.Connection, key: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not key.strip():
            raise WorldError("Calendar writes require an action key.")
        encoded = json.dumps(payload, sort_keys=True)
        if self._apply_once(db, key):
            db.execute(
                "UPDATE applied_actions SET calendar_payload = ? WHERE key = ?", (encoded, key)
            )
            return None
        saved = db.execute(
            "SELECT calendar_payload, event_id FROM applied_actions WHERE key = ?", (key,)
        ).fetchone()
        if saved["calendar_payload"] != encoded or saved["event_id"] is None:
            raise WorldError("This action key was already used for a different write.")
        # Read current state, not a cached acknowledgement: a later approval may have moved it.
        return {"applied": False, "event": self._read_event(db, saved["event_id"])}

    def create_event(
        self,
        key: str,
        title: str,
        start: str,
        end: str,
        attendees: list[str],
        timezone: str | None = None,
    ) -> dict[str, Any]:
        begin, finish = self._calendar_range(start, end, timezone)
        if not title.strip():
            raise WorldError("An event title is required.")
        payload = {
            "action": "create_event",
            "title": title,
            "start": begin.isoformat(),
            "end": finish.isoformat(),
            "attendees": attendees,
        }
        with self._connect() as db:
            # Reserve the writer before checking conflicts; another connection cannot
            # insert or move an event into the checked range before this write commits.
            db.execute("BEGIN IMMEDIATE")
            replay = self._calendar_replay(db, key, payload)
            if replay is not None:
                return replay
            self._check_booking(db, begin, finish)
            event_id = f"e-{uuid4().hex[:8]}"
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (event_id, title, begin.isoformat(), finish.isoformat(), json.dumps(attendees)),
            )
            db.execute("UPDATE applied_actions SET event_id = ? WHERE key = ?", (event_id, key))
            return {"applied": True, "event": self._read_event(db, event_id)}

    def move_event(
        self, key: str, event_id: str, start: str, end: str, timezone: str | None = None
    ) -> dict[str, Any]:
        begin, finish = self._calendar_range(start, end, timezone)
        payload = {
            "action": "move_event",
            "event_id": event_id,
            "start": begin.isoformat(),
            "end": finish.isoformat(),
        }
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._calendar_replay(db, key, payload)
            if replay is not None:
                return replay
            event = self._read_event(db, event_id)
            if event["protected"]:
                raise WorldError("Focus Block events are protected and cannot be moved.")
            self._check_booking(db, begin, finish, exclude=event_id)
            db.execute(
                "UPDATE events SET start = ?, end = ? WHERE id = ?",
                (begin.isoformat(), finish.isoformat(), event_id),
            )
            db.execute("UPDATE applied_actions SET event_id = ? WHERE key = ?", (event_id, key))
            return {"applied": True, "event": self._read_event(db, event_id)}

    def search_contacts(self, query: str) -> list[dict[str, Any]]:
        pattern = f"%{query}%"
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM contacts WHERE name LIKE ? OR email LIKE ? ORDER BY name",
                (pattern, pattern),
            ).fetchall()
        return [dict(row) for row in rows]
