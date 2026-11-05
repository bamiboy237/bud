"""Temporary tool results, retained while their session is running or awaiting approval."""

import asyncio
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field
from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition
from pydantic_ai_harness import ToolOutputLimits
from pydantic_ai_harness.tool_output_limits import Band, Spill, Truncate

from bud.harness.code import inside_code_mode

SPILL_CHARS = 10_000
PREVIEW_CHARS = 1_000


class SqliteResultStore:
    def __init__(self, path: Path, *, retention: timedelta) -> None:
        if retention.total_seconds() <= 0:
            raise ValueError("Tool-result retention must be positive")
        self.path = path
        self.retention = retention
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS tool_results (
                    handle TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    last_used REAL NOT NULL
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS result_last_used ON tool_results(last_used)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    async def write(self, key: str, data: bytes) -> str:
        return await asyncio.to_thread(self._write, key, data)

    def _write(self, key: str, data: bytes) -> str:
        run_id, separator, _ = key.partition("/")
        if not separator:
            raise ValueError("Tool-result handles must identify their run")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                "SELECT session_id FROM events WHERE event_key = ? AND type = 'run.queued'",
                (f"{run_id}:queued",),
            ).fetchone()
            if owner is None:
                raise ValueError("Cannot store a tool result without an accepted run")
            db.execute(
                """INSERT INTO tool_results VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(handle) DO UPDATE SET
                    payload = excluded.payload, last_used = excluded.last_used""",
                (key, run_id, owner[0], data, time.time()),
            )
        return key

    async def read(self, handle: str) -> bytes:
        return await asyncio.to_thread(self._read, handle)

    def _read(self, handle: str, max_bytes: int | None = None) -> bytes:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT CASE WHEN ? IS NULL OR length(payload) <= ? THEN payload END
                   FROM tool_results WHERE handle = ?""",
                (max_bytes, max_bytes, handle),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("Tool result expired or is unavailable")
            if row[0] is None:
                raise ValueError("Stored result exceeds 8 MB. Read source data in smaller batches.")
            db.execute(
                "UPDATE tool_results SET last_used = ? WHERE handle = ?", (time.time(), handle)
            )
            return row[0]

    async def read_tool_result_chars(
        self,
        handle: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=4_000)] = 4_000,
        from_end: bool = False,
    ) -> str:
        """Read a stored result by character offset, including oversized single-line JSON.

        Args:
            handle: Exact handle from a spilled read-only tool result.
            offset: Characters to skip from the start, or the end when from_end is true.
            limit: Characters to return, from 1 to 4,000.
            from_end: Read a slice near the end instead of the start.
        """
        try:
            text = (await self.read(handle)).decode("utf-8", errors="replace")
        except FileNotFoundError:
            return "Stored result expired or is unavailable. Repeat the original read-only lookup."
        if from_end:
            end = max(0, len(text) - offset)
            start = max(0, end - limit)
        else:
            start = min(offset, len(text))
            end = min(start + limit, len(text))
        return f"[Characters {start}:{end} of {len(text)}]\n{text[start:end]}"

    async def load_tool_result(self, handle: str) -> Any:
        """Load a full stored result for sandbox processing, decoding JSON when possible.

        Args:
            handle: Exact handle from a saved read or Code Mode result.
        """
        payload = await asyncio.to_thread(self._read, handle, 8_000_000)
        text = payload.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    async def prune(self) -> int:
        return await asyncio.to_thread(self._prune)

    def _prune(self) -> int:
        # The same SQLite transaction excludes a newly accepted run from expiry.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = db.execute(
                """WITH latest AS (
                    SELECT session_id, type,
                        ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY seq DESC) AS rank
                    FROM events WHERE type IN (
                        'run.queued', 'run.started', 'run.waiting', 'run.completed', 'run.failed',
                        'run.stop_requested', 'run.cancelled'
                    )
                )
                DELETE FROM tool_results WHERE last_used < ? AND NOT EXISTS (
                    SELECT 1 FROM latest WHERE latest.session_id = tool_results.session_id
                    AND latest.rank = 1
                    AND latest.type IN (
                        'run.queued', 'run.started', 'run.waiting', 'run.stop_requested'
                    )
                ) RETURNING handle""",
                (time.time() - self.retention.total_seconds(),),
            ).fetchall()
        return len(deleted)


def build_output_limits(store: SqliteResultStore, read_tools: set[str]) -> ToolOutputLimits:
    limited = read_tools | {"read_memory", "search_memory", "load_tool_result"}

    def model_visible_result(ctx: RunContext[None], tool: ToolDefinition) -> bool:
        # Inner data stays whole; only the sandbox's public answer enters model context.
        return tool.name == "run_code" or (tool.name in limited and not inside_code_mode.get())

    return ToolOutputLimits(
        store=store,
        tool_filter=model_visible_result,
        bands=[
            Band(
                over=SPILL_CHARS,
                action=Spill(preview_chars=PREVIEW_CHARS, then=Truncate(max_chars=4_000)),
            )
        ],
    )
