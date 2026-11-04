"""Persistent, atomic spend counters for local harness workers."""

import asyncio
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path

from pydantic_ai_harness.spend import SpendEntry, Spent


class SqliteSpendStore:
    """Store batches in one local SQLite file, shared by threads and processes.

    Tokens are remembered for the lifetime of their key's window. A key with no
    expiry retains its tokens indefinitely. Duplicate entries do not refresh TTL.
    The optional clock must return timezone-aware datetimes.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if str(path) == ":memory:":
            raise ValueError("Spend storage requires a persistent SQLite file")
        self.path = Path(path).expanduser().resolve()
        self.clock = clock if clock is not None else lambda: datetime.now(UTC)

    async def get_many(self, keys: Sequence[str]) -> Mapping[str, Spent]:
        return await asyncio.to_thread(self._get_many, tuple(keys))

    async def add_many(self, entries: Sequence[SpendEntry]) -> Mapping[str, Spent]:
        return await asyncio.to_thread(self._add_many, tuple(entries))

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute(
                """CREATE TABLE IF NOT EXISTS spend_windows (
                    key TEXT PRIMARY KEY,
                    usd TEXT NOT NULL,
                    tokens INTEGER NOT NULL,
                    requests INTEGER NOT NULL,
                    unpriced_requests INTEGER NOT NULL,
                    expires_at TEXT
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS spend_tokens (
                    key TEXT NOT NULL REFERENCES spend_windows(key) ON DELETE CASCADE,
                    token TEXT NOT NULL,
                    PRIMARY KEY (key, token)
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS spend_windows_expiry ON spend_windows(expires_at)"
            )
            with db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db

    def _now(self) -> datetime:
        now = self.clock()
        if now.utcoffset() is None:
            raise ValueError("Spend storage clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _read(db: sqlite3.Connection, key: str, now: str) -> Spent:
        row = db.execute(
            """SELECT usd, tokens, requests, unpriced_requests FROM spend_windows
               WHERE key = ? AND (expires_at IS NULL OR expires_at > ?)""",
            (key, now),
        ).fetchone()
        if row is None:
            return Spent()
        return Spent(usd=Decimal(row[0]), tokens=row[1], requests=row[2], unpriced_requests=row[3])

    def _get_many(self, keys: Sequence[str]) -> Mapping[str, Spent]:
        with self._transaction(write=False) as db:
            now = self._now().isoformat(timespec="microseconds")
            return {key: self._read(db, key, now) for key in keys}

    def _add_many(self, entries: Sequence[SpendEntry]) -> Mapping[str, Spent]:
        with self._transaction(write=True) as db:
            now = self._now()
            timestamp = now.isoformat(timespec="microseconds")
            db.execute("DELETE FROM spend_windows WHERE expires_at <= ?", (timestamp,))
            totals: dict[str, Spent] = {}
            for entry in entries:
                current = totals.get(entry.key)
                if current is None:
                    current = self._read(db, entry.key, timestamp)
                if (
                    entry.token is not None
                    and db.execute(
                        "SELECT 1 FROM spend_tokens WHERE key = ? AND token = ?",
                        (entry.key, entry.token),
                    ).fetchone()
                ):
                    totals[entry.key] = current
                    continue
                if not entry.usd.is_finite():
                    raise ValueError("Spend amounts must be finite")
                # Align both coefficients and allow a carry without rounding money.
                with localcontext() as context:
                    context.prec = (
                        max(current.usd.adjusted(), entry.usd.adjusted())
                        - min(
                            int(current.usd.as_tuple().exponent),
                            int(entry.usd.as_tuple().exponent),
                        )
                        + 2
                    )
                    usd = current.usd + entry.usd
                updated = Spent(
                    usd=usd,
                    tokens=current.tokens + entry.tokens,
                    requests=current.requests + entry.requests,
                    unpriced_requests=current.unpriced_requests + entry.unpriced,
                )
                expires_at = (
                    None
                    if entry.ttl is None
                    else (now + entry.ttl).isoformat(timespec="microseconds")
                )
                db.execute(
                    """INSERT INTO spend_windows
                       (key, usd, tokens, requests, unpriced_requests, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                           usd = excluded.usd, tokens = excluded.tokens,
                           requests = excluded.requests,
                           unpriced_requests = excluded.unpriced_requests,
                           expires_at = excluded.expires_at""",
                    (
                        entry.key,
                        str(updated.usd),
                        updated.tokens,
                        updated.requests,
                        updated.unpriced_requests,
                        expires_at,
                    ),
                )
                if entry.token is not None:
                    db.execute(
                        "INSERT INTO spend_tokens (key, token) VALUES (?, ?)",
                        (entry.key, entry.token),
                    )
                totals[entry.key] = updated
            return totals
