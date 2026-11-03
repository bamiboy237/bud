"""SQLite-backed event log for session history.
Each session's events are numbered by `seq`, and `unfinished_runs` finds runs that never
reached a terminal event.
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from bud.core.events import RUN_END_EVENTS, Event, EventType

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_decision_per_approval
    ON events (json_extract(data, '$.approval_id'))
    WHERE type IN ('approval.granted', 'approval.denied');
CREATE UNIQUE INDEX IF NOT EXISTS one_event_per_client_message
    ON events (session_id, json_extract(data, '$.message_id'))
    WHERE type = 'user.message';
"""


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def append(self, session_id: str, type: EventType, data: dict[str, Any] | None = None) -> Event:
        event = self._insert(session_id, type, data, "INSERT")
        assert event is not None
        return event

    def append_once(
        self, session_id: str, type: EventType, data: dict[str, Any] | None = None
    ) -> Event | None:
        """Return None when a unique index already holds this fact, so the first writer wins."""
        return self._insert(session_id, type, data, "INSERT OR IGNORE")

    def _insert(
        self, session_id: str, type: EventType, data: dict[str, Any] | None, verb: str
    ) -> Event | None:
        """One INSERT computes seq, so two writers can't claim the same number."""
        event_id = uuid4().hex
        created_at = datetime.now(UTC).isoformat()
        payload = json.dumps(data or {})
        with self._connect() as db:
            row = db.execute(
                f"""
                {verb} INTO events (id, session_id, seq, type, data, created_at)
                SELECT ?, ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?
                FROM events WHERE session_id = ?
                RETURNING seq
                """,
                (event_id, session_id, type, payload, created_at, session_id),
            ).fetchone()
        if row is None:
            return None
        return Event(
            id=event_id,
            session_id=session_id,
            seq=row[0],
            type=type,
            data=data or {},
            created_at=created_at,
        )

    def read(self, session_id: str, after: int = 0) -> list[Event]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, session_id, seq, type, data, created_at FROM events"
                " WHERE session_id = ? AND seq > ? ORDER BY seq",
                (session_id, after),
            ).fetchall()
        return [
            Event(
                id=row[0],
                session_id=row[1],
                seq=row[2],
                type=row[3],
                data=json.loads(row[4]),
                created_at=row[5],
            )
            for row in rows
        ]

    def exists(self, session_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM events WHERE session_id = ? LIMIT 1", (session_id,)
            ).fetchone()
        return row is not None

    def sessions_with_decisions(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT session_id FROM events"
                " WHERE type IN ('approval.granted', 'approval.denied')"
            ).fetchall()
        return [row[0] for row in rows]

    def unfinished_runs(self) -> list[tuple[str, str]]:
        placeholders = ", ".join("?" for _ in RUN_END_EVENTS)
        with self._connect() as db:
            rows = db.execute(
                f"""
                SELECT started.session_id, json_extract(started.data, '$.run_id')
                FROM events AS started
                WHERE started.type = 'run.started'
                  AND NOT EXISTS (
                    SELECT 1 FROM events AS ended
                    WHERE ended.session_id = started.session_id
                      AND ended.type IN ({placeholders})
                      AND json_extract(ended.data, '$.run_id')
                          = json_extract(started.data, '$.run_id')
                  )
                """,
                RUN_END_EVENTS,
            ).fetchall()
        return [(row[0], row[1]) for row in rows]
