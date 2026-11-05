"""A fictional inbox, calendar, and contacts stored in SQLite.

`reset` restores the seed, and `snapshot` returns the state used by eval checkers.
The simulated clock uses the seed's `now`; generated IDs are not fixed.
"""

import json
import sqlite3
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import uuid4

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
            empty = db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
        if empty:
            self.reset()

    def profile(self) -> str:
        info = self.info
        return (
            f"The person is {info['person']} <{info['email']}>, timezone {info['timezone']}.\n"
            f"Working hours: {info['working_hours']}\n{info['protected_blocks']}"
        )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def reset(self) -> None:
        with self._connect() as db:
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
        cursor = db.execute("INSERT OR IGNORE INTO applied_actions VALUES (?)", (key,))
        return cursor.rowcount == 1

    def search_email(
        self, query: str = "", sender: str = "", unread_only: bool = False, limit: int = 10
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT m.id, m.thread_id, t.subject, m.sender, m.sent_at, m.unread, m.body"
            " FROM messages m JOIN threads t ON t.id = m.thread_id"
            " WHERE m.folder != 'draft'"
            " AND (t.subject LIKE ? OR m.body LIKE ? OR m.sender LIKE ?) AND m.sender LIKE ?"
        )
        pattern = f"%{query}%"
        params: list[Any] = [pattern, pattern, pattern, f"%{sender}%"]
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

    def draft_reply(self, thread_id: str, body: str) -> str:
        with self._connect() as db:
            last = db.execute(
                "SELECT sender FROM messages WHERE thread_id = ? AND folder = 'inbox'"
                " ORDER BY sent_at DESC LIMIT 1",
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
        return draft_id

    def send_email(self, key: str, to: list[str], subject: str, body: str) -> bool:
        """Return False when this key already sent, so nothing new goes out."""
        with self._connect() as db:
            if not self._apply_once(db, key):
                return False
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

    def list_events(self, start: str, end: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE start < ? AND end > ? ORDER BY start", (end, start)
            ).fetchall()
        return [{**dict(row), "attendees": json.loads(row["attendees"])} for row in rows]

    def create_event(
        self, key: str, title: str, start: str, end: str, attendees: list[str]
    ) -> str | None:
        with self._connect() as db:
            if not self._apply_once(db, key):
                return None
            event_id = f"e-{uuid4().hex[:8]}"
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (event_id, title, start, end, json.dumps(attendees)),
            )
        return event_id

    def move_event(self, key: str, event_id: str, start: str, end: str) -> bool:
        with self._connect() as db:
            if db.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone() is None:
                raise WorldError(f"No event {event_id}. Use list_events to find event ids.")
            if not self._apply_once(db, key):
                return False
            db.execute("UPDATE events SET start = ?, end = ? WHERE id = ?", (start, end, event_id))
        return True

    def search_contacts(self, query: str) -> list[dict[str, Any]]:
        pattern = f"%{query}%"
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM contacts WHERE name LIKE ? OR email LIKE ? ORDER BY name",
                (pattern, pattern),
            ).fetchall()
        return [dict(row) for row in rows]
