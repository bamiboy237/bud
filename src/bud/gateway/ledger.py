"""SQLite ledger of model request outcomes, usage, and cost.

Ledger write errors are logged, so the ledger may not contain every request.
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
    billing TEXT NOT NULL DEFAULT 'api',
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
    """A request record. Missing usage or cost is distinct from zero."""

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
    billing: str = "api"


@dataclass(frozen=True)
class CostRow:
    day: str
    model: str
    billing: str
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
            columns = {row[1] for row in db.execute("PRAGMA table_info(model_requests)")}
            if "billing" not in columns:
                db.execute(
                    "ALTER TABLE model_requests ADD COLUMN billing TEXT NOT NULL DEFAULT 'api'"
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def record(self, entry: LedgerEntry) -> None:
        """Log SQLite errors without failing the model request."""
        try:
            with self._connect() as db:
                db.execute(
                    """
                    INSERT INTO model_requests (
                        created_at, session_id, model, status, input_tokens, output_tokens,
                        cached_tokens, reasoning_tokens, latency_ms, cost_usd, error, billing
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        entry.billing,
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

    def session_models(self, session_id: str) -> list[dict[str, str]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT model, billing FROM model_requests WHERE session_id = ? "
                "ORDER BY model, billing",
                (session_id,),
            ).fetchall()
        return [{"model": model, "billing": billing} for model, billing in rows]

    def costs_by_day_and_model(self) -> list[CostRow]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT substr(created_at, 1, 10) AS day,
                       model,
                       billing,
                       COUNT(*),
                       SUM(status != 'ok'),
                       SUM(billing = 'api' AND cost_usd IS NULL),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(cost_usd), 0)
                FROM model_requests
                GROUP BY day, model, billing
                ORDER BY day DESC, model, billing
                """
            ).fetchall()
        return [CostRow(*row) for row in rows]
