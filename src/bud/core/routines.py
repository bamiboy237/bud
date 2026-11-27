"""Durable skill-based scheduled routines, occurrence claims, and delivery tracking."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import stat
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, Field, SerializeAsAny

from bud.core.hub import Hub
from bud.core.log import _is_job_session
from bud.core.projection import (
    PendingApproval,
    RoutineBudget,
    RoutineDestination,
    RoutineOccurrenceView,
    RoutineView,
    parked_approvals,
    queue_paused,
    runs,
    undecided_approvals,
)
from bud.skills import SkillStore, valid_name
from bud.skills.installer import SkillError
from bud.workspace import PRIVATE_WORDS

logger = logging.getLogger(__name__)

MAX_CATCH_UP = 5
DEFAULT_APPROVAL_EXPIRY_SECONDS = 86_400
DEFAULT_GRACE_SECONDS = 300
RECENT_RUN_LIMIT = 10

MissedRunPolicy = Literal["run_once", "skip", "catch_up"]
OverlapPolicy = Literal["skip", "queue"]
RoutineStatus = Literal["active", "paused"]
OccurrenceExecutionStatus = Literal[
    "pending",
    "queued",
    "running",
    "awaiting_approval",
    "completed",
    "failed",
    "cancelled",
    "needs_review",
    "skipped",
]
OccurrenceDeliveryStatus = Literal[
    "pending",
    "claimed",
    "dispatched",
    "confirmed",
    "failed",
    "needs_review",
    "not_required",
]

ACTIVE_OCCURRENCE_STATUSES = ("pending", "queued", "running", "awaiting_approval")


class RoutineError(Exception):
    """Base error for routine validation, state, or lookup failures."""


class RoutineNotFoundError(RoutineError):
    pass


class RoutineValidationError(RoutineError):
    pass


class RoutineConflictError(RoutineError):
    pass


def validate_cron(expr: str) -> str:
    """Validate and normalize a 5-field standard cron expression."""
    normalized = " ".join(expr.split())
    parts = normalized.split(" ")
    if len(parts) != 5 or not croniter.is_valid(normalized):
        raise RoutineValidationError(
            "Use a standard 5-field cron expression (minute hour day month weekday)."
        )
    return normalized


def validate_timezone(name: str) -> ZoneInfo:
    """Validate an IANA timezone name."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise RoutineValidationError(f"Unknown IANA timezone: {name!r}.") from exc


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def next_slot_after(cron: str, timezone: str, after_utc: datetime) -> datetime:
    """Return the first cron slot strictly after `after_utc`, in UTC."""
    zone = validate_timezone(timezone)
    anchor = _as_utc(after_utc).astimezone(zone)
    nxt = croniter(cron, anchor).get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=zone)
    return nxt.astimezone(UTC)


def slots_due(
    cron: str,
    timezone: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    policy: MissedRunPolicy,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
) -> list[tuple[datetime, Literal["scheduled", "missed"]]]:
    """Return due cron slots in `(start_utc, end_utc]` according to `missed_run_policy`."""
    start = _as_utc(start_utc)
    end = _as_utc(end_utc)
    if end <= start:
        return []
    zone = validate_timezone(timezone)
    it = croniter(cron, (end + timedelta(microseconds=1)).astimezone(zone))
    latest_local = it.get_prev(datetime)
    if latest_local.tzinfo is None:
        latest_local = latest_local.replace(tzinfo=zone)
    latest = latest_local.astimezone(UTC)
    if latest <= start:
        return []
    grace_cutoff = end - timedelta(seconds=grace_seconds)
    if policy == "skip":
        if latest < grace_cutoff:
            return []
        return [(latest, "scheduled")]
    if policy == "run_once":
        # Coalesce one or more missed slots into a single occurrence for the latest slot.
        prev_local = it.get_prev(datetime)
        if prev_local.tzinfo is None:
            prev_local = prev_local.replace(tzinfo=zone)
        prev = prev_local.astimezone(UTC)
        missed = prev > start or latest < grace_cutoff
        return [(latest, "missed" if missed else "scheduled")]
    # policy == "catch_up": bounded chronological replay up to MAX_CATCH_UP slots.
    collected: list[tuple[datetime, Literal["scheduled", "missed"]]] = []
    cur = latest
    while cur > start and len(collected) < MAX_CATCH_UP:
        kind: Literal["scheduled", "missed"] = "missed" if cur < grace_cutoff else "scheduled"
        collected.append((cur, kind))
        prev_local = it.get_prev(datetime)
        if prev_local.tzinfo is None:
            prev_local = prev_local.replace(tzinfo=zone)
        cur = prev_local.astimezone(UTC)
    collected.reverse()
    return collected


def _letter(number: int) -> str:
    result = ""
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def routine_report_name(routine: RoutineDefinition, occurrence_id: str, finished: datetime) -> str:
    """Workspace path for a routine occurrence report, filtering private filename words."""
    words = re.findall(r"[a-z0-9]+", routine.title.lower())
    slug = "-".join(word for word in words if word not in PRIVATE_WORDS)[:50].strip("-")
    return (
        f"reports/{finished:%Y-%m-%d-%H%M%S}-routine-{routine.routine_seq}-"
        f"{occurrence_id[:8]}-{slug or 'report'}.md"
    )


def save_routine_report(
    workspace: Path, name: str, title: str, routine_seq: int, finished: datetime, text: str
) -> bool:
    """Write a routine report once without following symlinks or replacing existing files."""
    folder, file = name.split("/")
    stamp = f"Routine {routine_seq}, finished {finished:%Y-%m-%d %H:%M} UTC."
    body = f"# {title}\n\n{stamp}\n\n{text}\n".encode()
    directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(workspace, directory)
    try:
        with suppress(FileExistsError):
            os.mkdir(folder, 0o700, dir_fd=root)
        reports = os.open(folder, directory, dir_fd=root)
        try:
            try:
                fd = os.open(
                    file,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=reports,
                )
            except FileExistsError:
                found = os.stat(file, dir_fd=reports, follow_symlinks=False)
                return stat.S_ISREG(found.st_mode)
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
            return True
        finally:
            os.close(reports)
    finally:
        os.close(root)


