"""Scoped saved user preferences with monotonic revisions and forgetting.

Preferences are stored in SQLite (`scoped_preferences`) with explicit scope
(`global` or `skill:<name>`), canonical key, monotonic revision, and active vs
forgotten status, then projected authoritatively into `MEMORY.md` under
`## Saved Preferences` (distinct from `## Facts`).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Collection, Generator
from contextlib import closing, contextmanager, suppress
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai_harness import ArtifactScanner, Memory
from pydantic_ai_harness.memory import MemoryConflictError

MAIN = "MEMORY.md"
MAX_MEMORY_CHARS = 20_000
MAX_PREFERENCES_CHARS = 8_000
MAX_CLEARED_SHOWN = 20

GLOBAL_SCOPE = "global"
SKILL_PREFIX = "skill:"
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

PREFERENCES_HEADING = "## Saved Preferences"
FACTS_HEADING = "## Facts"
PREFERENCES_HEADER = (
    "Only the active preferences below apply "
    "(current revisions supersede older summaries and recalled conversations)."
)

# Memory and preferences are context, never authority: nothing saved here may widen what bud can do.
PERMISSION = re.compile(
    r"without (asking|approval|permission|checking)|(don'?t|no need to|never) (need to )?"
    r"(ask|check|confirm)|auto[- ]?(send|approve|accept)|skip (the )?approval|just send"
    r"|(you )?(can|may) (send|delete|accept|book)|approve (everything|all)",
    re.IGNORECASE,
)

_STOP_KEY_WORDS = frozenset(
    {"the", "a", "an", "owner", "prefers", "prefer", "to", "in", "on", "for", "of", "with", "is"}
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scoped_preferences (
    memory_scope TEXT NOT NULL,
    pref_scope TEXT NOT NULL,
    pref_key TEXT NOT NULL,
    value TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (memory_scope, pref_scope, pref_key)
);
CREATE TABLE IF NOT EXISTS preference_operations (
    id TEXT PRIMARY KEY,
    result_json TEXT NOT NULL
);
"""


class PreferenceSecurityError(ValueError):
    """Raised when a preference attempts to grant permissions or fails artifact scanning."""


class PreferenceRecord(BaseModel, frozen=True):
    scope: str
    key: str
    value: str
    revision: int
    status: Literal["active", "forgotten"]
    updated_at: str
    source: Literal["explicit", "learned"] = "explicit"


def norm_text(text: str) -> str:
    return " ".join(text.casefold().replace("’", "'").split())


def normalize_scope(scope: str | None) -> str:
    """Normalize a preference scope to `global` or `skill:<name>`."""
    if scope is None:
        return GLOBAL_SCOPE
    raw = scope.strip().lower()
    if not raw or raw == GLOBAL_SCOPE:
        return GLOBAL_SCOPE
    name = raw.removeprefix(SKILL_PREFIX).strip()
    if len(name) > 64 or not SKILL_NAME_RE.fullmatch(name):
        raise ValueError(
            "Scope must be 'global' or 'skill:<name>' using lowercase letters, digits, and hyphens."
        )
    return f"{SKILL_PREFIX}{name}"


def normalize_key(key: str) -> str:
    """Normalize a preference key into a canonical slug."""
    raw = re.sub(r"[\s/]+", "-", key.strip().lower()).strip("-")
    if not KEY_RE.fullmatch(raw):
        raise ValueError(
            "Key must be 1 to 64 characters of lowercase letters, digits, dots, "
            "underscores, or hyphens."
        )
    return raw


def auto_key(text: str) -> str:
    """Derive a deterministic key slug when a learned preference omits one."""
    tokens = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP_KEY_WORDS]
    slug = "-".join(tokens[:5])[:48].strip("-")
    if slug and KEY_RE.fullmatch(slug):
        return slug
    return f"pref-{sha256(norm_text(text).encode()).hexdigest()[:10]}"


