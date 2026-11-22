"""SQLite ledger of model request outcomes, usage, and cost.

Ledger write errors are logged, so the ledger may not contain every request.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_requests (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    session_id TEXT,
    run_id TEXT,
    model TEXT NOT NULL,
    billing TEXT NOT NULL DEFAULT 'api',
    profile_id TEXT,
    profile_version TEXT,
    profile_digest TEXT,
    prompt_version TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'incomplete')),
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    reasoning_tokens INTEGER,
    latency_ms REAL NOT NULL,
    cost_usd REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS model_requests_by_session ON model_requests(session_id);
CREATE INDEX IF NOT EXISTS model_requests_by_created ON model_requests(created_at);
"""

_EXTRA_COLUMNS = {
    "billing": "TEXT NOT NULL DEFAULT 'api'",
    "run_id": "TEXT",
    "profile_id": "TEXT",
    "profile_version": "TEXT",
    "profile_digest": "TEXT",
    "prompt_version": "TEXT",
}


@dataclass(frozen=True)
class LedgerEntry:
    """A request record. Missing usage or cost is distinct from zero."""

    model: str
    status: str
    latency_ms: float
    session_id: str | None = None
    run_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    billing: str = "api"
    profile_id: str | None = None
    profile_version: str | None = None
    profile_digest: str | None = None
    prompt_version: str | None = None


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
            for column, definition in _EXTRA_COLUMNS.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE model_requests ADD COLUMN {column} {definition}")
            db.execute("CREATE INDEX IF NOT EXISTS model_requests_by_run ON model_requests(run_id)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    def record(self, entry: LedgerEntry) -> None:
        """Log SQLite errors without failing the model request."""
        try:
            with self._connect() as db:
                db.execute(
                    """
                    INSERT INTO model_requests (
                        created_at, session_id, run_id, model, status, input_tokens,
                        output_tokens, cached_tokens, reasoning_tokens, latency_ms, cost_usd,
                        error, billing, profile_id, profile_version, profile_digest,
                        prompt_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.now(UTC).isoformat(),
                        entry.session_id,
                        entry.run_id,
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
                        entry.profile_id,
                        entry.profile_version,
                        entry.profile_digest,
                        entry.prompt_version,
                    ),
                )
        except sqlite3.Error:
            logger.exception("could not write ledger entry for %s", entry.model)

    def run_requests(self, run_id: str) -> list[dict[str, Any]]:
        """All recorded model requests for one run, oldest first."""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT model, billing, status, input_tokens, output_tokens, cost_usd,
                       profile_id, profile_version, profile_digest, prompt_version, error
                FROM model_requests
                WHERE run_id = ?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "model": model,
                "billing": billing,
                "status": status,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "profile_id": profile_id,
                "profile_version": profile_version,
                "profile_digest": profile_digest,
                "prompt_version": prompt_version,
                "error": error,
            }
            for (
                model,
                billing,
                status,
                input_tokens,
                output_tokens,
                cost_usd,
                profile_id,
                profile_version,
                profile_digest,
                prompt_version,
                error,
            ) in rows
        ]

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

    def session_profile(self, session_id: str) -> tuple[int, float]:
        """Model requests and seconds spent waiting on them, for one session."""
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(latency_ms), 0) FROM model_requests"
                " WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row[0], row[1] / 1000

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
