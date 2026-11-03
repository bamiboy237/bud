"""SQLite record of every model request: tokens, latency, status, and cost.
`Ledger.costs_by_day_and_model` aggregates these rows for `bud costs`.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_requests (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    session_id TEXT,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'incomplete')),
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    reasoning_tokens INTEGER,
    latency_ms REAL NOT NULL,
    cost_usd REAL,
    error TEXT
);
"""


@dataclass(frozen=True)
class LedgerEntry:
    """One model request. None means the provider never told us, which is different from 0."""

    model: str
    status: str
    latency_ms: float
    session_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class CostRow:
    day: str
    model: str
    requests: int
    failed: int
    unknown_cost: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def record(self, entry: LedgerEntry) -> None:
        """A ledger fault is logged and never fails a request already paid for."""
        try:
            with self._connect() as db:
                db.execute(
                    """
                    INSERT INTO model_requests (
                        created_at, session_id, model, status, input_tokens, output_tokens,
                        cached_tokens, reasoning_tokens, latency_ms, cost_usd, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.now(UTC).isoformat(),
                        entry.session_id,
                        entry.model,
                        entry.status,
                        entry.input_tokens,
                        entry.output_tokens,
                        entry.cached_tokens,
                        entry.reasoning_tokens,
                        entry.latency_ms,
                        entry.cost_usd,
                        entry.error,
                    ),
                )
        except sqlite3.Error:
            logger.exception("could not write ledger entry for %s", entry.model)

    def session_totals(self, session_id: str) -> tuple[float, int]:
        """USD and tokens for one session."""
        with self._connect() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0),"
                " COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)"
                " FROM model_requests WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row[0], row[1]

    def costs_by_day_and_model(self) -> list[CostRow]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT substr(created_at, 1, 10) AS day,
                       model,
                       COUNT(*),
                       SUM(status != 'ok'),
                       SUM(cost_usd IS NULL),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(cost_usd), 0)
                FROM model_requests
                GROUP BY day, model
                ORDER BY day DESC, model
                """
            ).fetchall()
        return [CostRow(*row) for row in rows]