def validate_preference_value(value: str, *, scanner: ArtifactScanner | None = None) -> str:
    """Clean and guard a preference value against permissions and artifact threats."""
    cleaned = " ".join(value.split())
    if not 3 <= len(cleaned) <= 300:
        raise ValueError("Preference value must be between 3 and 300 characters.")
    if PERMISSION.search(cleaned):
        raise PreferenceSecurityError(
            "Preferences cannot grant permissions, skip approvals, or automate restricted actions."
        )
    if scanner is not None:
        scan = scanner.scan(cleaned, source="memory:preferences")
        if scan.flagged:
            raise PreferenceSecurityError(
                f"Not saved: an artifact scan flagged the content ({scan.summary()}). "
                "Store the preference in plain words without instructions, HTML comments, "
                "or invisible characters."
            )
    return cleaned


def split_memory_sections(content: str) -> list[str]:
    """Return all non-preference lines (facts and legacy bullets) from MEMORY.md."""
    facts: list[str] = []
    in_preferences = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_preferences = stripped == PREFERENCES_HEADING
            if stripped in (PREFERENCES_HEADING, FACTS_HEADING):
                continue
        if in_preferences:
            continue
        if stripped:
            facts.append(line.rstrip())
    return facts


def _memory_bullet(record: PreferenceRecord) -> str:
    return (
        f"- [{record.scope} | key: {record.key} | rev {record.revision} | "
        f"updated {record.updated_at}] {record.value}"
    )


def render_memory_document(
    active: list[PreferenceRecord],
    forgotten: list[PreferenceRecord],
    facts_lines: list[str],
) -> str:
    """Render MEMORY.md with `## Saved Preferences` clearly separated from `## Facts`."""
    sections: list[str] = []
    if active or forgotten:
        pref_lines = [PREFERENCES_HEADING, PREFERENCES_HEADER]
        used = sum(len(line) + 1 for line in pref_lines)
        for rec in active:
            bullet = _memory_bullet(rec)
            if used + len(bullet) + 1 > MAX_PREFERENCES_CHARS:
                break
            pref_lines.append(bullet)
            used += len(bullet) + 1
        if forgotten:
            shown = forgotten[-MAX_CLEARED_SHOWN:]
            cleared = ", ".join(f"{rec.scope}/{rec.key} (rev {rec.revision})" for rec in shown)
            line = f"Cleared preferences (do not restore from older summaries): {cleared}"
            if used + len(line) + 1 <= MAX_PREFERENCES_CHARS:
                pref_lines.append(line)
        sections.append("\n".join(pref_lines))
    if facts_lines:
        sections.append("\n".join([FACTS_HEADING, *facts_lines]))
    return "\n\n".join(sections) + "\n" if sections else ""


def _enable_wal(connection: sqlite3.Connection) -> None:
    with suppress(sqlite3.OperationalError):
        connection.execute("PRAGMA journal_mode = WAL")


