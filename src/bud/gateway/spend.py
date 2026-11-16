"""A persistent SQLite counter for pydantic-ai-harness's SpendLimits.

Each batch commits atomically. Response tokens prevent double counting when recovery
reaches the store without its durable journal. Unknown or expired keys read as zero.
"""

import asyncio
import sqlite3
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic_ai.messages import ModelResponse
from pydantic_ai_harness import SpendLimits
from pydantic_ai_harness.spend import Budget, SpendEntry, Spent

from bud.gateway.registry import Registry

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
    key TEXT PRIMARY KEY,
    usd TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    requests INTEGER NOT NULL,
    unpriced INTEGER NOT NULL,
    expires REAL
);
CREATE TABLE IF NOT EXISTS spend_applied (
    key TEXT NOT NULL,
    token TEXT NOT NULL,
    expires REAL,
    PRIMARY KEY (key, token)
);
"""


class SqliteSpendStore:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.clock = clock
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _read(db: sqlite3.Connection, keys: Sequence[str], now: float) -> dict[str, Spent]:
        totals = dict.fromkeys(keys, Spent())
        for key, usd, tokens, requests, unpriced in db.execute(
            f"SELECT key, usd, tokens, requests, unpriced FROM spend WHERE key IN "
            f"({','.join('?' * len(keys))}) AND (expires IS NULL OR expires > ?)",
            [*keys, now],
        ):
            totals[key] = Spent(
                usd=Decimal(usd), tokens=tokens, requests=requests, unpriced_requests=unpriced
            )
        return totals

    async def get_many(self, keys: Sequence[str]) -> Mapping[str, Spent]:
        return await asyncio.to_thread(self._get_many, keys)

    def _get_many(self, keys: Sequence[str]) -> Mapping[str, Spent]:
        with self._connect() as db:
            return self._read(db, keys, self.clock().timestamp())

    async def add_many(self, entries: Sequence[SpendEntry]) -> Mapping[str, Spent]:
        return await asyncio.to_thread(self._add_many, entries)

    def _add_many(self, entries: Sequence[SpendEntry]) -> Mapping[str, Spent]:
        now = self.clock().timestamp()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM spend WHERE expires IS NOT NULL AND expires <= ?", [now])
                db.execute(
                    "DELETE FROM spend_applied WHERE expires IS NOT NULL AND expires <= ?", [now]
                )
                for entry in entries:
                    expires = now + entry.ttl.total_seconds() if entry.ttl is not None else None
                    if entry.token is not None:
                        claimed = db.execute(
                            "INSERT OR IGNORE INTO spend_applied VALUES (?, ?, ?)",
                            [entry.key, entry.token, expires],
                        ).rowcount
                        if not claimed:
                            continue  # This response already counted against this key.
                    current = self._read(db, [entry.key], now)[entry.key]
                    db.execute(
                        "INSERT OR REPLACE INTO spend VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            entry.key,
                            str(current.usd + entry.usd),
                            current.tokens + entry.tokens,
                            current.requests + entry.requests,
                            current.unpriced_requests + entry.unpriced,
                            expires,
                        ],
                    )
                totals = self._read(db, [entry.key for entry in entries], now)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return totals


def build_spend_limits(path: Path, registry: Registry, daily_usd: Decimal) -> SpendLimits | None:
    """A daily USD cap over every model response, priced from bud's model registry.

    None when the cap is zero (off). A response from a model the registry has no price
    for counts its tokens and requests but no dollars.
    """
    if daily_usd <= 0:
        return None
    specs = {spec.id: spec for spec in registry.models}

    def price(response: ModelResponse) -> Decimal | None:
        # Gateway metadata identifies the route, including custom endpoints with the same name.
        route = (response.metadata or {}).get("bud_model_id")
        spec = specs.get(route) if isinstance(route, str) else None
        cost = spec.cost(response.usage) if spec is not None else None
        return Decimal(str(cost)) if cost is not None else None

    return SpendLimits(
        budgets=[Budget(usd=daily_usd, window="day", warn_at=0.8, name="daily")],
        store=SqliteSpendStore(path),
        price=price,
    )