class RoutineDefinition(BaseModel):
    routine_id: str
    routine_seq: int
    source_id: str
    revision: int
    feed_id: str
    title: str
    skill: str
    prompt: str
    cron: str
    timezone: str
    missed_run_policy: MissedRunPolicy = "run_once"
    overlap_policy: OverlapPolicy = "skip"
    approval_expiry_seconds: int | None = DEFAULT_APPROVAL_EXPIRY_SECONDS
    budget: RoutineBudget
    owner_request: str
    destination: RoutineDestination
    status: RoutineStatus = "active"
    pause_reason: str | None = None
    last_evaluated_at: datetime
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RoutineOccurrence(BaseModel):
    occurrence_id: str
    routine_id: str
    routine_revision: int
    feed_id: str
    session_id: str
    run_id: str | None = None
    dedup_key: str
    trigger_kind: Literal["scheduled", "missed", "manual"]
    scheduled_for: datetime
    triggered_at: datetime
    execution_status: OccurrenceExecutionStatus
    delivery_status: OccurrenceDeliveryStatus
    dispatch_claim_id: str | None = None
    delivery_receipt: str | None = None
    delivery_confirmed_at: datetime | None = None
    report_path: str | None = None
    uncertain_tools: list[str] = []
    error: str | None = None
    completed_at: datetime | None = None

    def to_view(self) -> RoutineOccurrenceView:
        return RoutineOccurrenceView(
            occurrence_id=self.occurrence_id,
            routine_id=self.routine_id,
            routine_revision=self.routine_revision,
            feed_id=self.feed_id,
            session_id=self.session_id,
            run_id=self.run_id,
            dedup_key=self.dedup_key,
            trigger_kind=self.trigger_kind,
            scheduled_for=self.scheduled_for.isoformat(),
            triggered_at=self.triggered_at.isoformat(),
            execution_status=self.execution_status,
            delivery_status=self.delivery_status,
            dispatch_claim_id=self.dispatch_claim_id,
            delivery_receipt=self.delivery_receipt,
            delivery_confirmed_at=(
                self.delivery_confirmed_at.isoformat() if self.delivery_confirmed_at else None
            ),
            report_path=self.report_path,
            uncertain_tools=list(self.uncertain_tools),
            error=self.error,
            completed_at=self.completed_at.isoformat() if self.completed_at else None,
        )


class RoutineRunView(RoutineOccurrenceView):
    pending_approvals: list[PendingApproval] = Field(default_factory=list)
    unconfirmed_actions: list[dict[str, Any]] = Field(default_factory=list)


class RoutineInspectionView(RoutineView):
    active_occurrence: SerializeAsAny[RoutineOccurrenceView] | None = None
    latest_occurrence: SerializeAsAny[RoutineOccurrenceView] | None = None
    recent_occurrences: list[SerializeAsAny[RoutineOccurrenceView]] = Field(default_factory=list)


_ROUTINE_COLS = (
    "routine_id, routine_seq, source_id, revision, feed_id, title, skill, prompt, cron, "
    "timezone, missed_run_policy, overlap_policy, approval_expiry_seconds, budget_json, "
    "owner_request, destination_json, status, pause_reason, last_evaluated_at, next_run_at, "
    "last_run_at, created_at, updated_at"
)

_OCC_COLS = (
    "occurrence_id, routine_id, routine_revision, feed_id, session_id, run_id, dedup_key, "
    "trigger_kind, scheduled_for, triggered_at, execution_status, delivery_status, "
    "dispatch_claim_id, delivery_receipt, delivery_confirmed_at, report_path, "
    "uncertain_tools_json, error, completed_at"
)


