"""Durable transport metadata, not a second conversation history.

Core sessions remain authoritative. Incoming text is not stored here. Only message
digests, routing, cursors and pending delivery bodies are retained. A send is claimed
before network dispatch; interrupted/uncertain sends never silently retry.
"""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class TransportError(Exception):
    """Safe operational error; never includes a credential or private body."""


class TransportStore:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise TransportError("Photon state must be an owned 0700 directory, not a symlink.")
        fd = os.open(
            directory / "adapter.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        self.lock = os.fdopen(fd, "r+")
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise TransportError("Photon lock must be an owned private regular file.")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path = directory / "adapter.db"
            dbfd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(dbfd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise TransportError("Photon database must be an owned private regular file.")
            finally:
                os.close(dbfd)
            with self.connect() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise TransportError(
                        "Unsupported Photon state version; restore compatible state."
                    )
                tables = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                expected = {"state", "routes", "inbox", "outbox"}
                if (version or tables) and not expected.issubset(tables):
                    raise TransportError("Incomplete Photon state; it will not be reset.")
                if tables:
                    cursor = db.execute("SELECT value FROM state WHERE name='cursor'").fetchone()
                    if cursor is None:
                        raise TransportError("Missing Photon cursor; it will not be reset.")
                    self._cursor(cursor[0])
                    columns = {
                        "state": {"name", "value"},
                        "routes": {"space", "agent", "parent", "session", "core_seq", "ready"},
                        "inbox": {"id", "digest", "state"},
                        "outbox": {
                            "id",
                            "group_id",
                            "space",
                            "parent",
                            "body",
                            "state",
                            "receipt",
                            "delivery_status",
                        },
                    }
                    if version == 2:
                        columns["inbox"].add("reply_to")
                        columns["outbox"].update(("approval_id", "proposal_hash"))
                    for name, required in columns.items():
                        actual = {row[1] for row in db.execute(f"PRAGMA table_info({name})")}
                        if not required.issubset(actual):
                            raise TransportError("Incomplete Photon schema; restore saved state.")
                    if (
                        db.execute(
                            "SELECT 1 FROM inbox WHERE state NOT IN ('dispatching','done') LIMIT 1"
                        ).fetchone()
                        or db.execute(
                            """SELECT 1 FROM outbox WHERE state NOT IN
                           ('queued','sending','accepted','unknown','blocked') LIMIT 1"""
                        ).fetchone()
                    ):
                        raise TransportError("Invalid Photon dispatch state; it was not reset.")
                db.execute("BEGIN IMMEDIATE")
                if not tables:
                    db.execute("CREATE TABLE state (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    db.execute("""CREATE TABLE routes (
                        space TEXT PRIMARY KEY, agent TEXT NOT NULL, parent TEXT NOT NULL,
                        session TEXT NOT NULL, core_seq INTEGER NOT NULL DEFAULT 0,
                        ready INTEGER NOT NULL DEFAULT 0
                    )""")
                    db.execute("""CREATE TABLE inbox (
                        id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL,
                        reply_to TEXT
                    )""")
                    db.execute("""CREATE TABLE outbox (
                        id TEXT PRIMARY KEY, group_id TEXT NOT NULL, space TEXT NOT NULL,
                        parent TEXT NOT NULL, body TEXT, state TEXT NOT NULL DEFAULT 'queued',
                        receipt TEXT, delivery_status TEXT, approval_id TEXT, proposal_hash TEXT
                    )""")
                    db.execute("INSERT INTO state VALUES ('cursor','0')")
                elif version < 2:
                    # Old previews never acquire permission-bearing reply references.
                    db.execute("ALTER TABLE inbox ADD COLUMN reply_to TEXT")
                    db.execute("ALTER TABLE outbox ADD COLUMN approval_id TEXT")
                    db.execute("ALTER TABLE outbox ADD COLUMN proposal_hash TEXT")
                db.execute("PRAGMA user_version=2")
                # A process may have died after submitting the send. Keep its
                # receipt/claim, do not turn it back into a retryable job.
                db.execute("UPDATE outbox SET state='unknown',body=NULL WHERE state='sending'")
                self._block_partial(db)
        except BaseException:
            self.lock.close()
            raise

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self) -> None:
        self.lock.close()

    def get(self, name: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE name=?", (name,)).fetchone()
            return row[0] if row else None

    def set(self, name: str, value: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (name, value))

    @property
    def cursor(self) -> int:
        return self._cursor(self.get("cursor"))

    @staticmethod
    def _cursor(raw: str | None) -> int:
        if raw is None or not re.fullmatch(r"0|[1-9][0-9]{0,15}", raw):
            raise TransportError("Invalid Photon cursor; state was not reset.")
        value = int(raw)
        if not 0 <= value <= 9007199254740991:
            raise TransportError("Invalid Photon cursor; state was not reset.")
        return value

    def routes(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM routes ORDER BY rowid")]

    def route(
        self, space: str, agent: str, parent: str, session: str | None = None, initial_seq: int = 0
    ) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM routes WHERE space=?", (space,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO routes(space,agent,parent,session,core_seq) VALUES (?,?,?,?,?)",
                    (space, agent, parent, session or uuid4().hex, initial_seq),
                )
            elif row["agent"] != agent or (session and row["session"] != session):
                raise TransportError("Photon route changed; refusing to reuse its conversation.")
            else:
                db.execute("UPDATE routes SET parent=? WHERE space=?", (parent, space))
            return dict(db.execute("SELECT * FROM routes WHERE space=?", (space,)).fetchone())

    def accept(self, identity: str, fingerprint: str, *, parent: str | None = None) -> str:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM inbox WHERE id=?", (identity,)).fetchone()
            if row is not None:
                if row["digest"] != fingerprint:
                    raise TransportError("Photon reused a message ID with different content.")
                if row["reply_to"] is not None and row["reply_to"] != (parent or ""):
                    raise TransportError(
                        "Photon reused a message ID with a different reply target."
                    )
                return row["state"]
            db.execute(
                "INSERT INTO inbox(id,digest,state,reply_to) VALUES (?,?,'dispatching',?)",
                (identity, fingerprint, parent or ""),
            )
            return "new"

    def ready(self, space: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE routes SET ready=1 WHERE space=?", (space,))

    def complete(self, identity: str, cursor: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE inbox SET state='done' WHERE id=?", (identity,))
            db.execute("INSERT OR REPLACE INTO state VALUES ('cursor',?)", (str(cursor),))

    @staticmethod
    def _enqueue(
        db, route: dict, identity: str, text: str, persist: bool, approval: dict | None = None
    ) -> list[tuple[str, str]]:
        # Split complete output rather than discarding the tail. Approval command
        # lines follow the full arguments and therefore appear in the final part.
        chunks = [text[i : i + 1800] for i in range(0, len(text), 1800)]
        result = []
        for index, chunk in enumerate(chunks):
            key = digest(f"{identity}:{index}")
            body = f"({index + 1}/{len(chunks)}) {chunk}" if len(chunks) > 1 else chunk
            row = db.execute(
                """INSERT OR IGNORE INTO outbox(id,group_id,space,parent,body)
                   VALUES (?,?,?,?,?) RETURNING id""",
                (key, identity, route["space"], route["parent"], body if persist else None),
            ).fetchone()
            if row:
                result.append((key, body))
                if approval is not None and index == len(chunks) - 1:
                    db.execute(
                        "UPDATE outbox SET approval_id=?,proposal_hash=? WHERE id=?",
                        (approval["approval_id"], proposal_hash(approval), key),
                    )
        return result

    def enqueue(self, route: dict, identity: str, text: str, *, persist: bool = True) -> list:
        with self.connect() as db:
            return self._enqueue(db, route, identity, text, persist)

    def project(
        self, route: dict, seq: int, text: str | None, *, approval: dict | None = None
    ) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT core_seq FROM routes WHERE space=?", (route["space"],)
            ).fetchone()
            if row[0] >= seq:
                return
            if text:
                self._enqueue(db, route, f"core:{route['session']}:{seq}", text, True, approval)
            db.execute("UPDATE routes SET core_seq=? WHERE space=?", (seq, route["space"]))

    def approval_reference(self, space: str, receipt: str | None) -> dict | None:
        """Only the final, known preview part can identify an action—not arbitrary text."""
        if not receipt:
            return None
        with self.connect() as db:
            rows = db.execute(
                """SELECT o.*,r.session FROM outbox o JOIN routes r ON o.space=r.space
                   WHERE o.space=? AND o.receipt=? AND o.approval_id IS NOT NULL""",
                (space, receipt),
            ).fetchall()
            if len(rows) != 1:
                return None
            item = dict(rows[0])
            # A reply proves this final part arrived. Earlier parts still require
            # delivery evidence, not merely HTTP acceptance.
            incomplete = db.execute(
                """SELECT 1 FROM outbox WHERE group_id=? AND
                   (state != 'accepted' OR delivery_status='failed' OR
                    (id != ? AND COALESCE(delivery_status,'') != 'delivered')) LIMIT 1""",
                (item["group_id"], item["id"]),
            ).fetchone()
            item["complete"] = incomplete is None
            return item

    @staticmethod
    def _block_partial(db) -> None:
        db.execute(
            """UPDATE outbox SET state='blocked',body=NULL WHERE state='queued'
               AND group_id IN (SELECT group_id FROM outbox
                                WHERE state='unknown' OR delivery_status='failed')"""
        )

    def claim(self) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._block_partial(db)
            row = db.execute(
                """SELECT o.*,r.agent FROM outbox o JOIN routes r ON o.space=r.space
                   WHERE o.state='queued' ORDER BY o.rowid LIMIT 1"""
            ).fetchone()
            if row:
                db.execute("UPDATE outbox SET state='sending' WHERE id=?", (row["id"],))
            return dict(row) if row else None

    def sent(self, identity: str, receipt: str | None, status: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE outbox SET state=?,body=NULL,receipt=?,delivery_status=? WHERE id=?",
                ("unknown" if status == "unknown" else "accepted", receipt, status, identity),
            )

    def delivery(self, receipt: str, status: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE outbox SET delivery_status=? WHERE receipt=?", (status, receipt))

    def uncertain(self) -> int:
        with self.connect() as db:
            return db.execute(
                """SELECT count(*) FROM outbox WHERE state IN ('sending','unknown','blocked')
                   OR delivery_status='failed'"""
            ).fetchone()[0]


def fingerprint(message: dict) -> str:
    return digest(json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def proposal_hash(approval: dict) -> str:
    return fingerprint({key: approval[key] for key in ("approval_id", "tool", "args")})
