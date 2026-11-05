"""Hosted sign-in and local account references. OAuth tokens stay with Composio."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from composio import SESSION_PRESET_DIRECT_TOOLS, Composio

from bud.config import Settings

TOOLKITS = ("gmail", "googlecalendar")


class ConnectionError(Exception):
    """An integration error safe to show in the CLI and session log."""


@contextmanager
def safe_errors(message: str) -> Iterator[None]:
    try:
        yield
    except ConnectionError:
        raise
    except Exception as exc:
        if getattr(exc, "status_code", None) == 401:
            message = "Composio rejected the API key. Set a valid COMPOSIO_API_KEY in .env."
        # SDK exceptions can contain request headers or credential-bearing URLs.
        raise ConnectionError(message) from None


class Connections:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.folder = settings.data_dir / "composio"
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.folder / "connections.db"
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS owner (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS accounts (
                    toolkit TEXT PRIMARY KEY, account_id TEXT NOT NULL
                );
                """
            )
            db.execute(
                "INSERT INTO owner SELECT ? WHERE NOT EXISTS (SELECT 1 FROM owner)",
                (uuid4().hex,),
            )
            self.user_id = db.execute("SELECT id FROM owner").fetchone()[0]
        self.path.chmod(0o600)

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def client(self) -> Composio:
        key = self.settings.composio_api_key
        if key is None or not key.get_secret_value().strip():
            raise ConnectionError("Set COMPOSIO_API_KEY in .env before connecting an account.")
        return Composio(
            api_key=key.get_secret_value(), allow_tracking=False, max_retries=0, timeout=20
        )

    def accounts(self) -> dict[str, str]:
        with self._db() as db:
            return dict(db.execute("SELECT toolkit, account_id FROM accounts"))

    def connect(self, toolkit: str) -> str:
        if toolkit not in TOOLKITS:
            raise ValueError("Unsupported toolkit")
        with safe_errors("Could not start Composio sign-in. Check your key and connection."):
            session = self.client().create(
                user_id=self.user_id,
                toolkits=[toolkit],
                session_preset=SESSION_PRESET_DIRECT_TOOLS,
                manage_connections=False,
            )
            request = session.authorize(toolkit)
            if not request.redirect_url:
                raise ConnectionError("Composio did not return a sign-in link. Try again.")
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO accounts VALUES (?, ?)", (toolkit, request.id))
            return request.redirect_url

    def statuses(self) -> dict[str, str]:
        return self._statuses(self.accounts())

    def _statuses(self, accounts: dict[str, str]) -> dict[str, str]:
        statuses = dict.fromkeys(TOOLKITS, "NOT_CONNECTED")
        if not accounts:
            return statuses
        with safe_errors("Could not check Composio connections. Check your key and network."):
            client = self.client()
            for toolkit, account_id in accounts.items():
                account = client.connected_accounts.get(nanoid=account_id)
                if account.user_id != self.user_id or account.toolkit.slug != toolkit:
                    raise ConnectionError(
                        "Connection owner or toolkit changed. Reconnect the account."
                    )
                statuses[toolkit] = "DISABLED" if account.is_disabled else account.status
        return statuses

    def active_accounts(self) -> dict[str, str]:
        accounts = self.accounts()
        statuses = self._statuses(accounts)
        return {
            toolkit: account_id
            for toolkit, account_id in accounts.items()
            if statuses[toolkit] == "ACTIVE"
        }
