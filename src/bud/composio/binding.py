"""Local single-owner account generations; no credentials or remote operations."""

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

TOOLKITS = ("gmail", "googlecalendar")
# Any app Composio offers uses a slug of this shape. The two above are the ones bud ships
# guidance for; a discovered app is accepted by the same rules.
TOOLKIT_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def valid_toolkit(slug: str) -> bool:
    return bool(TOOLKIT_SLUG.fullmatch(slug))


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


@dataclass(frozen=True)
class Attempt:
    """One sign-in: the binding it started under, and a token only it holds, so a second
    sign-in for the same app cannot be mistaken for this one."""

    binding: AccountBinding
    token: str


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
            db.execute(
                "CREATE TABLE IF NOT EXISTS inactive_accounts "
                "(toolkit TEXT PRIMARY KEY, account_id TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS session_scopes "
                "(generation INTEGER PRIMARY KEY, scope TEXT NOT NULL)"
            )
            if "owner" not in existing:
                if db.execute("SELECT count(*) FROM accounts").fetchone()[0]:
                    raise ConnectionError(
                        "Missing owner for retained accounts. Restore owner state."
                    )
                db.execute("INSERT INTO owner VALUES (?)", (uuid4().hex,))
            if "binding_state" not in existing:
                db.execute("INSERT INTO binding_state VALUES (1, 1)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(pending_accounts)")}
            if "attempt" not in columns:
                db.execute("ALTER TABLE pending_accounts ADD COLUMN attempt TEXT")
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
            not valid_toolkit(toolkit) or not isinstance(account, str) or not account.strip()
            for toolkit, account in accounts
        ):
            raise ConnectionError(
                "Invalid local account mapping. Restore the original owner state."
            )
        if any(not valid_toolkit(toolkit) for toolkit in pending) or any(
            account is not None and (not isinstance(account, str) or not account.strip())
            for account in pending.values()
        ):
            raise ConnectionError("Invalid pending connection state.")
        return AccountBinding(owners[0][0], generation, accounts), pending

    def snapshot(self) -> tuple[AccountBinding, dict[str, str | None]]:
        with self._db() as db:
            db.execute("BEGIN")
            return self._read(db)

    @staticmethod
    def _inactive(db: sqlite3.Connection) -> dict[str, str]:
        return dict(db.execute("SELECT toolkit, account_id FROM inactive_accounts"))

    def inventory(self) -> tuple[AccountBinding, dict[str, str | None], dict[str, str]]:
        with self._db() as db:
            db.execute("BEGIN")
            binding, pending = self._read(db)
            return binding, pending, self._inactive(db)

    def session_scope(self, binding: AccountBinding) -> str:
        """The owner's one session directory, kept across account changes: a reconnect loads
        into the same chat and memory. A new generation reuses the latest directory; approvals
        given under an earlier account are denied when the account changes (bud.core.server)."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._read(db)[0] != binding:
                raise ConnectionError("Account binding changed before opening the session.")
            latest = db.execute(
                "SELECT scope FROM session_scopes ORDER BY generation DESC LIMIT 1"
            ).fetchone()
            db.execute(
                "INSERT OR IGNORE INTO session_scopes VALUES (?, ?)",
                (binding.generation, latest[0] if latest else binding.scope),
            )
            return db.execute(
                "SELECT scope FROM session_scopes WHERE generation = ?", (binding.generation,)
            ).fetchone()[0]

    def begin_connect(self, toolkit: str, *, replace_active: bool = True) -> Attempt:
        if not valid_toolkit(toolkit):
            raise ConnectionError("Unsupported toolkit name.")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current, _ = self._read(db)
            # Fence even an explicit reconnect to exactly the same remote ID. An app that was
            # only pending was never active, so no approval or tool can point at it: a fresh
            # link replaces the unfinished one without a fence, as Cancel does.
            if toolkit in current.accounts:
                if not replace_active:
                    raise ConnectionError("This app is already connected.")
                db.execute("UPDATE binding_state SET generation = generation + 1 WHERE id = 1")
            db.execute("DELETE FROM accounts WHERE toolkit = ?", (toolkit,))
            db.execute("DELETE FROM inactive_accounts WHERE toolkit = ?", (toolkit,))
            token = uuid4().hex
            db.execute(
                "INSERT OR REPLACE INTO pending_accounts (toolkit, account_id, attempt) "
                "VALUES (?, NULL, ?)",
                (toolkit, token),
            )
            return Attempt(self._read(db)[0], token)

    def save_pending(self, attempt: Attempt, toolkit: str, account_id: str) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current, pending = self._read(db)
            row = db.execute(
                "SELECT attempt FROM pending_accounts WHERE toolkit = ?", (toolkit,)
            ).fetchone()
            if (
                current != attempt.binding
                or toolkit not in pending
                or pending[toolkit] is not None
                or row is None
                or row[0] != attempt.token
            ):
                raise ConnectionError(
                    "Connection changed during sign-in. Check /connections before trying again."
                )
            if not account_id:
                raise ConnectionError("Composio did not return a connection reference.")
            db.execute(
                "UPDATE pending_accounts SET account_id = ? WHERE toolkit = ?",
                (account_id, toolkit),
            )

    def cancel_connect(self, toolkit: str) -> bool:
        """Stop tracking an unfinished sign-in without disconnecting an active account."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            removed = db.execute(
                "DELETE FROM pending_accounts WHERE toolkit = ?", (toolkit,)
            ).rowcount
            return removed == 1

    def disconnect(self, toolkit: str) -> bool:
        """Remove Bud's references and invalidate tools that held the active account."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("DELETE FROM accounts WHERE toolkit = ?", (toolkit,)).rowcount
            if active:
                db.execute("UPDATE binding_state SET generation = generation + 1 WHERE id = 1")
            pending = db.execute(
                "DELETE FROM pending_accounts WHERE toolkit = ?", (toolkit,)
            ).rowcount
            inactive = db.execute(
                "DELETE FROM inactive_accounts WHERE toolkit = ?", (toolkit,)
            ).rowcount
            return bool(active or pending or inactive)

    def accept_verified(
        self,
        expected: AccountBinding,
        pending: dict[str, str | None],
        active: dict[str, str],
        remaining: dict[str, str | None],
        *,
        inactive: dict[str, str] | None = None,
    ) -> AccountBinding:
        """Compare-and-swap a complete remotely verified snapshot, never a stale result."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._read(db) != (expected, pending) or (
                inactive is not None and self._inactive(db) != inactive
            ):
                raise ConnectionError(
                    "Connections changed during verification. Check /connections."
                )
            if expected.accounts != active or pending != remaining:
                removed = expected.accounts.items() - active.items()
                if removed:
                    db.execute("UPDATE binding_state SET generation = generation + 1 WHERE id = 1")
                    db.executemany(
                        "INSERT OR REPLACE INTO inactive_accounts VALUES (?, ?)", removed
                    )
                db.execute("DELETE FROM accounts")
                db.executemany("INSERT INTO accounts VALUES (?, ?)", sorted(active.items()))
                # Only remove rows: an unfinished sign-in keeps its attempt token.
                db.executemany(
                    "DELETE FROM pending_accounts WHERE toolkit = ?",
                    [(toolkit,) for toolkit in pending if toolkit not in remaining],
                )
            return self._read(db)[0]

    def retire(self, expected: AccountBinding, *, disconnected: str | None = None) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._read(db)[0] == expected:
                db.execute("UPDATE binding_state SET generation = generation + 1 WHERE id = 1")
                if disconnected is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO inactive_accounts "
                        "SELECT toolkit, account_id FROM accounts WHERE toolkit = ?",
                        (disconnected,),
                    )
                    db.execute("DELETE FROM accounts WHERE toolkit = ?", (disconnected,))