class RoutineStore:
    """SQLite-backed routine definitions, occurrences, dedup keys, and delivery claims."""

    def __init__(
        self,
        hub: Hub,
        *,
        skills: SkillStore | None = None,
        workspace: Path | None = None,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
    ) -> None:
        self.hub = hub
        self.log = hub.log
        self.skills = skills
        self.workspace = workspace
        self.grace_seconds = grace_seconds
        with self.log._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS routines (
                    routine_id TEXT PRIMARY KEY,
                    routine_seq INTEGER NOT NULL UNIQUE,
                    source_id TEXT NOT NULL UNIQUE,
                    revision INTEGER NOT NULL DEFAULT 1,
                    feed_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    skill TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    cron TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    missed_run_policy TEXT NOT NULL DEFAULT 'run_once',
                    overlap_policy TEXT NOT NULL DEFAULT 'skip',
                    approval_expiry_seconds INTEGER,
                    budget_json TEXT NOT NULL,
                    owner_request TEXT NOT NULL,
                    destination_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    pause_reason TEXT,
                    last_evaluated_at TEXT NOT NULL,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS routines_by_feed ON routines(feed_id);
                CREATE INDEX IF NOT EXISTS routines_by_status ON routines(status, next_run_at);

                CREATE TABLE IF NOT EXISTS routine_occurrences (
                    occurrence_id TEXT PRIMARY KEY,
                    routine_id TEXT NOT NULL,
                    routine_revision INTEGER NOT NULL,
                    feed_id TEXT NOT NULL,
                    session_id TEXT NOT NULL UNIQUE,
                    run_id TEXT,
                    dedup_key TEXT NOT NULL UNIQUE,
                    trigger_kind TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    triggered_at TEXT NOT NULL,
                    execution_status TEXT NOT NULL,
                    delivery_status TEXT NOT NULL,
                    dispatch_claim_id TEXT,
                    delivery_receipt TEXT,
                    delivery_confirmed_at TEXT,
                    report_path TEXT,
                    uncertain_tools_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS routine_occ_by_routine
                    ON routine_occurrences(routine_id, triggered_at);
                CREATE INDEX IF NOT EXISTS routine_occ_by_feed
                    ON routine_occurrences(feed_id);
                """
            )

    @staticmethod
    def _routine(row: tuple[Any, ...]) -> RoutineDefinition:
        return RoutineDefinition(
            routine_id=row[0],
            routine_seq=int(row[1]),
            source_id=row[2],
            revision=int(row[3]),
            feed_id=row[4],
            title=row[5],
            skill=row[6],
            prompt=row[7],
            cron=row[8],
            timezone=row[9],
            missed_run_policy=row[10],
            overlap_policy=row[11],
            approval_expiry_seconds=row[12],
            budget=RoutineBudget.model_validate_json(row[13]),
            owner_request=row[14],
            destination=RoutineDestination.model_validate_json(row[15]),
            status=row[16],
            pause_reason=row[17],
            last_evaluated_at=datetime.fromisoformat(row[18]),
            next_run_at=datetime.fromisoformat(row[19]) if row[19] else None,
            last_run_at=datetime.fromisoformat(row[20]) if row[20] else None,
            created_at=datetime.fromisoformat(row[21]),
            updated_at=datetime.fromisoformat(row[22]),
        )

    @staticmethod
    def _occ(row: tuple[Any, ...]) -> RoutineOccurrence:
        return RoutineOccurrence(
            occurrence_id=row[0],
            routine_id=row[1],
            routine_revision=int(row[2]),
            feed_id=row[3],
            session_id=row[4],
            run_id=row[5],
            dedup_key=row[6],
            trigger_kind=row[7],
            scheduled_for=datetime.fromisoformat(row[8]),
            triggered_at=datetime.fromisoformat(row[9]),
            execution_status=row[10],
            delivery_status=row[11],
            dispatch_claim_id=row[12],
            delivery_receipt=row[13],
            delivery_confirmed_at=datetime.fromisoformat(row[14]) if row[14] else None,
            report_path=row[15],
            uncertain_tools=json.loads(row[16]),
            error=row[17],
            completed_at=datetime.fromisoformat(row[18]) if row[18] else None,
        )

    def _validate_skill(self, skill: str) -> str:
        try:
            valid_name(skill)
        except SkillError as exc:
            raise RoutineValidationError(str(exc)) from None
        if self.skills is not None and skill not in self.skills.snapshot():
            raise RoutineValidationError(f"Skill {skill!r} is not installed or enabled.")
        return skill

    def _validate_feed(self, db: sqlite3.Connection, feed_id: str) -> None:
        exists = db.execute(
            "SELECT 1 FROM events WHERE session_id = ? LIMIT 1", (feed_id,)
        ).fetchone()
        if not exists or _is_job_session(db, feed_id):
            raise RoutineValidationError(f"Unknown conversation session: {feed_id!r}.")

    @staticmethod
    def _validate_text(value: str, label: str, *, max_len: int) -> str:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > max_len:
            raise RoutineValidationError(f"{label} must be 1 to {max_len} characters.")
        return cleaned

    def create(
        self,
        *,
        feed_id: str,
        title: str,
        skill: str,
        prompt: str,
        cron: str,
        timezone: str = "UTC",
        missed_run_policy: MissedRunPolicy = "run_once",
        overlap_policy: OverlapPolicy = "skip",
        approval_expiry_seconds: int | None = DEFAULT_APPROVAL_EXPIRY_SECONDS,
        budget: RoutineBudget | None = None,
        destination: RoutineDestination | None = None,
        source_id: str | None = None,
        owner_request: str | None = None,
        now: datetime | None = None,
    ) -> RoutineDefinition:
        clean_title = self._validate_text(title, "Title", max_len=120)
        clean_prompt = self._validate_text(prompt, "Prompt", max_len=10_000)
        clean_skill = self._validate_skill(skill)
        clean_cron = validate_cron(cron)
        validate_timezone(timezone)
        if approval_expiry_seconds is not None and approval_expiry_seconds < 1:
            raise RoutineValidationError("approval_expiry_seconds must be positive or null.")
        budget_cfg = budget or RoutineBudget()
        dest_cfg = destination or RoutineDestination(feed_id=feed_id)
        if dest_cfg.feed_id != feed_id:
            # Verify destination feed exists too when specified explicitly.
            with self.log._connect() as db:
                self._validate_feed(db, dest_cfg.feed_id)
        now_utc = _as_utc(now or datetime.now(UTC))
        next_run = next_slot_after(clean_cron, timezone, now_utc)
        src = source_id or uuid4().hex
        routine_id = uuid4().hex
        request = clean_prompt if owner_request is None else owner_request

        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_feed(db, feed_id)
            self._validate_feed(db, dest_cfg.feed_id)
            existing = db.execute(
                f"SELECT {_ROUTINE_COLS} FROM routines WHERE source_id = ?", (src,)
            ).fetchone()
            if existing is not None:
                found = self._routine(existing)
                if (
                    found.feed_id,
                    found.title,
                    found.skill,
                    found.prompt,
                    found.owner_request,
                    found.cron,
                    found.timezone,
                    found.missed_run_policy,
                    found.overlap_policy,
                    found.approval_expiry_seconds,
                    found.budget,
                    found.destination,
                ) != (
                    feed_id,
                    clean_title,
                    clean_skill,
                    clean_prompt,
                    request,
                    clean_cron,
                    timezone,
                    missed_run_policy,
                    overlap_policy,
                    approval_expiry_seconds,
                    budget_cfg,
                    dest_cfg,
                ):
                    raise RoutineConflictError(
                        "Routine source_id already used with different configuration."
                    )
                return found
            max_seq = db.execute("SELECT COALESCE(MAX(routine_seq), 0) FROM routines").fetchone()[0]
            routine_seq = int(max_seq) + 1
            db.execute(
                f"""INSERT INTO routines ({_ROUTINE_COLS})
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    routine_id,
                    routine_seq,
                    src,
                    1,
                    feed_id,
                    clean_title,
                    clean_skill,
                    clean_prompt,
                    clean_cron,
                    timezone,
                    missed_run_policy,
                    overlap_policy,
                    approval_expiry_seconds,
                    budget_cfg.model_dump_json(),
                    request,
                    dest_cfg.model_dump_json(),
                    "active",
                    None,
                    now_utc.isoformat(),
                    next_run.isoformat(),
                    None,
                    now_utc.isoformat(),
                    now_utc.isoformat(),
                ),
            )
            self.log._insert(
                db,
                feed_id,
                "routine.created",
                {
                    "routine_id": routine_id,
                    "routine_seq": routine_seq,
                    "title": clean_title,
                    "skill": clean_skill,
                    "cron": clean_cron,
                    "timezone": timezone,
                    "next_run_at": next_run.isoformat(),
                },
                once=True,
                key=f"routine:created:{routine_id}",
            )
        self.hub.wake(feed_id)
        return self.get(routine_id)

    def get(self, routine_id: str) -> RoutineDefinition:
        with self.log._connect() as db:
            row = db.execute(
                f"SELECT {_ROUTINE_COLS} FROM routines WHERE routine_id = ?", (routine_id,)
            ).fetchone()
        if row is None:
            raise RoutineNotFoundError(f"Unknown routine: {routine_id}")
        return self._routine(row)

    def list_routines(self, feed_id: str | None = None) -> list[RoutineDefinition]:
        with self.log._connect() as db:
            rows = db.execute(
                f"SELECT {_ROUTINE_COLS} FROM routines"
                + (" WHERE feed_id = ?" if feed_id is not None else "")
                + " ORDER BY routine_seq",
                (feed_id,) if feed_id is not None else (),
            ).fetchall()
        return [self._routine(row) for row in rows]

    def edit(
        self,
        routine_id: str,
        *,
        now: datetime | None = None,
        title: str | None = None,
        skill: str | None = None,
        prompt: str | None = None,
        cron: str | None = None,
        timezone: str | None = None,
        missed_run_policy: MissedRunPolicy | None = None,
        overlap_policy: OverlapPolicy | None = None,
        approval_expiry_seconds: int | None = None,
        clear_approval_expiry: bool = False,
        budget: RoutineBudget | None = None,
        destination: RoutineDestination | None = None,
    ) -> RoutineDefinition:
        now_utc = _as_utc(now or datetime.now(UTC))
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT {_ROUTINE_COLS} FROM routines WHERE routine_id = ?", (routine_id,)
            ).fetchone()
            if row is None:
                raise RoutineNotFoundError(f"Unknown routine: {routine_id}")
            current = self._routine(row)
            new_title = (
                self._validate_text(title, "Title", max_len=120)
                if title is not None
                else current.title
            )
            new_prompt = (
                self._validate_text(prompt, "Prompt", max_len=10_000)
                if prompt is not None
                else current.prompt
            )
            new_skill = self._validate_skill(skill) if skill is not None else current.skill
            new_cron = validate_cron(cron) if cron is not None else current.cron
            new_tz = timezone if timezone is not None else current.timezone
            validate_timezone(new_tz)
            new_missed = missed_run_policy or current.missed_run_policy
            new_overlap = overlap_policy or current.overlap_policy
            if clear_approval_expiry:
                new_expiry = None
            elif approval_expiry_seconds is not None:
                if approval_expiry_seconds < 1:
                    raise RoutineValidationError(
                        "approval_expiry_seconds must be positive or null."
                    )
                new_expiry = approval_expiry_seconds
            else:
                new_expiry = current.approval_expiry_seconds
            new_budget = budget if budget is not None else current.budget
            new_dest = destination if destination is not None else current.destination
            self._validate_feed(db, new_dest.feed_id)
            new_rev = current.revision + 1
            # Do not retroactively trigger old schedule slots after an edit.
            next_run = (
                next_slot_after(new_cron, new_tz, now_utc) if current.status == "active" else None
            )
            db.execute(
                """UPDATE routines
                   SET revision=?, title=?, skill=?, prompt=?, cron=?, timezone=?,
                       missed_run_policy=?, overlap_policy=?, approval_expiry_seconds=?,
                       budget_json=?, owner_request=?, destination_json=?,
                       last_evaluated_at=?, next_run_at=?, updated_at=?
                   WHERE routine_id=?""",
                (
                    new_rev,
                    new_title,
                    new_skill,
                    new_prompt,
                    new_cron,
                    new_tz,
                    new_missed,
                    new_overlap,
                    new_expiry,
                    new_budget.model_dump_json(),
                    new_prompt,
                    new_dest.model_dump_json(),
                    now_utc.isoformat(),
                    next_run.isoformat() if next_run else None,
                    now_utc.isoformat(),
                    routine_id,
                ),
            )
            self.log._insert(
                db,
                current.feed_id,
                "routine.updated",
                {
                    "routine_id": routine_id,
                    "revision": new_rev,
                    "title": new_title,
                    "skill": new_skill,
                    "cron": new_cron,
                    "timezone": new_tz,
                    "status": current.status,
                    "next_run_at": next_run.isoformat() if next_run else None,
                },
            )
        self.hub.wake(current.feed_id)
        return self.get(routine_id)

    def _pause_in_tx(
        self, db: sqlite3.Connection, routine_id: str, now_utc: datetime, reason: str
    ) -> str | None:
        row = db.execute(
            f"SELECT {_ROUTINE_COLS} FROM routines WHERE routine_id = ?", (routine_id,)
        ).fetchone()
        if row is None:
            raise RoutineNotFoundError(f"Unknown routine: {routine_id}")
        current = self._routine(row)
        # Preserve "uncertain" escalation even if another pause tries to overwrite it.
        effective_reason = (
            "uncertain"
            if current.status == "paused"
            and current.pause_reason == "uncertain"
            and reason != "uncertain"
            else reason
        )
        if current.status == "paused" and current.pause_reason == effective_reason:
            return None
        db.execute(
            """UPDATE routines
               SET status='paused', pause_reason=?, last_evaluated_at=?,
                   next_run_at=NULL, updated_at=?
               WHERE routine_id=?""",
            (effective_reason, now_utc.isoformat(), now_utc.isoformat(), routine_id),
        )
        self.log._insert(
            db,
            current.feed_id,
            "routine.paused",
            {"routine_id": routine_id, "reason": effective_reason},
        )
        return current.feed_id

    def pause(
        self, routine_id: str, *, now: datetime | None = None, reason: str = "owner"
    ) -> RoutineDefinition:
        now_utc = _as_utc(now or datetime.now(UTC))
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            wake_feed = self._pause_in_tx(db, routine_id, now_utc, reason)
        if wake_feed is not None:
            self.hub.wake(wake_feed)
        return self.get(routine_id)

    def pause_for_stop(self, routine_id: str, *, now: datetime) -> list[str]:
        """Pause the schedule and record stop requests before another action can run."""
        sessions: list[str] = []
        reason = "Stopped by the user."
        now_utc = _as_utc(now)
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            wake_feed = self._pause_in_tx(db, routine_id, now_utc, "owner")
            rows = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE routine_id = ? "
                f"AND execution_status IN ({','.join('?' for _ in ACTIVE_OCCURRENCE_STATUSES)})",
                (routine_id, *ACTIVE_OCCURRENCE_STATUSES),
            ).fetchall()
            for row in rows:
                occ = self._occ(row)
                events = self.log._read(db, occ.session_id)
                states = runs(events)
                current = states[-1] if states else None
                run_id = current.run_id if current else occ.run_id
                if run_id is None or (
                    current
                    and current.status not in ("queued", "running", "awaiting_approval", "stopping")
                ):
                    continue
                if not queue_paused(events):
                    self.log._insert(db, occ.session_id, "queue.paused", {"reason": "stopped"})
                self.log._insert(
                    db,
                    occ.session_id,
                    "run.stop_requested",
                    {"run_id": run_id, "reason": reason},
                    once=True,
                    key=f"{run_id}:stop",
                )
                for request in undecided_approvals(events) + parked_approvals(events):
                    self.log._insert(
                        db,
                        occ.session_id,
                        "approval.denied",
                        {"approval_id": request["approval_id"], "reason": reason},
                        once=True,
                    )
                if current is None or current.status == "awaiting_approval":
                    self.log._ack_stop(db, occ.session_id, run_id)
                if current is None:
                    db.execute(
                        "UPDATE routine_occurrences SET execution_status = 'cancelled', "
                        "delivery_status = 'not_required', error = ?, completed_at = ? "
                        "WHERE occurrence_id = ?",
                        (reason, now_utc.isoformat(), occ.occurrence_id),
                    )
                sessions.append(occ.session_id)
        if wake_feed is not None:
            self.hub.wake(wake_feed)
        return sessions

    def resume(
        self,
        routine_id: str,
        *,
        now: datetime | None = None,
        acknowledge_uncertain: bool = False,
    ) -> RoutineDefinition:
        now_utc = _as_utc(now or datetime.now(UTC))
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT {_ROUTINE_COLS} FROM routines WHERE routine_id = ?", (routine_id,)
            ).fetchone()
            if row is None:
                raise RoutineNotFoundError(f"Unknown routine: {routine_id}")
            current = self._routine(row)
            unresolved = db.execute(
                """SELECT occurrence_id FROM routine_occurrences
                   WHERE routine_id = ?
                   AND (execution_status = 'needs_review' OR delivery_status = 'needs_review')""",
                (routine_id,),
            ).fetchall()
            if (current.pause_reason == "uncertain" or unresolved) and not acknowledge_uncertain:
                raise RoutineConflictError(
                    "Routine has uncertain side effects or unconfirmed delivery; inspect and "
                    "pass acknowledge_uncertain=True before resuming."
                )
            if acknowledge_uncertain and unresolved:
                for (occ_id,) in unresolved:
                    db.execute(
                        """UPDATE routine_occurrences
                           SET execution_status = CASE
                               WHEN execution_status = 'needs_review' THEN 'cancelled'
                               ELSE execution_status END,
                               delivery_status = CASE
                               WHEN delivery_status = 'needs_review' THEN 'failed'
                               ELSE delivery_status END
                           WHERE occurrence_id = ?""",
                        (occ_id,),
                    )
            if current.status == "active":
                return current
            next_run = next_slot_after(current.cron, current.timezone, now_utc)
            db.execute(
                """UPDATE routines
                   SET status='active', pause_reason=NULL, last_evaluated_at=?,
                       next_run_at=?, updated_at=?
                   WHERE routine_id=?""",
                (now_utc.isoformat(), next_run.isoformat(), now_utc.isoformat(), routine_id),
            )
            self.log._insert(
                db,
                current.feed_id,
                "routine.resumed",
                {"routine_id": routine_id, "next_run_at": next_run.isoformat()},
            )
        self.hub.wake(current.feed_id)
        return self.get(routine_id)

    def _insert_occurrence(
        self,
        db: sqlite3.Connection,
        routine: RoutineDefinition,
        *,
        dedup_key: str,
        trigger_kind: Literal["scheduled", "missed", "manual"],
        scheduled_for: datetime,
        now_utc: datetime,
    ) -> RoutineOccurrence | None:
        occ_id = uuid4().hex
        session_id = uuid4().hex
        run_id = uuid4().hex
        cursor = db.execute(
            f"""INSERT OR IGNORE INTO routine_occurrences ({_OCC_COLS})
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                occ_id,
                routine.routine_id,
                routine.revision,
                routine.feed_id,
                session_id,
                run_id,
                dedup_key,
                trigger_kind,
                _as_utc(scheduled_for).isoformat(),
                now_utc.isoformat(),
                "pending",
                "pending",
                None,
                None,
                None,
                None,
                "[]",
                None,
                None,
            ),
        )
        if cursor.rowcount == 0:
            return None
        self.log._insert(db, session_id, "session.created", {"routine_id": routine.routine_id})
        self.log._insert(
            db,
            routine.feed_id,
            "routine.triggered",
            {
                "routine_id": routine.routine_id,
                "occurrence_id": occ_id,
                "routine_revision": routine.revision,
                "trigger_kind": trigger_kind,
                "scheduled_for": _as_utc(scheduled_for).isoformat(),
                "dedup_key": dedup_key,
                "origin": routine.destination.origin.model_dump(),
            },
            once=True,
            key=f"routine:trigger:{dedup_key}",
        )
        row = db.execute(
            f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE occurrence_id = ?", (occ_id,)
        ).fetchone()
        assert row is not None
        return self._occ(row)

    def claim_scheduled_occurrences(
        self, routine_id: str, now: datetime | None = None
    ) -> list[RoutineOccurrence]:
        """Evaluate due/missed slots for one routine under a write lock and dedup by slot."""
        now_utc = _as_utc(now or datetime.now(UTC))
        created: list[RoutineOccurrence] = []
        feed_to_wake: str | None = None
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT {_ROUTINE_COLS} FROM routines WHERE routine_id = ?", (routine_id,)
            ).fetchone()
            if row is None:
                return []
            routine = self._routine(row)
            if routine.status != "active":
                # Advance watermark so paused periods are never backfilled upon resume.
                if now_utc > routine.last_evaluated_at:
                    db.execute(
                        "UPDATE routines SET last_evaluated_at = ? WHERE routine_id = ?",
                        (now_utc.isoformat(), routine_id),
                    )
                return []
            if now_utc <= routine.last_evaluated_at:
                return []
            due = slots_due(
                routine.cron,
                routine.timezone,
                routine.last_evaluated_at,
                now_utc,
                policy=routine.missed_run_policy,
                grace_seconds=self.grace_seconds,
            )
            next_run = next_slot_after(routine.cron, routine.timezone, now_utc)
            if not due:
                db.execute(
                    "UPDATE routines SET last_evaluated_at=?, next_run_at=? WHERE routine_id=?",
                    (now_utc.isoformat(), next_run.isoformat(), routine_id),
                )
                return []
            active_count = db.execute(
                f"""SELECT COUNT(*) FROM routine_occurrences
                    WHERE routine_id = ? AND execution_status IN
                    ({",".join("?" for _ in ACTIVE_OCCURRENCE_STATUSES)})""",
                (routine_id, *ACTIVE_OCCURRENCE_STATUSES),
            ).fetchone()[0]
            if routine.overlap_policy == "skip" and active_count > 0:
                db.execute(
                    "UPDATE routines SET last_evaluated_at=?, next_run_at=? WHERE routine_id=?",
                    (now_utc.isoformat(), next_run.isoformat(), routine_id),
                )
                return []
            for slot_utc, trigger_kind in due:
                dedup_key = f"sched:{routine_id}:{slot_utc.isoformat()}"
                occ = self._insert_occurrence(
                    db,
                    routine,
                    dedup_key=dedup_key,
                    trigger_kind=trigger_kind,
                    scheduled_for=slot_utc,
                    now_utc=now_utc,
                )
                if occ is not None:
                    created.append(occ)
            last_run = (
                now_utc.isoformat()
                if created
                else (routine.last_run_at.isoformat() if routine.last_run_at else None)
            )
            db.execute(
                """UPDATE routines
                   SET last_evaluated_at=?, next_run_at=?, last_run_at=?, updated_at=?
                   WHERE routine_id=?""",
                (
                    now_utc.isoformat(),
                    next_run.isoformat(),
                    last_run,
                    now_utc.isoformat(),
                    routine_id,
                ),
            )
            feed_to_wake = routine.feed_id
        if created and feed_to_wake is not None:
            self.hub.wake(feed_to_wake)
        return created

    def claim_manual_occurrence(
        self,
        routine_id: str,
        *,
        now: datetime | None = None,
        trigger_id: str | None = None,
    ) -> tuple[RoutineOccurrence, bool]:
        """Trigger an immediate occurrence (idempotent by trigger_id). Keeps paused status."""
        now_utc = _as_utc(now or datetime.now(UTC))
        trig = trigger_id or uuid4().hex
        dedup_key = f"manual:{routine_id}:{trig}"
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT {_ROUTINE_COLS} FROM routines WHERE routine_id = ?", (routine_id,)
            ).fetchone()
            if row is None:
                raise RoutineNotFoundError(f"Unknown routine: {routine_id}")
            routine = self._routine(row)
            existing = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
            if existing is not None:
                return self._occ(existing), False
            if routine.pause_reason == "uncertain":
                raise RoutineConflictError(
                    "Routine has uncertain side effects; inspect and acknowledge before running."
                )
            unresolved = db.execute(
                """SELECT 1 FROM routine_occurrences
                   WHERE routine_id = ?
                   AND (execution_status = 'needs_review' OR delivery_status = 'needs_review')
                   LIMIT 1""",
                (routine_id,),
            ).fetchone()
            if unresolved is not None:
                raise RoutineConflictError(
                    "Routine has an occurrence needing review; resolve before running again."
                )
            active = db.execute(
                f"""SELECT 1 FROM routine_occurrences
                    WHERE routine_id = ? AND execution_status IN
                    ({",".join("?" for _ in ACTIVE_OCCURRENCE_STATUSES)}) LIMIT 1""",
                (routine_id, *ACTIVE_OCCURRENCE_STATUSES),
            ).fetchone()
            if active is not None:
                raise RoutineConflictError("An occurrence of this routine is already active.")
            occ = self._insert_occurrence(
                db,
                routine,
                dedup_key=dedup_key,
                trigger_kind="manual",
                scheduled_for=now_utc,
                now_utc=now_utc,
            )
            assert occ is not None
            db.execute(
                "UPDATE routines SET last_run_at=?, updated_at=? WHERE routine_id=?",
                (now_utc.isoformat(), now_utc.isoformat(), routine_id),
            )
        self.hub.wake(routine.feed_id)
        return occ, True

    def occurrence(self, occurrence_id: str) -> RoutineOccurrence:
        with self.log._connect() as db:
            row = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
        if row is None:
            raise RoutineNotFoundError(f"Unknown occurrence: {occurrence_id}")
        return self._occ(row)

    def occurrence_for_session(self, session_id: str) -> RoutineOccurrence | None:
        with self.log._connect() as db:
            row = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._occ(row) if row is not None else None

    def occurrences(
        self, routine_id: str, *, limit: int = 20, offset: int = 0
    ) -> list[RoutineOccurrence]:
        with self.log._connect() as db:
            rows = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE routine_id = ? "
                "ORDER BY triggered_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (routine_id, limit, offset),
            ).fetchall()
        return [self._occ(row) for row in rows]

    def occurrence_page(
        self, routine_id: str, *, limit: int, offset: int
    ) -> tuple[list[RoutineOccurrence], bool]:
        rows = self.occurrences(routine_id, limit=limit + 1, offset=offset)
        return rows[:limit], len(rows) > limit

    def active_occurrences(
        self, *, feed_id: str | None = None, routine_id: str | None = None
    ) -> list[RoutineOccurrence]:
        clauses = [f"execution_status IN ({','.join('?' for _ in ACTIVE_OCCURRENCE_STATUSES)})"]
        params: list[Any] = list(ACTIVE_OCCURRENCE_STATUSES)
        if feed_id is not None:
            clauses.append("feed_id = ?")
            params.append(feed_id)
        if routine_id is not None:
            clauses.append("routine_id = ?")
            params.append(routine_id)
        with self.log._connect() as db:
            rows = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE {' AND '.join(clauses)} "
                "ORDER BY triggered_at, rowid",
                params,
            ).fetchall()
        return [self._occ(row) for row in rows]

    def pending_dispatches(self) -> list[RoutineOccurrence]:
        with self.log._connect() as db:
            rows = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE execution_status = 'pending' "
                "ORDER BY triggered_at"
            ).fetchall()
        return [self._occ(row) for row in rows]

    def unconfirmed_deliveries(self) -> list[RoutineOccurrence]:
        with self.log._connect() as db:
            rows = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences "
                "WHERE execution_status = 'completed' "
                "AND delivery_status IN ('pending', 'claimed') ORDER BY triggered_at"
            ).fetchall()
        return [self._occ(row) for row in rows]

    def open_approval_sessions(self) -> list[str]:
        with self.log._connect() as db:
            rows = db.execute(
                "SELECT session_id FROM routine_occurrences "
                "WHERE execution_status = 'awaiting_approval'"
            ).fetchall()
        return [row[0] for row in rows]

    def mark_queued(self, occurrence_id: str) -> None:
        with self.log._connect() as db:
            db.execute(
                "UPDATE routine_occurrences SET execution_status = 'queued' "
                "WHERE occurrence_id = ? AND execution_status = 'pending'",
                (occurrence_id,),
            )

    def sync_execution(
        self,
        session_id: str,
        status: OccurrenceExecutionStatus,
        *,
        uncertain_tools: list[str] | None = None,
        error: str | None = None,
        completed_at: datetime | None = None,
    ) -> RoutineOccurrence | None:
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            occ = self._occ(row)
            if (
                occ.execution_status == "cancelled"
                and status in ACTIVE_OCCURRENCE_STATUSES
                and occ.run_id is not None
                and self.log._stopped(db, occ.run_id)
            ):
                return occ
            tools = uncertain_tools if uncertain_tools is not None else occ.uncertain_tools
            delivery = occ.delivery_status
            if status == "needs_review":
                delivery = "needs_review"
            elif status in ("cancelled", "failed") and delivery == "pending":
                delivery = "not_required"
            done_at = (
                _as_utc(completed_at).isoformat()
                if completed_at is not None
                else (
                    occ.completed_at.isoformat()
                    if occ.completed_at is not None
                    else (
                        datetime.now(UTC).isoformat()
                        if status in ("completed", "failed", "cancelled", "needs_review")
                        else None
                    )
                )
            )
            db.execute(
                """UPDATE routine_occurrences
                   SET execution_status=?, delivery_status=?, uncertain_tools_json=?,
                       error=?, completed_at=?
                   WHERE occurrence_id=?""",
                (
                    status,
                    delivery,
                    json.dumps(tools),
                    error if error is not None else occ.error,
                    done_at,
                    occ.occurrence_id,
                ),
            )
            if status == "needs_review":
                self._pause_in_tx(db, occ.routine_id, datetime.now(UTC), "uncertain")
            updated = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE occurrence_id = ?",
                (occ.occurrence_id,),
            ).fetchone()
            assert updated is not None
            return self._occ(updated)

    def claim_delivery(self, occurrence_id: str) -> RoutineOccurrence | None:
        """Persist a delivery dispatch claim BEFORE writing report/posting to destination."""
        claim_id = f"deliver:{occurrence_id}"
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            if row is None:
                return None
            occ = self._occ(row)
            if occ.delivery_status != "pending":
                return None
            db.execute(
                """UPDATE routine_occurrences
                   SET delivery_status='claimed', dispatch_claim_id=?
                   WHERE occurrence_id=? AND delivery_status='pending'""",
                (claim_id, occurrence_id),
            )
            updated = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            assert updated is not None
            return self._occ(updated)

    def mark_delivery_dispatched(
        self,
        occurrence_id: str,
        *,
        receipt: str | None = None,
        report_path: str | None = None,
    ) -> RoutineOccurrence:
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            if row is None:
                raise RoutineNotFoundError(f"Unknown occurrence: {occurrence_id}")
            occ = self._occ(row)
            if occ.delivery_status == "confirmed":
                return occ
            db.execute(
                """UPDATE routine_occurrences
                   SET delivery_status='dispatched', delivery_receipt=COALESCE(?, delivery_receipt),
                       report_path=COALESCE(?, report_path)
                   WHERE occurrence_id=?""",
                (receipt, report_path, occurrence_id),
            )
        return self.occurrence(occurrence_id)

    def confirm_delivery(
        self,
        occurrence_id: str,
        *,
        receipt: str | None = None,
        report_path: str | None = None,
        confirmed_at: datetime | None = None,
    ) -> RoutineOccurrence:
        now_utc = _as_utc(confirmed_at or datetime.now(UTC))
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            if row is None:
                raise RoutineNotFoundError(f"Unknown occurrence: {occurrence_id}")
            occ = self._occ(row)
            if occ.delivery_status == "confirmed":
                return occ
            final_receipt = receipt or occ.delivery_receipt or occ.dispatch_claim_id
            db.execute(
                """UPDATE routine_occurrences
                   SET delivery_status='confirmed',
                       delivery_receipt=?,
                       delivery_confirmed_at=?,
                       report_path=COALESCE(?, report_path)
                   WHERE occurrence_id=?""",
                (final_receipt, now_utc.isoformat(), report_path, occurrence_id),
            )
            routine_row = db.execute(
                "SELECT destination_json FROM routines WHERE routine_id = ?", (occ.routine_id,)
            ).fetchone()
            dest_feed = (
                RoutineDestination.model_validate_json(routine_row[0]).feed_id
                if routine_row is not None
                else occ.feed_id
            )
            self.log._insert(
                db,
                dest_feed,
                "routine.delivery_confirmed",
                {
                    "routine_id": occ.routine_id,
                    "occurrence_id": occurrence_id,
                    "dispatch_claim_id": occ.dispatch_claim_id,
                    "delivery_receipt": final_receipt,
                    "report_path": report_path or occ.report_path,
                },
                once=True,
                key=f"routine:occ:{occurrence_id}:confirmed",
            )
        self.hub.wake(dest_feed)
        return self.occurrence(occurrence_id)

    def mark_delivery_needs_review(self, occurrence_id: str, reason: str) -> RoutineOccurrence:
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT {_OCC_COLS} FROM routine_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            if row is None:
                raise RoutineNotFoundError(f"Unknown occurrence: {occurrence_id}")
            occ = self._occ(row)
            db.execute(
                """UPDATE routine_occurrences
                   SET delivery_status='needs_review', error=COALESCE(error, ?)
                   WHERE occurrence_id=?""",
                (reason, occurrence_id),
            )
            self.log._insert(
                db,
                occ.feed_id,
                "routine.needs_review",
                {
                    "routine_id": occ.routine_id,
                    "occurrence_id": occurrence_id,
                    "dispatch_claim_id": occ.dispatch_claim_id,
                    "text": reason,
                },
                once=True,
                key=f"routine:occ:{occurrence_id}:delivery_review",
            )
            wake_feed = self._pause_in_tx(db, occ.routine_id, datetime.now(UTC), "uncertain")
        if wake_feed is not None:
            self.hub.wake(wake_feed)
        return self.occurrence(occurrence_id)

    def _references(self, routine: RoutineDefinition) -> dict[str, str]:
        """Assign unique R<seq>-<letter> labels across all occurrences of this routine."""
        with self.log._connect() as db:
            rows = db.execute(
                "SELECT session_id FROM routine_occurrences WHERE routine_id = ? "
                "ORDER BY triggered_at ASC, rowid ASC",
                (routine.routine_id,),
            ).fetchall()
        refs: dict[str, str] = {}
        index = 1
        for (occ_session,) in rows:
            for event in self.log.read(occ_session):
                if event.type == "approval.requested":
                    refs[event.data["approval_id"]] = f"R{routine.routine_seq}-{_letter(index)}"
                    index += 1
        return refs

    def resolve_approval(self, feed_id: str, reference: str) -> tuple[str, str] | None:
        match = re.fullmatch(r"R([1-9][0-9]*)-([A-Z]+)", reference.upper())
        routines = self.list_routines(feed_id)
        if match:
            seq = int(match[1])
            routines = [r for r in routines if r.routine_seq == seq]
        fallback: tuple[str, str] | None = None
        for routine in routines:
            refs = self._references(routine)
            for occ in self.occurrences(routine.routine_id, limit=20):
                events = self.log.read(occ.session_id)
                open_ids = {
                    req["approval_id"]
                    for req in [*undecided_approvals(events), *parked_approvals(events)]
                }
                for req in undecided_approvals(events) + parked_approvals(events):
                    approval_id = req["approval_id"]
                    label = refs.get(approval_id, "")
                    if reference == approval_id or reference.upper() == label:
                        return occ.session_id, approval_id
                if fallback is None:
                    for event in events:
                        if event.type != "approval.requested":
                            continue
                        approval_id = event.data["approval_id"]
                        if approval_id in open_ids:
                            continue
                        label = refs.get(approval_id, "")
                        if reference == approval_id or reference.upper() == label:
                            fallback = (occ.session_id, approval_id)
        return fallback

    def run_view(
        self, occ: RoutineOccurrence, refs: dict[str, str] | None = None
    ) -> RoutineRunView:
        events = self.log.read(occ.session_id)
        requests = undecided_approvals(events) + parked_approvals(events)
        if requests and refs is None:
            refs = self._references(self.get(occ.routine_id))
        pending = [
            PendingApproval.model_validate(
                {
                    **req,
                    "routine_id": occ.routine_id,
                    "occurrence_id": occ.occurrence_id,
                    "reference": (refs or {}).get(req["approval_id"]),
                }
            )
            for req in requests
        ]
        call_args = {}
        approval_args = {}
        returned = set()
        decisions = []
        for event in events:
            if event.type == "tool.call":
                call_args[event.data["run_id"], event.data["call_id"]] = event.data.get("args")
            elif event.type == "tool.result":
                returned.add((event.data["run_id"], event.data["call_id"]))
            elif event.type == "action.decided" and event.data["outcome"] in (
                "approved",
                "allowed",
                "auto-allow",
            ):
                decisions.append(event.data)
            elif event.type == "approval.requested":
                approval_args[event.data["call_id"]] = event.data.get("args")
        unconfirmed = [
            {
                "run_id": action["run_id"],
                "call_id": action["call_id"],
                "tool": action["tool"],
                "args": call_args.get(
                    (action["run_id"], action["call_id"]), approval_args.get(action["call_id"])
                ),
            }
            for action in decisions
            if (action["run_id"], action["call_id"]) not in returned
        ]
        return RoutineRunView(
            **occ.to_view().model_dump(), pending_approvals=pending, unconfirmed_actions=unconfirmed
        )

    def view(
        self,
        routine: RoutineDefinition,
        *,
        recent: list[RoutineOccurrence] | None = None,
        offset: int = 0,
    ) -> RoutineInspectionView:
        if recent is None:
            recent = self.occurrences(routine.routine_id, limit=RECENT_RUN_LIMIT)
        latest = self.occurrences(routine.routine_id, limit=1) if offset else recent[:1]
        active = self.active_occurrences(routine_id=routine.routine_id)
        refs = self._references(routine)
        views = {occ.occurrence_id: self.run_view(occ, refs) for occ in recent}
        for occ in latest + active:
            if occ.occurrence_id not in views:
                views[occ.occurrence_id] = self.run_view(occ, refs)
        return RoutineInspectionView(
            routine_id=routine.routine_id,
            routine_seq=routine.routine_seq,
            revision=routine.revision,
            feed_id=routine.feed_id,
            title=routine.title,
            skill=routine.skill,
            prompt=routine.prompt,
            cron=routine.cron,
            timezone=routine.timezone,
            missed_run_policy=routine.missed_run_policy,
            overlap_policy=routine.overlap_policy,
            approval_expiry_seconds=routine.approval_expiry_seconds,
            budget=routine.budget,
            destination=routine.destination,
            status=routine.status,
            pause_reason=routine.pause_reason,
            last_evaluated_at=routine.last_evaluated_at.isoformat(),
            next_run_at=routine.next_run_at.isoformat() if routine.next_run_at else None,
            last_run_at=routine.last_run_at.isoformat() if routine.last_run_at else None,
            active_occurrence=views[active[-1].occurrence_id] if active else None,
            latest_occurrence=views[latest[0].occurrence_id] if latest else None,
            pending_approvals=[
                approval
                for occ in reversed(active)
                if occ.execution_status == "awaiting_approval"
                for approval in views[occ.occurrence_id].pending_approvals
            ],
            recent_occurrences=[views[occ.occurrence_id] for occ in recent],
        )

    def views(self, feed_id: str | None = None) -> list[RoutineView]:
        return [self.view(routine) for routine in self.list_routines(feed_id)]
