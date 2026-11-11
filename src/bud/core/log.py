"""SQLite session events, accepted runs, and idempotent workflow projections."""

import json
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from bud.core.events import RUN_END_EVENTS, Event, EventType
from bud.core.projection import (
    message_batch,
    queue_paused,
    queued_messages,
    runs,
    undecided_approvals,
)
from bud.limits import MAX_QUEUED_MESSAGES


class QueueFull(Exception):
    pass


class StopRequested(Exception):
    pass


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
-- The latest conversation snapshots, kept out of the append-only log so that each
-- turn does not store every earlier turn again.
CREATE TABLE IF NOT EXISTS conversations (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_by_session ON conversations (session_id);
CREATE INDEX IF NOT EXISTS events_by_type_session ON events (type, session_id);
"""
# Recovery replays one run, which needs its predecessor's snapshot; keep a spare.
KEEP_CONVERSATIONS = 2
CACHED_SESSIONS = 32


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        # Committed events per session. The log is append-only and seq is unique, so a
        # cached prefix plus a fetch of later rows equals a full read.
        self._cache: OrderedDict[str, list[Event]] = OrderedDict()
        self._cache_lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
            if "event_key" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN event_key TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS event_key_once ON events(event_key)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    def append(self, session_id: str, type: EventType, data: dict[str, Any] | None = None) -> Event:
        with self._connect() as db:
            event = self._insert(db, session_id, type, data or {})
        assert event is not None
        return event

    def append_once(
        self,
        session_id: str,
        type: EventType,
        data: dict[str, Any] | None = None,
        *,
        key: str | None = None,
    ) -> Event | None:
        with self._connect() as db:
            return self._insert(db, session_id, type, data or {}, once=True, key=key)

    @staticmethod
    def _insert(
        db: sqlite3.Connection,
        session_id: str,
        type: EventType,
        data: dict,
        *,
        once: bool = False,
        key: str | None = None,
    ) -> Event | None:
        if (
            type
            in {
                "run.started",
                "run.recovered",
                "run.waiting",
                "run.completed",
                "run.failed",
                "run.cancelled",
                "run.stop_requested",
                "approval.requested",
            }
            and "origin" not in data
        ):
            row = db.execute(
                """SELECT json_extract(data, '$.origin'),
                          COALESCE(json_extract(data, '$.message_ids[#-1]'),
                                   json_extract(data, '$.message_id')),
                          json_extract(data, '$.quiet') FROM events
                   WHERE session_id=? AND type='run.queued'
                   AND json_extract(data, '$.run_id')=?""",
                (session_id, data.get("run_id")),
            ).fetchone()
            data = {
                **data,
                "origin": json.loads(row[0]) if row and row[0] else None,
                "source_message_id": row[1] if row else None,
                "quiet": bool(row[2]) if row else False,
            }
        event_id = uuid4().hex
        created_at = datetime.now(UTC)
        verb = "INSERT OR IGNORE" if once else "INSERT"
        row = db.execute(
            f"""
            {verb} INTO events (id, session_id, seq, type, data, created_at, event_key)
            SELECT ?, ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?
            FROM events WHERE session_id = ?
            RETURNING seq
            """,
            (event_id, session_id, type, json.dumps(data), created_at.isoformat(), key, session_id),
        ).fetchone()
        if row is None:
            return None
        return Event(
            id=event_id,
            session_id=session_id,
            seq=row[0],
            type=type,
            data=data,
            created_at=created_at,
        )

    def read(self, session_id: str, after: int = 0) -> list[Event]:
        with self._connect() as db:
            return self._read(db, session_id, after, remember=True)

    def _read(
        self, db: sqlite3.Connection, session_id: str, after: int = 0, *, remember: bool = False
    ) -> list[Event]:
        """Read events after `after`. Only reads outside a write transaction may update the
        cache, so rows from a transaction that later rolls back are never remembered."""
        with self._cache_lock:
            cached = self._cache.get(session_id)
            if cached is not None:
                self._cache.move_to_end(session_id)
        last = cached[-1].seq if cached else 0
        if after > last or (not cached and after > 0):
            return self._fetch(db, session_id, after)
        fresh = self._fetch(db, session_id, last)
        if remember and fresh:
            with self._cache_lock:
                if self._cache.get(session_id) is cached:
                    self._cache[session_id] = (cached or []) + fresh
                    self._cache.move_to_end(session_id)
                    while len(self._cache) > CACHED_SESSIONS:
                        self._cache.popitem(last=False)
        return [event for event in cached or [] if event.seq > after] + fresh

    def last_seq(self, session_id: str) -> int:
        """Capture a stored-event boundary without reading the event payloads."""
        with self._connect() as db:
            return db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?", (session_id,)
            ).fetchone()[0]

    @staticmethod
    def _fetch(db: sqlite3.Connection, session_id: str, after: int) -> list[Event]:
        rows = db.execute(
            "SELECT id, session_id, seq, type, data, created_at FROM events "
            "WHERE session_id = ? AND seq > ? ORDER BY seq",
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

    def conversation(self, session_id: str, run_id: str) -> str | None:
        """The conversation snapshot saved when `run_id` ended, if it is still kept."""
        with self._connect() as db:
            row = db.execute(
                "SELECT data FROM conversations WHERE run_id = ? AND session_id = ?",
                (run_id, session_id),
            ).fetchone()
            if row is None:
                # Logs written before the conversations table kept snapshots inline.
                row = db.execute(
                    "SELECT json_extract(data, '$.conversation') FROM events "
                    "WHERE session_id = ? AND event_key = ?",
                    (session_id, f"{run_id}:terminal"),
                ).fetchone()
        return row[0] if row and row[0] else None

    def exists(self, session_id: str) -> bool:
        with self._connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM events WHERE session_id = ? LIMIT 1", (session_id,)
                ).fetchone()
                is not None
            )

    def primary_session(self, preferred: str | None = None) -> str:
        """Choose once, transactionally; never guess which old thread to merge."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT session_id FROM events WHERE event_key='owner:primary-session'"
            ).fetchone()
            if row:
                if not db.execute(
                    "SELECT 1 FROM events WHERE session_id=? AND type='session.created'",
                    (row[0],),
                ).fetchone():
                    raise ValueError("Primary conversation is unavailable; it was not replaced.")
                if preferred is not None and preferred != row[0]:
                    raise ValueError(
                        "The primary conversation is already set. Explicit threads were not merged."
                    )
                return row[0]
            session_id = preferred or uuid4().hex
            exists = db.execute(
                "SELECT 1 FROM events WHERE session_id=? AND type='session.created'",
                (session_id,),
            ).fetchone()
            if preferred is not None and not exists:
                raise ValueError("The requested existing conversation is unavailable.")
            if not exists:
                self._insert(db, session_id, "session.created", {})
            self._insert(db, session_id, "session.primary", {}, key="owner:primary-session")
            return session_id

    def accept_message(
        self, session_id: str, text: str, message_id: str, origin: dict | None = None
    ) -> Event | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            event = self._accept_message(db, session_id, text, message_id, origin)
            if event is not None:
                self._supersede(db, session_id, message_id, origin)
            return event

    def _supersede(self, db, session_id: str, message_id: str, origin: dict | None) -> None:
        # New conversational input makes unapproved proposals stale. Already
        # granted permissions are not recalled or changed by this operation.
        for request in undecided_approvals(self._read(db, session_id)):
            self._insert(
                db,
                session_id,
                "approval.denied",
                {
                    "approval_id": request["approval_id"],
                    "origin": origin,
                    "revision_message_id": message_id,
                },
            )

    def _supersede_updates(self, db, session_id: str, run_id: str) -> bool:
        events = self._read(db, session_id)
        started = next(
            event.seq
            for event in reversed(events)
            if event.type == "run.queued" and event.data["run_id"] == run_id
        )
        pending = {item.message_id for item in queued_messages(events)}
        update = next(
            (
                event
                for event in reversed(events)
                if event.seq > started
                and event.type == "message.queued"
                and event.data["message_id"] in pending
            ),
            None,
        )
        if update is None or not undecided_approvals(events):
            return False
        self._supersede(db, session_id, update.data["message_id"], update.data.get("origin"))
        return True

    def supersede_updates(self, session_id: str) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = undecided_approvals(self._read(db, session_id))
            return bool(pending) and self._supersede_updates(db, session_id, pending[0]["run_id"])

    def _accept_message(
        self, db, session_id: str, text: str, message_id: str, origin: dict | None
    ) -> Event | None:
        previous = db.execute(
            """SELECT data FROM events WHERE session_id = ?
                   AND type IN ('message.queued', 'user.message')
                   AND json_extract(data, '$.message_id') = ? ORDER BY seq LIMIT 1""",
            (session_id, message_id),
        ).fetchone()
        if previous:
            data = json.loads(previous[0])
            if data["text"] != text or data.get("origin") != origin:
                raise ValueError("Message ID already used with different content or origin")
            return None
        if len(queued_messages(self._read(db, session_id))) >= MAX_QUEUED_MESSAGES:
            raise QueueFull(f"This conversation already has {MAX_QUEUED_MESSAGES} waiting messages")
        return self._insert(
            db,
            session_id,
            "message.queued",
            {"message_id": message_id, "text": text, "origin": origin},
        )

    def revise_approval(
        self, session_id: str, approval_id: str, text: str, message_id: str, origin: dict
    ) -> Event | None:
        """Retire one pending permission and accept its correction atomically."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            event = self._accept_message(db, session_id, text, message_id, origin)
            if event is None:
                # A repeated transport request cannot decline a later proposal.
                return None
            pending = undecided_approvals(self._read(db, session_id))
            if len(pending) != 1 or pending[0]["approval_id"] != approval_id:
                raise ValueError("The exact action must be the only pending approval to revise.")
            self._insert(
                db,
                session_id,
                "approval.denied",
                {"approval_id": approval_id, "origin": origin, "revision_message_id": message_id},
            )
            return event

    def edit_message(self, session_id: str, message_id: str, text: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not any(
                m.message_id == message_id for m in queued_messages(self._read(db, session_id))
            ):
                raise ValueError("Message is no longer queued")
            self._insert(db, session_id, "message.edited", {"message_id": message_id, "text": text})

    def cancel_message(self, session_id: str, message_id: str) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not any(
                m.message_id == message_id for m in queued_messages(self._read(db, session_id))
            ):
                return False
            self._insert(db, session_id, "message.cancelled", {"message_id": message_id})
            return True

    def set_queue_paused(self, session_id: str, paused: bool) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if queue_paused(self._read(db, session_id)) == paused:
                return False
            self._insert(db, session_id, "queue.paused" if paused else "queue.resumed", {})
            return True

    def dispatch_message(self, session_id: str, message_id: str, run: dict) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            events = self._read(db, session_id)
            pending = queued_messages(events)
            states = runs(events)
            if (
                queue_paused(events)
                or not pending
                or pending[0].message_id != message_id
                or (
                    states
                    and states[-1].status in ("queued", "running", "stopping", "awaiting_approval")
                )
            ):
                return False
            batch = message_batch(events)
            if run.get("message_ids", [message_id]) != [item.message_id for item in batch]:
                return False
            if run["text"] != "\n\n".join(item.text for item in batch):
                return False
            for item in batch:
                self._insert(db, session_id, "user.message", item.model_dump(), once=True)
                self._insert(
                    db,
                    session_id,
                    "message.dispatched",
                    {"message_id": item.message_id, "run_id": run["run_id"]},
                )
            self._insert(db, session_id, "run.queued", run, key=f"{run['run_id']}:queued")
            return True

    def queue_sessions(self) -> list[str]:
        with self._connect() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT session_id FROM events WHERE type = 'message.queued'"
                )
            ]

    def stop_requested(self, run_id: str) -> bool:
        with self._connect() as db:
            return self._stopped(db, run_id)

    @staticmethod
    def _stopped(db: sqlite3.Connection, run_id: str) -> bool:
        return (
            db.execute("SELECT 1 FROM events WHERE event_key = ?", (f"{run_id}:stop",)).fetchone()
            is not None
        )

    def request_stop(self, session_id: str) -> dict | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            events = self._read(db, session_id)
            if not queue_paused(events):
                self._insert(db, session_id, "queue.paused", {})
            states = runs(events)
            if not states or states[-1].status not in (
                "queued",
                "running",
                "awaiting_approval",
                "stopping",
            ):
                return None
            current = states[-1]
            data = {"run_id": current.run_id, "reason": "Stopped by the user."}
            self._insert(
                db, session_id, "run.stop_requested", data, once=True, key=f"{current.run_id}:stop"
            )
            for request in undecided_approvals(events):
                self._insert(
                    db,
                    session_id,
                    "approval.denied",
                    {"approval_id": request["approval_id"], "reason": "Stopped by the user."},
                    once=True,
                )
            if current.status == "awaiting_approval":
                self._ack_stop(db, session_id, current.run_id)
            return data

    def acknowledge_stop(self, session_id: str, run_id: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._stopped(db, run_id):
                self._ack_stop(db, session_id, run_id)

    def _ack_stop(self, db: sqlite3.Connection, session_id: str, run_id: str) -> None:
        events = self._read(db, session_id)
        returned = {
            e.data["call_id"]
            for e in events
            if e.type == "tool.result" and e.data["run_id"] == run_id
        }
        uncertain = [
            e.data["tool"]
            for e in events
            if e.type == "action.decided"
            and e.data["run_id"] == run_id
            and e.data["outcome"] == "approved"
            and e.data["call_id"] not in returned
        ]
        cancelled = self._insert(
            db,
            session_id,
            "run.cancelled",
            {
                "run_id": run_id,
                "reason": (
                    f"Stopped. Check the outcome of {', '.join(uncertain)} before retrying."
                    if uncertain
                    else "Stopped by the user."
                ),
                "uncertain_tools": uncertain,
            },
            once=True,
            key=f"{run_id}:stopped",
        )
        if cancelled is not None and uncertain and not queue_paused(self._read(db, session_id)):
            self._insert(db, session_id, "queue.paused", {})

    def record_action(self, session_id: str, run_id: str, decision: dict) -> None:
        # Permission and Stop serialize on the same database transaction.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._stopped(db, run_id):
                raise StopRequested("This run was stopped before another tool received permission")
            self._insert(
                db,
                session_id,
                "action.decided",
                {"run_id": run_id, **decision},
                once=True,
                key=f"{run_id}:decision:{decision['call_id']}",
            )

    def finish_run(
        self, session_id: str, run_id: str, kind: EventType, data: dict, approvals: list[dict]
    ) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._stopped(db, run_id):
                self._ack_stop(db, session_id, run_id)
                return
            for request in approvals:
                self._insert(
                    db,
                    session_id,
                    "approval.requested",
                    request,
                    once=True,
                    key=f"{run_id}:approval:{request['call_id']}",
                )
            conversation = data.get("conversation")
            if conversation is not None:
                data = {key: value for key, value in data.items() if key != "conversation"}
                data["conversation_saved"] = True
            terminal = self._insert(db, session_id, kind, data, once=True, key=f"{run_id}:terminal")
            if terminal is not None and conversation is not None:
                db.execute(
                    "INSERT OR REPLACE INTO conversations (run_id, session_id, data) "
                    "VALUES (?, ?, ?)",
                    (run_id, session_id, conversation),
                )
                db.execute(
                    """DELETE FROM conversations WHERE session_id = ? AND rowid NOT IN (
                        SELECT rowid FROM conversations WHERE session_id = ?
                        ORDER BY rowid DESC LIMIT ?)""",
                    (session_id, session_id, KEEP_CONVERSATIONS),
                )
            if terminal is not None and kind == "run.waiting":
                self._supersede_updates(db, session_id, run_id)
            if (
                terminal is not None
                and kind == "run.failed"
                and not queue_paused(self._read(db, session_id))
            ):
                self._insert(db, session_id, "queue.paused", {})

    def sessions_with_decisions(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT session_id FROM events "
                "WHERE type IN ('approval.granted', 'approval.denied', 'question.answered')"
            ).fetchall()
        return [row[0] for row in rows]

    def pending_runs(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT queued.data FROM events AS queued
                WHERE queued.type = 'run.queued' AND NOT EXISTS (
                    SELECT 1 FROM events AS ended
                    WHERE ended.session_id = queued.session_id
                    AND ended.type IN (
                        'run.waiting', 'run.completed', 'run.failed', 'run.cancelled'
                    )
                    AND json_extract(ended.data, '$.run_id') = json_extract(queued.data, '$.run_id')
                ) ORDER BY queued.created_at"""
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def unfinished_runs(self) -> list[tuple[str, str]]:
        placeholders = ", ".join("?" for _ in RUN_END_EVENTS)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT started.session_id, json_extract(started.data, '$.run_id')
                FROM events AS started WHERE started.type = 'run.started' AND NOT EXISTS (
                    SELECT 1 FROM events AS ended WHERE ended.session_id = started.session_id
                    AND ended.type IN ({placeholders})
                    AND json_extract(ended.data, '$.run_id')
                        = json_extract(started.data, '$.run_id')
                )""",
                RUN_END_EVENTS,
            ).fetchall()
        return [(row[0], row[1]) for row in rows]
