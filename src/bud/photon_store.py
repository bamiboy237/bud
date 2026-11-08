"""Durable v2 delivery claims and cursors; the core owns conversation history."""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path


class TransportError(Exception):
    """An operational error without private content or credentials."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def fingerprint(value: dict) -> str:
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def proposal_hash(value: dict) -> str:
    return fingerprint({key: value[key] for key in ("approval_id", "tool", "args")})


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    _private(path, directory=True)


def _private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    if (
        info.st_uid != os.getuid()
        or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
        or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
        or (not directory and info.st_nlink != 1)
    ):
        raise TransportError("Photon state must use owned private files and directories.")


SCHEMA = [
    "CREATE TABLE state(name TEXT PRIMARY KEY,value TEXT NOT NULL)",
    """CREATE TABLE routes(space TEXT PRIMARY KEY,agent TEXT NOT NULL,parent TEXT NOT NULL,
       session TEXT NOT NULL,core_seq INTEGER NOT NULL DEFAULT 0,
       ready INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE inbox(id TEXT PRIMARY KEY,digest TEXT NOT NULL,state TEXT NOT NULL,
       reply_to TEXT)""",
    """CREATE TABLE outbox(id TEXT PRIMARY KEY,group_id TEXT NOT NULL,space TEXT NOT NULL,
       parent TEXT NOT NULL,body TEXT,state TEXT NOT NULL DEFAULT 'queued',
       receipt TEXT,delivery_status TEXT,approval_id TEXT,proposal_hash TEXT)""",
]


class TransportStore:
    def __init__(self, directory: Path) -> None:
        private_directory(directory)
        fd = os.open(
            directory / "adapter.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        self.lock = os.fdopen(fd, "r+")
        try:
            _private(directory / "adapter.lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path = directory / "adapter.db"
            dbfd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            os.close(dbfd)
            _private(self.path)
            with self.connect() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                tables = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if version not in (0, 1, 2, 3):
                    raise TransportError("Unsupported Photon state; it was not reset.")
                if tables or version:
                    required = {
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
                    if version >= 2:
                        required["inbox"].add("reply_to")
                        required["outbox"].update(("approval_id", "proposal_hash"))
                    if not required.keys() <= tables:
                        raise TransportError("Incomplete Photon state; it was not reset.")
                    for table, columns in required.items():
                        actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                        if not columns <= actual:
                            raise TransportError("Incomplete Photon schema; it was not reset.")
                    cursor = db.execute("SELECT value FROM state WHERE name='cursor'").fetchone()
                    self._cursor(cursor[0] if cursor else None)
                    if (
                        db.execute(
                            "SELECT 1 FROM inbox WHERE state NOT IN ('dispatching','done')"
                        ).fetchone()
                        or db.execute("""SELECT 1 FROM outbox WHERE
                            state NOT IN ('queued','sending','accepted','unknown','blocked')
                            """).fetchone()
                    ):
                        raise TransportError("Invalid Photon claims; they were not reset.")
                db.execute("BEGIN IMMEDIATE")
                if not tables:
                    for statement in SCHEMA:
                        db.execute(statement)
                    db.execute("INSERT INTO state VALUES ('cursor','0')")
                elif version < 2:
                    # Additive migration preserves the original v2 cursor, routes and claims.
                    db.execute("ALTER TABLE inbox ADD COLUMN reply_to TEXT")
                    db.execute("ALTER TABLE outbox ADD COLUMN approval_id TEXT")
                    db.execute("ALTER TABLE outbox ADD COLUMN proposal_hash TEXT")
                if tables and version < 3:
                    # Old queued previews promised native yes/no. Do not send them now.
                    # Core approvals stay intact; /status presents the current exact commands.
                    db.execute("""UPDATE outbox SET state='blocked',body=NULL
                        WHERE state='queued' AND group_id IN
                        (SELECT group_id FROM outbox WHERE approval_id IS NOT NULL)""")
                db.execute("PRAGMA user_version=3")
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

    @staticmethod
    def _cursor(raw: str | None) -> int:
        if raw is None or not re.fullmatch(r"0|[1-9][0-9]{0,15}", raw):
            raise TransportError("Invalid Photon cursor; it was not reset.")
        value = int(raw)
        if value > 9007199254740991:
            raise TransportError("Invalid Photon cursor; it was not reset.")
        return value

    @property
    def cursor(self) -> int:
        return self._cursor(self.get("cursor"))

    def routes(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM routes ORDER BY rowid")]

    def route(self, space: str, agent: str, parent: str, session: str, initial_seq: int) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM routes WHERE space=?", (space,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO routes(space,agent,parent,session,core_seq) VALUES (?,?,?,?,?)",
                    (space, agent, parent, session, initial_seq),
                )
            elif row["agent"] != agent or row["session"] != session:
                raise TransportError("The conversation binding changed; it was not replaced.")
            else:
                db.execute("UPDATE routes SET parent=? WHERE space=?", (parent, space))
            return dict(db.execute("SELECT * FROM routes WHERE space=?", (space,)).fetchone())

    def ready(self, space: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE routes SET ready=1 WHERE space=?", (space,))

    def accept(self, identity: str, payload_hash: str, *, parent: str | None = None) -> str:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM inbox WHERE id=?", (identity,)).fetchone()
            if row:
                if row["digest"] != payload_hash or (
                    row["reply_to"] is not None and row["reply_to"] != (parent or "")
                ):
                    raise TransportError("An incoming message identity changed.")
                return row["state"]
            db.execute(
                "INSERT INTO inbox VALUES (?,?,'dispatching',?)",
                (identity, payload_hash, parent or ""),
            )
            return "new"

    def complete(self, identity: str, cursor: int) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE inbox SET state='done' WHERE id=?", (identity,))
            db.execute("INSERT OR REPLACE INTO state VALUES ('cursor',?)", (str(cursor),))

    @staticmethod
    def _enqueue(db, route, identity, text, persist, approval=None) -> list[tuple[str, str]]:
        chunks = [text[i : i + 1800] for i in range(0, len(text), 1800)]
        if approval and len(text) > 1800:
            # Keep exact decision commands together and behind the complete details.
            details, commands = text.rsplit("\n\n", 1)
            chunks = [details[i : i + 1800] for i in range(0, len(details), 1800)] + [commands]
        result = []
        for index, body in enumerate(chunks):
            key = digest(f"{identity}:{index}")
            row = db.execute(
                """INSERT OR IGNORE INTO outbox
                   (id,group_id,space,parent,body,approval_id,proposal_hash)
                   VALUES (?,?,?,?,?,?,?) RETURNING id""",
                (
                    key,
                    identity,
                    route["space"],
                    "",
                    body if persist else None,
                    approval["approval_id"] if approval and index == len(chunks) - 1 else None,
                    proposal_hash(approval) if approval else None,
                ),
            ).fetchone()
            if row:
                result.append((key, body))
        return result

    def enqueue(
        self,
        route: dict,
        identity: str,
        text: str,
        *,
        persist: bool = True,
        approval: dict | None = None,
    ) -> list:
        with self.connect() as db:
            return self._enqueue(db, route, identity, text, persist, approval)

    def project(self, route: dict, seq: int, text: str | None, *, approval=None) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT core_seq FROM routes WHERE space=?", (route["space"],)
            ).fetchone()
            if row is None or row[0] >= seq:
                return
            if text:
                self._enqueue(db, route, f"core:{route['session']}:{seq}", text, True, approval)
            db.execute("UPDATE routes SET core_seq=? WHERE space=?", (seq, route["space"]))

    @staticmethod
    def _block_partial(db) -> None:
        db.execute("""UPDATE outbox SET state='blocked',body=NULL WHERE state='queued'
            AND group_id IN (SELECT group_id FROM outbox
            WHERE state IN ('unknown','blocked') OR delivery_status='failed')""")

    def claim(self) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._block_partial(db)
            row = db.execute("""SELECT o.*,r.agent FROM outbox o
                JOIN routes r ON o.space=r.space WHERE o.state='queued' AND
                (o.approval_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM outbox p WHERE p.group_id=o.group_id AND p.rowid<o.rowid
                    AND (p.state!='accepted' OR COALESCE(p.delivery_status,'')!='delivered')
                )) ORDER BY o.rowid LIMIT 1""").fetchone()
            if row:
                db.execute("UPDATE outbox SET state='sending' WHERE id=?", (row["id"],))
            return dict(row) if row else None

    def sent(self, identity: str, receipt: str | None, status: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE outbox SET state=?,body=NULL,receipt=?,delivery_status=? WHERE id=?",
                ("unknown" if status == "unknown" else "accepted", receipt, status, identity),
            )
            self._block_partial(db)

    def waiting_deliveries(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute("""SELECT o.* FROM outbox o
                    WHERE o.state='accepted' AND o.receipt IS NOT NULL
                    AND COALESCE(o.delivery_status,'') NOT IN ('delivered','failed')
                    AND EXISTS (SELECT 1 FROM outbox q WHERE q.group_id=o.group_id
                        AND q.state='queued' AND q.approval_id IS NOT NULL)
                    ORDER BY o.rowid LIMIT 3""")
            ]

    def block(self, identity: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE outbox SET state='blocked',body=NULL WHERE id=?", (identity,))
            self._block_partial(db)

    def delivery(self, receipt: str, status: str) -> None:
        with self.connect() as db:
            # Late dispatch notifications must not erase delivery/failure evidence.
            db.execute(
                """UPDATE outbox SET delivery_status=? WHERE receipt=?
                   AND COALESCE(delivery_status,'') NOT IN ('delivered','failed')""",
                (status, receipt),
            )
            self._block_partial(db)