class PreferenceStore:
    """SQLite-backed scoped preference registry synced into `MEMORY.md`."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        memory: Memory | None = None,
        today: Callable[[], date] = date.today,
        scanner: ArtifactScanner | None = None,
    ) -> None:
        self.path = path
        self.memory = memory
        self.today = today
        self.scanner = scanner or (memory.scanner if memory is not None else ArtifactScanner())
        self._lock = threading.RLock()
        self._mem_conn: sqlite3.Connection | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
        with self._connect() as db:
            db.executescript(SCHEMA)

    @property
    def memory_scope(self) -> str:
        if self.memory is None:
            return "main"
        ns = self.memory.namespace
        if callable(ns):
            return self.memory.agent_name
        return f"{ns}/{self.memory.agent_name}" if ns else self.memory.agent_name

    @property
    def memory_path(self) -> str:
        return f"{self.memory_scope}/{MAIN}"

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        with self._lock:
            if self._mem_conn is not None:
                with self._mem_conn:
                    yield self._mem_conn
                return
            assert self.path is not None
            with closing(sqlite3.connect(self.path, timeout=30, check_same_thread=False)) as db:
                db.execute("PRAGMA busy_timeout = 30000")
                _enable_wal(db)
                with db:
                    yield db

    def _row_to_record(self, row: tuple[Any, ...]) -> PreferenceRecord:
        return PreferenceRecord(
            scope=row[0],
            key=row[1],
            value=row[2],
            revision=int(row[3]),
            status=row[4],
            updated_at=row[5],
            source=row[6],
        )

    def list_preferences(
        self, scope: str | None = None, *, include_forgotten: bool = False
    ) -> list[PreferenceRecord]:
        """Return saved preferences for this memory scope, global first."""
        pref_scope = normalize_scope(scope) if scope is not None else None
        clauses = ["memory_scope = ?"]
        params: list[Any] = [self.memory_scope]
        if pref_scope is not None:
            clauses.append("pref_scope = ?")
            params.append(pref_scope)
        if not include_forgotten:
            clauses.append("status = 'active'")
        where = " AND ".join(clauses)
        query = (
            "SELECT pref_scope, pref_key, value, revision, status, updated_at, source "
            f"FROM scoped_preferences WHERE {where} "
            "ORDER BY (pref_scope != 'global'), pref_scope, pref_key"
        )
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def _id(self, record: PreferenceRecord) -> str:
        return sha256(f"{self.memory_scope}/{record.scope}/{record.key}".encode()).hexdigest()[:32]

    def prompt_records(
        self,
        *,
        scopes: Collection[str] | None = None,
        snapshot: str | None = None,
        earlier: Collection[PreferenceRecord] = (),
    ) -> list[dict[str, str]]:
        """Return active preferences in the selected prompt scopes or exact memory snapshot."""
        lines = set(snapshot.splitlines()) if snapshot is not None else None
        matching = {
            self._id(record): record
            for record in (*earlier, *self.list_preferences())
            if (scopes is None or record.scope in scopes)
            and (lines is None or _memory_bullet(record) in lines)
        }
        return [
            {
                "id": pref_id,
                "text": record.value,
                "scope": record.scope,
                "source": record.source,
            }
            for pref_id, record in matching.items()
        ]

    async def forget_by_id(self, pref_id: str) -> bool:
        for record in self.list_preferences():
            if self._id(record) == pref_id:
                result = await self.forget(record.scope, record.key)
                return result["status"] == "forgotten"
        return False

    def get(self, scope: str, key: str) -> PreferenceRecord | None:
        """Look up one preference by scope and key."""
        pref_scope = normalize_scope(scope)
        pref_key = normalize_key(key)
        with self._connect() as db:
            row = db.execute(
                "SELECT pref_scope, pref_key, value, revision, status, updated_at, source "
                "FROM scoped_preferences "
                "WHERE memory_scope = ? AND pref_scope = ? AND pref_key = ?",
                (self.memory_scope, pref_scope, pref_key),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def describe_change(self, tool_args: dict[str, Any]) -> str:
        """Trusted summary of the target preference for approval cards and the reviewer."""
        try:
            pref_scope = normalize_scope(str(tool_args.get("scope", "")))
            pref_key = normalize_key(str(tool_args.get("key", "")))
        except ValueError:
            return "Scoped user preference."
        existing = self.get(pref_scope, pref_key)
        if existing is None:
            return f"New preference in {pref_scope} (key {pref_key!r})."
        if existing.status == "forgotten":
            return (
                f"Previously cleared preference in {pref_scope} "
                f"(key {pref_key!r}, rev {existing.revision})."
            )
        return (
            f"Existing preference in {pref_scope} (key {pref_key!r}, rev {existing.revision}): "
            f"{existing.value!r}"
        )

    async def set(
        self,
        scope: str,
        key: str,
        value: str,
        *,
        source: Literal["explicit", "learned"] = "explicit",
        today: date | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or supersede a scoped preference and sync `MEMORY.md`."""
        pref_scope = normalize_scope(scope)
        pref_key = normalize_key(key)
        cleaned = validate_preference_value(value, scanner=self.scanner)
        stamp = (today or self.today()).isoformat()
        mem_scope = self.memory_scope

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if operation_id is not None:
                receipt = db.execute(
                    "SELECT result_json FROM preference_operations WHERE id = ?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    return {**json.loads(receipt[0]), "replayed": True}

            row = db.execute(
                "SELECT value, revision, status FROM scoped_preferences "
                "WHERE memory_scope = ? AND pref_scope = ? AND pref_key = ?",
                (mem_scope, pref_scope, pref_key),
            ).fetchone()
            if row is not None and row[2] == "active" and norm_text(row[0]) == norm_text(cleaned):
                result: dict[str, Any] = {
                    "scope": pref_scope,
                    "key": pref_key,
                    "value": row[0],
                    "revision": int(row[1]),
                    "previous_revision": int(row[1]),
                    "previous_value": row[0],
                    "status": "unchanged",
                    "replayed": False,
                }
            else:
                prev_rev = int(row[1]) if row is not None else None
                prev_value = row[0] if (row is not None and row[2] == "active") else None
                new_rev = (prev_rev + 1) if prev_rev is not None else 1
                status = "updated" if row is not None else "created"
                db.execute(
                    "INSERT INTO scoped_preferences "
                    "(memory_scope, pref_scope, pref_key, value, revision, "
                    "status, updated_at, source) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?, ?) "
                    "ON CONFLICT(memory_scope, pref_scope, pref_key) DO UPDATE SET "
                    "value = excluded.value, revision = excluded.revision, status = 'active', "
                    "updated_at = excluded.updated_at, source = excluded.source",
                    (mem_scope, pref_scope, pref_key, cleaned, new_rev, stamp, source),
                )
                result = {
                    "scope": pref_scope,
                    "key": pref_key,
                    "value": cleaned,
                    "revision": new_rev,
                    "previous_revision": prev_rev,
                    "previous_value": prev_value,
                    "status": status,
                    "replayed": False,
                }
            if operation_id is not None:
                db.execute(
                    "INSERT INTO preference_operations(id, result_json) VALUES (?, ?)",
                    (operation_id, json.dumps(result)),
                )

        await self.sync_memory()
        return result

    async def forget(
        self,
        scope: str,
        key: str,
        *,
        today: date | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Forget an active preference, clear its value, bump revision, and sync `MEMORY.md`."""
        pref_scope = normalize_scope(scope)
        pref_key = normalize_key(key)
        stamp = (today or self.today()).isoformat()
        mem_scope = self.memory_scope

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if operation_id is not None:
                receipt = db.execute(
                    "SELECT result_json FROM preference_operations WHERE id = ?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    return {**json.loads(receipt[0]), "replayed": True}

            row = db.execute(
                "SELECT value, revision, status FROM scoped_preferences "
                "WHERE memory_scope = ? AND pref_scope = ? AND pref_key = ?",
                (mem_scope, pref_scope, pref_key),
            ).fetchone()
            if row is None or row[2] != "active":
                result: dict[str, Any] = {
                    "scope": pref_scope,
                    "key": pref_key,
                    "revision": int(row[1]) if row is not None else None,
                    "status": "not_found",
                    "replayed": False,
                }
            else:
                prev_rev = int(row[1])
                new_rev = prev_rev + 1
                db.execute(
                    "UPDATE scoped_preferences "
                    "SET value = '', revision = ?, status = 'forgotten', "
                    "updated_at = ?, source = 'explicit' "
                    "WHERE memory_scope = ? AND pref_scope = ? AND pref_key = ?",
                    (new_rev, stamp, mem_scope, pref_scope, pref_key),
                )
                result = {
                    "scope": pref_scope,
                    "key": pref_key,
                    "revision": new_rev,
                    "previous_revision": prev_rev,
                    "status": "forgotten",
                    "replayed": False,
                }
            if operation_id is not None:
                db.execute(
                    "INSERT INTO preference_operations(id, result_json) VALUES (?, ?)",
                    (operation_id, json.dumps(result)),
                )

        await self.sync_memory()
        return result

    async def sync_memory(self) -> None:
        """Project current scoped preferences into `MEMORY.md` alongside existing `## Facts`."""
        if self.memory is None:
            return
        store = self.memory.store
        assert not callable(store)
        path = self.memory_path
        for _ in range(8):
            current = await store.read(path, max_chars=MAX_MEMORY_CHARS)
            all_rows = self.list_preferences(include_forgotten=True)
            active = [rec for rec in all_rows if rec.status == "active"]
            forgotten = [rec for rec in all_rows if rec.status == "forgotten"]
            existing = current.content if current else ""
            facts = split_memory_sections(existing)
            rendered = render_memory_document(active, forgotten, facts)
            if rendered == existing:
                return
            try:
                await store.write(
                    path, rendered, expected_version=current.version if current else None
                )
                return
            except MemoryConflictError:
                continue
        raise RuntimeError("MEMORY.md changed repeatedly while syncing preferences.")

    def inspect_preferences(self, scope: str | None = None) -> dict[str, Any]:
        """Inspect currently active saved user preferences.

        Args:
            scope: Optional filter: `"global"` or `"skill:<name>"` (or a skill name).
                Omit to inspect all scopes.
        """
        try:
            normalized = normalize_scope(scope) if scope is not None else None
        except ValueError as exc:
            raise ModelRetry(str(exc)) from None
        all_rows = self.list_preferences(normalized, include_forgotten=True)
        active = [
            {
                "scope": rec.scope,
                "key": rec.key,
                "value": rec.value,
                "revision": rec.revision,
                "updated_at": rec.updated_at,
            }
            for rec in all_rows
            if rec.status == "active"
        ]
        cleared = [
            {"scope": rec.scope, "key": rec.key, "revision": rec.revision}
            for rec in all_rows
            if rec.status == "forgotten"
        ]
        return {"preferences": active, "cleared": cleared}

    async def set_preference(
        self, ctx: RunContext[None], scope: str, key: str, value: str
    ) -> dict[str, Any]:
        """Save or update a lasting user preference in a scope (`global` or `skill:<name>`).

        Requires approval. Supersedes any earlier revision of the same scope and key.
        Never use this for one-off instructions or for permissions/approval overrides.

        Args:
            scope: `"global"` or `"skill:<name>"` (e.g. `"skill:attention-briefing"`).
            key: Short canonical slug for the preference (e.g. `"format"`, `"email-signoff"`).
            value: Plain-language preference rule (3 to 300 characters).
        """
        approval = (ctx.tool_call_metadata or {}).get("approval_id")
        operation_id = f"pref:set:{approval}" if ctx.tool_call_approved and approval else None
        try:
            return await self.set(scope, key, value, source="explicit", operation_id=operation_id)
        except PreferenceSecurityError as exc:
            raise ToolFailed(str(exc)) from None
        except ValueError as exc:
            raise ModelRetry(str(exc)) from None

    async def forget_preference(
        self, ctx: RunContext[None], scope: str, key: str
    ) -> dict[str, Any]:
        """Forget a saved user preference by scope and key so it no longer applies.

        Requires approval. Removes the active preference and prevents older summaries
        from restoring it.

        Args:
            scope: `"global"` or `"skill:<name>"` (e.g. `"skill:attention-briefing"`).
            key: Key slug of the preference to forget (from `inspect_preferences`).
        """
        approval = (ctx.tool_call_metadata or {}).get("approval_id")
        operation_id = f"pref:forget:{approval}" if ctx.tool_call_approved and approval else None
        try:
            return await self.forget(scope, key, operation_id=operation_id)
        except ValueError as exc:
            raise ModelRetry(str(exc)) from None

    def tools(self) -> list[Tool[None]]:
        return [
            Tool(self.inspect_preferences),
            Tool(self.set_preference),
            Tool(self.forget_preference),
        ]
