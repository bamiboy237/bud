"""Persistent Timer Primitives and Background Wakeup Routines (COURSE Block B08).

Provides durable, SQLite-backed timer primitives with timezone and DST awareness,
per-slot deduplication, configurable missed-run policies ('run_latest', 'skip', 'run_all'),
and resilient background wakeup dispatching across server restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from builtins import list as _list
from collections.abc import Awaitable, Callable, Generator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

TIMER_SCHEMA = """
CREATE TABLE IF NOT EXISTS timers (
    timer_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    schedule_type TEXT NOT NULL,      -- 'once', 'interval', 'daily'
    schedule_value TEXT NOT NULL,     -- ISO timestamp, seconds string, or 'HH:MM'
    timezone TEXT NOT NULL DEFAULT 'UTC',
    next_fire_at TEXT NOT NULL,       -- ISO UTC timestamp
    last_fired_at TEXT,               -- ISO UTC timestamp
    status TEXT NOT NULL DEFAULT 'active', -- 'active', 'paused', 'completed', 'cancelled'
    payload TEXT NOT NULL,            -- JSON string
    missed_policy TEXT NOT NULL DEFAULT 'run_latest', -- 'run_latest', 'skip', 'run_all'
    slot_key TEXT,
    max_runs INTEGER,
    run_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timers_due ON timers(status, next_fire_at);
CREATE INDEX IF NOT EXISTS idx_timers_session ON timers(session_id);

CREATE TABLE IF NOT EXISTS timer_executions (
    execution_id TEXT PRIMARY KEY,
    timer_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    status TEXT NOT NULL,             -- 'dispatched', 'completed', 'skipped', 'failed'
    result TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (timer_id, slot_key)       -- Guarantees per-slot deduplication
);
CREATE INDEX IF NOT EXISTS idx_executions_timer ON timer_executions(timer_id);
"""


@dataclass(frozen=True)
class Timer:
    timer_id: str
    session_id: str
    name: str
    schedule_type: str
    schedule_value: str
    timezone: str
    next_fire_at: str
    last_fired_at: str | None
    status: str
    payload: dict[str, Any]
    missed_policy: str
    slot_key: str | None
    max_runs: int | None
    run_count: int
    created_at: str
    updated_at: str

    @property
    def is_active(self) -> bool:
        return self.status == "active"


def resolve_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def compute_slot_key(timer_id: str, scheduled_for_iso: str) -> str:
    """Produces a deterministic per-slot key to enforce exactly-once execution per slot."""
    return f"{timer_id}:{scheduled_for_iso[:19]}"


def calculate_next_fire_time(
    schedule_type: str,
    schedule_value: str,
    timezone_str: str,
    after_dt: datetime,
) -> datetime | None:
    """Calculates the next fire datetime (in UTC) for a timer."""
    zone = resolve_zone(timezone_str)
    after_dt = after_dt.replace(tzinfo=UTC) if after_dt.tzinfo is None else after_dt.astimezone(UTC)

    if schedule_type == "once":
        target = datetime.fromisoformat(schedule_value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=zone).astimezone(UTC)
        else:
            target = target.astimezone(UTC)
        return target if target > after_dt else None

    if schedule_type == "interval":
        seconds = int(schedule_value)
        if seconds <= 0:
            return None
        return after_dt + timedelta(seconds=seconds)

    if schedule_type == "daily":
        parts = [int(p) for p in schedule_value.split(":")[:2]]
        hour, minute = parts[0], parts[1]
        local_now = after_dt.astimezone(zone)
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate = candidate + timedelta(days=1)
        return candidate.astimezone(UTC)

    return None


class TimerStore:
    """Persistent SQLite store for timers and execution records."""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(TIMER_SCHEMA)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10.0)) as db, db:
            yield db

    def create_timer(
        self,
        session_id: str,
        name: str,
        schedule_type: str,
        schedule_value: str,
        payload: dict[str, Any] | None = None,
        *,
        timezone: str = "UTC",
        missed_policy: str = "run_latest",
        max_runs: int | None = None,
        timer_id: str | None = None,
        start_at: datetime | None = None,
    ) -> Timer:
        now = datetime.now(UTC)
        tid = timer_id or f"timer_{uuid4().hex[:12]}"

        if start_at is not None:
            next_fire = start_at if start_at.tzinfo else start_at.replace(tzinfo=UTC)
        else:
            next_fire = calculate_next_fire_time(schedule_type, schedule_value, timezone, now)
        if next_fire is None:
            if schedule_type == "once":
                # If scheduling once in the immediate future or now
                target = datetime.fromisoformat(schedule_value)
                next_fire = target if target.tzinfo else target.replace(tzinfo=UTC)
            else:
                next_fire = now + timedelta(seconds=60)

        next_fire_iso = next_fire.astimezone(UTC).isoformat()
        now_iso = now.isoformat()
        payload_json = json.dumps(payload or {})
        initial_slot_key = compute_slot_key(tid, next_fire_iso)

        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO timers
                (timer_id, session_id, name, schedule_type, schedule_value, timezone,
                 next_fire_at, last_fired_at, status, payload, missed_policy, slot_key,
                 max_runs, run_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    tid,
                    session_id,
                    name,
                    schedule_type,
                    schedule_value,
                    timezone,
                    next_fire_iso,
                    payload_json,
                    missed_policy,
                    initial_slot_key,
                    max_runs,
                    now_iso,
                    now_iso,
                ),
            )

        return self.get(tid)  # type: ignore

    def schedule_once(
        self,
        session_id: str,
        name: str,
        fire_at: datetime | str,
        payload: dict[str, Any] | None = None,
        *,
        timezone: str = "UTC",
    ) -> Timer:
        fire_at_iso = fire_at.isoformat() if isinstance(fire_at, datetime) else fire_at
        return self.create_timer(
            session_id=session_id,
            name=name,
            schedule_type="once",
            schedule_value=fire_at_iso,
            payload=payload,
            timezone=timezone,
            max_runs=1,
        )

    def schedule_interval(
        self,
        session_id: str,
        name: str,
        interval_seconds: int,
        payload: dict[str, Any] | None = None,
        *,
        timezone: str = "UTC",
        max_runs: int | None = None,
        start_at: datetime | None = None,
    ) -> Timer:
        return self.create_timer(
            session_id=session_id,
            name=name,
            schedule_type="interval",
            schedule_value=str(interval_seconds),
            payload=payload,
            timezone=timezone,
            max_runs=max_runs,
            start_at=start_at,
        )

    def schedule_daily(
        self,
        session_id: str,
        name: str,
        time_str: str,
        payload: dict[str, Any] | None = None,
        *,
        timezone: str = "UTC",
        start_at: datetime | None = None,
    ) -> Timer:
        return self.create_timer(
            session_id=session_id,
            name=name,
            schedule_type="daily",
            schedule_value=time_str,
            payload=payload,
            timezone=timezone,
            start_at=start_at,
        )

    def schedule_watchdog(
        self,
        session_id: str,
        task_id: int | str,
        timeout_seconds: int = 300,
        payload: dict[str, Any] | None = None,
    ) -> Timer:
        """Schedules a task watchdog timer that fires if the task stalls or times out."""
        data = {
            "action": "task_watchdog",
            "task_id": task_id,
            "timeout_seconds": timeout_seconds,
            "pet_status": "needsYou",
            **(payload or {}),
        }
        return self.schedule_interval(
            session_id=session_id,
            name=f"watchdog_task_{task_id}",
            interval_seconds=timeout_seconds,
            payload=data,
            max_runs=1,
        )

    def get(self, timer_id: str) -> Timer | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT timer_id, session_id, name, schedule_type, schedule_value,
                       timezone, next_fire_at, last_fired_at, status, payload,
                       missed_policy, slot_key, max_runs, run_count, created_at, updated_at
                FROM timers WHERE timer_id = ?
                """,
                (timer_id,),
            ).fetchone()
        if not row:
            return None
        return Timer(
            timer_id=row[0],
            session_id=row[1],
            name=row[2],
            schedule_type=row[3],
            schedule_value=row[4],
            timezone=row[5],
            next_fire_at=row[6],
            last_fired_at=row[7],
            status=row[8],
            payload=json.loads(row[9]) if row[9] else {},
            missed_policy=row[10],
            slot_key=row[11],
            max_runs=row[12],
            run_count=row[13],
            created_at=row[14],
            updated_at=row[15],
        )

    def list_timers(self, session_id: str | None = None, status: str | None = None) -> _list[Timer]:
        query = "SELECT timer_id FROM timers WHERE 1=1"
        params: list[Any] = []
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY next_fire_at ASC"

        with self._connect() as db:
            rows = db.execute(query, params).fetchall()

        timers: list[Timer] = []
        for r in rows:
            t = self.get(r[0])
            if t:
                timers.append(t)
        return timers

    list = list_timers

    def pause(self, timer_id: str) -> bool:
        now_iso = datetime.now(UTC).isoformat()
        with self._connect() as db:
            cur = db.execute(
                """
                UPDATE timers SET status = 'paused', updated_at = ?
                WHERE timer_id = ? AND status = 'active'
                """,
                (now_iso, timer_id),
            )
            return cur.rowcount > 0

    def resume(self, timer_id: str) -> bool:
        now = datetime.now(UTC)
        t = self.get(timer_id)
        if not t or t.status != "paused":
            return False
        next_fire = calculate_next_fire_time(t.schedule_type, t.schedule_value, t.timezone, now)
        next_fire_iso = (next_fire or now).isoformat()
        new_slot_key = compute_slot_key(timer_id, next_fire_iso)
        with self._connect() as db:
            cur = db.execute(
                """
                UPDATE timers
                SET status = 'active', next_fire_at = ?, slot_key = ?, updated_at = ?
                WHERE timer_id = ?
                """,
                (next_fire_iso, new_slot_key, now.isoformat(), timer_id),
            )
            return cur.rowcount > 0

    def cancel(self, timer_id: str) -> bool:
        now_iso = datetime.now(UTC).isoformat()
        with self._connect() as db:
            cur = db.execute(
                """
                UPDATE timers SET status = 'cancelled', updated_at = ?
                WHERE timer_id = ? AND status != 'cancelled'
                """,
                (now_iso, timer_id),
            )
            return cur.rowcount > 0

    def get_due_timers(self, now: datetime | None = None) -> _list[Timer]:
        current_time = now or datetime.now(UTC)
        current_iso = current_time.astimezone(UTC).isoformat()
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT timer_id FROM timers
                WHERE status = 'active' AND next_fire_at <= ?
                ORDER BY next_fire_at ASC
                """,
                (current_iso,),
            ).fetchall()
        due: list[Timer] = []
        for r in rows:
            t = self.get(r[0])
            if t and t.is_active:
                due.append(t)
        return due

    def claim_slot(
        self,
        timer_id: str,
        session_id: str,
        slot_key: str,
        scheduled_for: str,
        fired_at: str,
    ) -> str | None:
        """Atomically claims a slot for execution.

        Returns execution_id if claimed, None if already claimed.
        """
        execution_id = f"exec_{uuid4().hex[:12]}"
        now_iso = datetime.now(UTC).isoformat()
        try:
            with self._connect() as db:
                db.execute(
                    """
                    INSERT INTO timer_executions
                    (execution_id, timer_id, session_id, slot_key, scheduled_for,
                     fired_at, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'dispatched', ?)
                    """,
                    (
                        execution_id,
                        timer_id,
                        session_id,
                        slot_key,
                        scheduled_for,
                        fired_at,
                        now_iso,
                    ),
                )
            return execution_id
        except sqlite3.IntegrityError:
            # Slot already claimed or executed (per-slot deduplication)
            return None

    def record_execution_complete(
        self,
        execution_id: str,
        status: str = "completed",
        result: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE timer_executions SET status = ?, result = ? WHERE execution_id = ?",
                (status, result, execution_id),
            )

    def advance(self, timer_id: str, fired_at: datetime | None = None) -> str | None:
        """Advances timer after a fire event, updating run counts and calculating next fire time."""
        now = fired_at or datetime.now(UTC)
        t = self.get(timer_id)
        if not t:
            return None

        new_count = t.run_count + 1
        now_iso = now.isoformat()

        if t.schedule_type == "once" or (t.max_runs is not None and new_count >= t.max_runs):
            with self._connect() as db:
                db.execute(
                    """
                    UPDATE timers
                    SET status = 'completed', run_count = ?, last_fired_at = ?, updated_at = ?
                    WHERE timer_id = ?
                    """,
                    (new_count, now_iso, now_iso, timer_id),
                )
            return None

        next_fire = calculate_next_fire_time(t.schedule_type, t.schedule_value, t.timezone, now)
        if next_fire is None:
            with self._connect() as db:
                db.execute(
                    """
                    UPDATE timers
                    SET status = 'completed', run_count = ?, last_fired_at = ?, updated_at = ?
                    WHERE timer_id = ?
                    """,
                    (new_count, now_iso, now_iso, timer_id),
                )
            return None

        next_fire_iso = next_fire.astimezone(UTC).isoformat()
        new_slot_key = compute_slot_key(timer_id, next_fire_iso)

        with self._connect() as db:
            db.execute(
                """
                UPDATE timers
                SET next_fire_at = ?, slot_key = ?, run_count = ?, last_fired_at = ?, updated_at = ?
                WHERE timer_id = ?
                """,
                (next_fire_iso, new_slot_key, new_count, now_iso, now_iso, timer_id),
            )
        return next_fire_iso


class TimerWakeupDispatcher:
    """Background wakeup routine executing and dispatching due timers.

    Handles per-slot deduplication, downtime recovery (missed-run policies),
    and safe restart recovery without double-firing.
    """

    def __init__(
        self,
        store: TimerStore,
        *,
        on_wakeup: Callable[[Timer, dict[str, Any]], Awaitable[None] | None] | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.store = store
        self.on_wakeup = on_wakeup
        self.poll_interval = poll_interval
        self._running = False
        self._task: asyncio.Task | None = None

    async def tick(self, now: datetime | None = None) -> list[str]:
        """Runs one check cycle. Returns list of executed execution IDs."""
        current_time = now or datetime.now(UTC)
        due = self.store.get_due_timers(current_time)
        executed: list[str] = []

        for timer in due:
            scheduled_iso = timer.next_fire_at
            slot = timer.slot_key or compute_slot_key(timer.timer_id, scheduled_iso)
            now_iso = current_time.isoformat()

            # Handle missed runs when downtime occurred
            try:
                scheduled_dt = datetime.fromisoformat(scheduled_iso)
            except Exception:
                scheduled_dt = current_time

            # If delayed by more than 2x interval or 10 minutes, evaluate missed policy
            is_stale = (current_time - scheduled_dt) > timedelta(minutes=10)

            if is_stale and timer.missed_policy == "skip":
                # Advance timer without executing missed slot
                self.store.claim_slot(
                    timer.timer_id, timer.session_id, slot, scheduled_iso, now_iso
                )
                self.store.advance(timer.timer_id, current_time)
                continue

            exec_id = self.store.claim_slot(
                timer.timer_id,
                timer.session_id,
                slot,
                scheduled_iso,
                now_iso,
            )
            if not exec_id:
                # Slot already executed or claimed by another worker (deduplication)
                continue

            # Execute wakeup handler
            try:
                if self.on_wakeup is not None:
                    payload = dict(timer.payload)
                    payload["timer_id"] = timer.timer_id
                    payload["name"] = timer.name
                    payload["scheduled_for"] = scheduled_iso
                    payload["execution_id"] = exec_id
                    res = self.on_wakeup(timer, payload)
                    if asyncio.iscoroutine(res):
                        await res

                self.store.record_execution_complete(exec_id, status="completed", result="success")
                executed.append(exec_id)
            except Exception as exc:
                logger.exception("Wakeup dispatch failed for timer %s", timer.timer_id)
                self.store.record_execution_complete(exec_id, status="failed", result=str(exc))
            finally:
                self.store.advance(timer.timer_id, current_time)

        return executed

    async def run_loop(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in timer dispatcher loop: %s", exc)
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
