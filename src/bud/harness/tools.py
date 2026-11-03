"""Tools the agent can call.
`send_email` is bud's first write action. It writes to a simulated outbox table, so no real
mail leaves the machine. Whether a tool may run is decided in `actions.py`.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai import RunContext, Tool

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    approval_id TEXT PRIMARY KEY,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
"""


def current_time() -> str:
    """Return the current UTC date and time."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class Outbox:
    def __init__(self, path: Path) -> None:
        self.path = path
        with sqlite3.connect(path) as db:
            db.executescript(OUTBOX_SCHEMA)

    def send(self, approval_id: str, recipient: str, subject: str, body: str) -> bool:
        """The approval ID is the primary key, so a replay can't send a second copy."""
        with sqlite3.connect(self.path, timeout=5) as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO outbox VALUES (?, ?, ?, ?, ?)",
                (approval_id, recipient, subject, body, datetime.now(UTC).isoformat()),
            )
        return cursor.rowcount == 1

    def rows(self) -> list[tuple[str, str, str, str]]:
        with sqlite3.connect(self.path, timeout=5) as db:
            return db.execute(
                "SELECT sent_at, approval_id, recipient, subject FROM outbox ORDER BY sent_at"
            ).fetchall()


def build_tools(outbox: Outbox) -> list[Tool]:
    def send_email(ctx: RunContext[None], to: str, subject: str, body: str) -> str:
        """Send an email to one recipient. The user must approve it first.

        Args:
            to: Recipient email address.
            subject: Subject line.
            body: Plain text body.
        """
        approval_id = (ctx.tool_call_metadata or {})["approval_id"]
        if outbox.send(approval_id, to, subject, body):
            return f"Sent to {to}."
        return f"Already sent to {to}; nothing new was sent."

    return [Tool(current_time), Tool(send_email)]
