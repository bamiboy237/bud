"""Local single-owner account generations; no credentials or remote operations."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

TOOLKITS = ("gmail", "googlecalendar")


class ConnectionError(Exception):
    """An integration error safe to show in the CLI and session log."""


@dataclass(frozen=True)
class AccountBinding:
    owner: str
    generation: int
    active_accounts: tuple[tuple[str, str], ...]

    @property
    def accounts(self) -> dict[str, str]:
        return dict(self.active_accounts)

    @property
    def scope(self) -> str:
        value = json.dumps([self.owner, self.generation, self.active_accounts])
        return sha256(value.encode()).hexdigest()[:32]


class BindingStore:
    """Serialize intent, verified acceptance and retirement in real SQLite transactions."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ConnectionError(
                    "Unsupported account binding schema. Restore compatible state."
                )
            # Never repair modern state by resetting its generation: that can
            # make a retired account scope and its approvals current again.
            modern = version == 1 or bool(existing & {"binding_state", "pending_accounts"})
            required = {"owner", "accounts", "binding_state", "pending_accounts"}
            if modern and not required <= existing:
                raise ConnectionError("Incomplete account binding. Restore the original state.")
            db.execute("CREATE TABLE IF NOT EXISTS owner (id TEXT PRIMARY KEY)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS accounts "
                "(toolkit TEXT PRIMARY KEY, account_id TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS binding_state "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), "
                "generation INTEGER NOT NULL CHECK (generation > 0))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending_accounts "
                "(toolkit TEXT PRIMARY KEY, account_id TEXT)"
            )
            if "owner" not in existing:
                if db.execute("SELECT count(*) FROM accounts").fetchone()[0]:
                    raise ConnectionError(
                        "Missing owner for retained accounts. Restore owner state."
                    )
                db.execute("INSERT INTO owner VALUES (?)", (uuid4().hex,))
            if "binding_state" not in existing:
                db.execute("INSERT INTO binding_state VALUES (1, 1)")
            self._read(db)
            db.execute("PRAGMA user_version = 1")
        path.chmod(0o600)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    @staticmethod
    def _read(db: sqlite3.Connection) -> tuple[AccountBinding, dict[str, str | None]]:
        owners = db.execute("SELECT id FROM owner").fetchall()
        if len(owners) != 1 or not isinstance(owners[0][0], str) or not owners[0][0].strip():
            raise ConnectionError("Invalid local owner binding. Restore the original owner state.")
        state = db.execute("SELECT generation FROM binding_state WHERE id = 1").fetchone()
        if state is None:
            raise ConnectionError("Missing account generation. Restore the original binding state.")
        generation = state[0]
        if type(generation) is not int or generation <= 0:
            raise ConnectionError("Invalid account generation. Restore the original binding state.")
        accounts = tuple(db.execute("SELECT toolkit, account_id FROM accounts ORDER BY toolkit"))
        pending = dict(db.execute("SELECT toolkit, account_id FROM pending_accounts"))
        if any(
            toolkit not in TOOLKITS or not isinstance(account, str) or not account.strip()
            for toolkit, account in accounts
        ):
            raise ConnectionError(
                "Invalid local account mapping. Restore the original owner state."
            )
        if set(pending) - set(TOOLKITS) or any(
            account is not None and (not isinstance(account, str) or not account.strip())
            for account in pending.values()
        ):
            raise ConnectionError("Invalid pending connection state.")
        return AccountBinding(owners[0][0], generation, accounts), pending

    def snapshot(self) -> tuple[AccountBinding, dict[str, str | None]]:
        with self._db() as db:
            db.execute("BEGIN")
            return self._read(db)

    def begin_connect(self, toolkit: str) -> AccountBinding:
        if toolkit not in TOOLKITS:
            raise ConnectionError("Unsupported toolkit. Use gmail or calendar.")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._read(db)
            # Fence even an explicit reconnect to exactly the same remote ID.
            db.execute("UPDATE binding_state SET generation = generation + 1 WHERE id = 1")
            db.execute("DELETE FROM accounts WHERE toolkit = ?", (toolkit,))
            db.execute("INSERT OR REPLACE INTO pending_accounts VALUES (?, NULL)", (toolkit,))
            return self._read(db)[0]

    def save_pending(self, expected: AccountBinding, toolkit: str, account_id: str) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current, pending = self._read(db)
            if current != expected or toolkit not in pending or pending[toolkit] is not None:
                raise ConnectionError(
                    "Connection changed during sign-in. Check /connections before trying again."
                )
            if not account_id:
                raise ConnectionError("Composio did not return a connection reference.")
            db.execute(
                "UPDATE pending_accounts SET account_id = ? WHERE toolkit = ?",
                (account_id, toolkit),
            )

    def accept_verified(
        self,
        expected: AccountBinding,
        pending: dict[str, str | None],
        active: dict[str, str],
        remaining: dict[str, str | None],
    ) -> AccountBinding:
        """Compare-and-swap a complete remotely verified snapshot, never a stale result."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._read(db) != (expected, pending):
                raise ConnectionError(
                    "Connections changed during verification. Check /connections."
                )
            if expected.accounts != active or pending != remaining:
                db.execute("UPDATE binding_state SET generation = generation + 1 WHERE id = 1")
                db.execute("DELETE FROM accounts")
                db.executemany("INSERT INTO accounts VALUES (?, ?)", sorted(active.items()))
                db.execute("DELETE FROM pending_accounts")
                db.executemany("INSERT INTO pending_accounts VALUES (?, ?)", remaining.items())
            return self._read(db)[0]

    def retire(self, expected: AccountBinding, *, disconnected: str | None = None) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._read(db)[0] == expected:
                db.execute("UPDATE binding_state SET generation = generation + 1 WHERE id = 1")
                if disconnected is not None:
                    db.execute("DELETE FROM accounts WHERE toolkit = ?", (disconnected,))
