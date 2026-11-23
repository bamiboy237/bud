"""Durable, owner-authorized file work across paired Bud desktops."""

import hashlib
import secrets
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bud.core.browser_delivery import BrowserForm, BrowserUpdate
from bud.core.hub import Hub
from bud.core.projection import Attachment
from bud.workspace import ATTACHMENT_BYTES, read_workspace_bytes

WORKER_PATH = "/desktop-worker/work"
Id = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class PairDesktop(BaseModel):
    label: str = Field(min_length=1, max_length=48, pattern=r"^[\w .()-]+$")


class Dispatch(BaseModel):
    job_id: Id
    file: Attachment
    desktops: list[Id] = Field(min_length=1, max_length=2)
    browser: BrowserForm | None = None


class BrowserTarget(BaseModel):
    desktop: Id


class Receipt(BaseModel):
    step_id: Id
    sha256: Digest
    platform: Literal["darwin", "linux", "win32"]


class DesktopHandoffs:
    def __init__(self, db_path: Path, workspace: Path, hub: Hub):
        self.db_path = db_path
        self.workspace = workspace
        self.hub = hub
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS handoff_desktops (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS handoff_jobs (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, request TEXT NOT NULL,
                    sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS handoff_steps (
                    id TEXT PRIMARY KEY, job TEXT NOT NULL REFERENCES handoff_jobs(id),
                    position INTEGER NOT NULL,
                    desktop TEXT NOT NULL REFERENCES handoff_desktops(id),
                    platform TEXT, UNIQUE (job, position)
                );
                CREATE TABLE IF NOT EXISTS handoff_browser (
                    step TEXT PRIMARY KEY REFERENCES handoff_steps(id),
                    phase TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS handoff_steps_desktop ON handoff_steps(desktop);
            """)

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            yield db

    def authorized(self, token: str) -> bool:
        if len(token) != 43:
            return False
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db:
            return self._desktop(db, token) is not None

    @staticmethod
    def _desktop(db: sqlite3.Connection, token: str):
        return db.execute(
            "SELECT id FROM handoff_desktops WHERE token_hash = ? AND revoked = 0",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()

    def _publish(self, session: str, key: str, text: str, *, job: str | None = None) -> None:
        data = {"text": text}
        if job:
            data["desktop_job_id"] = job
        self.hub.publish_once(session, "background.finished", data, key=key)

    def routes(self) -> APIRouter:
        router = APIRouter()

        @router.post("/desktops", status_code=201)
        def pair(pairing: PairDesktop):
            desktop_id, token = uuid4().hex, secrets.token_urlsafe(32)
            with self.transaction() as db:
                db.execute(
                    "INSERT INTO handoff_desktops (id, label, token_hash) VALUES (?, ?, ?)",
                    (desktop_id, pairing.label, hashlib.sha256(token.encode()).hexdigest()),
                )
            return JSONResponse(
                {"id": desktop_id, "token": token},
                status_code=201,
                headers={"Cache-Control": "no-store"},
            )

        @router.get("/desktops")
        def desktops():
            with self.transaction() as db:
                return [
                    dict(row)
                    for row in db.execute(
                        "SELECT id, label FROM handoff_desktops WHERE revoked = 0"
                    )
                ]

        @router.delete("/desktops/{desktop_id}", status_code=204)
        def revoke(desktop_id: Id):
            with self.transaction() as db:
                db.execute("UPDATE handoff_desktops SET revoked = 1 WHERE id = ?", (desktop_id,))
            return Response(status_code=204)

        @router.post("/sessions/{session}/desktop-jobs", status_code=202)
        def dispatch(session: Id, body: Dispatch):
            if not self.hub.log.exists(session):
                raise HTTPException(404, "Unknown conversation.")
            if len(set(body.desktops)) != len(body.desktops):
                raise HTTPException(422, "Choose distinct desktops.")
            request = body.model_dump_json(exclude_none=True)
            with self.transaction() as db:
                existing = db.execute(
                    "SELECT session, request FROM handoff_jobs WHERE id = ?", (body.job_id,)
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != (session, request):
                        raise HTTPException(409, "This job ID belongs to a different request.")
                    return {"id": body.job_id}
                if not all(
                    db.execute(
                        "SELECT id FROM handoff_desktops WHERE id = ? AND revoked = 0", (desktop,)
                    ).fetchone()
                    for desktop in body.desktops
                ):
                    raise HTTPException(422, "A selected desktop is unavailable.")
                data = read_workspace_bytes(
                    self.workspace, body.file.path(session), limit=ATTACHMENT_BYTES
                )
                if data is None:
                    raise HTTPException(422, "The selected attachment is unavailable.")
                digest = hashlib.sha256(data).hexdigest()
                db.execute(
                    "INSERT INTO handoff_jobs VALUES (?, ?, ?, ?)",
                    (body.job_id, session, request, digest),
                )
                step_ids = [uuid4().hex for _ in body.desktops]
                db.executemany(
                    """INSERT INTO handoff_steps (id, job, position, desktop)
                       VALUES (?, ?, ?, ?)""",
                    [
                        (step_ids[position], body.job_id, position, desktop)
                        for position, desktop in enumerate(body.desktops)
                    ],
                )
                if body.browser is not None:
                    db.execute("INSERT INTO handoff_browser VALUES (?, 'queued')", (step_ids[-1],))
                    self._publish(
                        session,
                        f"handoff:{body.job_id}:queued",
                        f"Queued {body.file.name} for browser delivery to {body.browser.origin}.",
                        job=body.job_id,
                    )
            return {"id": body.job_id}

        @router.get("/sessions/{session}/desktop-jobs/{job_id}")
        def status(session: Id, job_id: Id):
            with self.transaction() as db:
                steps = [
                    dict(row)
                    for row in db.execute(
                        """SELECT s.id, d.label AS desktop, d.id AS desktop_id,
                                  d.revoked, s.platform,
                                  b.phase AS browser_state, j.request
                           FROM handoff_steps s JOIN handoff_desktops d ON d.id = s.desktop
                           JOIN handoff_jobs j ON j.id = s.job
                           LEFT JOIN handoff_browser b ON b.step = s.id
                           WHERE j.session = ? AND j.id = ? ORDER BY s.position""",
                        (session, job_id),
                    )
                ]
            if not steps:
                raise HTTPException(404, "Unknown desktop job.")
            details = Dispatch.model_validate_json(steps[0]["request"])
            for step in steps:
                del step["request"]
            return {
                "id": job_id,
                "steps": steps,
                "complete": all(s["platform"] for s in steps),
                "browser": details.browser.model_dump() if details.browser else None,
                "file": details.file.model_dump(),
            }

        @router.post("/sessions/{session}/desktop-jobs/{job_id}/browser/retarget")
        def retarget(session: Id, job_id: Id, body: BrowserTarget):
            with self.transaction() as db:
                found = db.execute(
                    """SELECT b.step, b.phase, s.desktop FROM handoff_browser b
                       JOIN handoff_steps s ON s.id = b.step
                       JOIN handoff_jobs j ON j.id = s.job
                       WHERE j.id = ? AND j.session = ?""",
                    (job_id, session),
                ).fetchone()
                if found is None:
                    raise HTTPException(404, "Unknown browser job.")
                if found["phase"] not in ("queued", "sign_in", "ready"):
                    raise HTTPException(409, "The submission has already been approved.")
                if (
                    db.execute(
                        "SELECT id FROM handoff_desktops WHERE id = ? AND revoked = 0",
                        (body.desktop,),
                    ).fetchone()
                    is None
                ):
                    raise HTTPException(422, "The selected desktop is unavailable.")
                if found["desktop"] != body.desktop:
                    db.execute(
                        "UPDATE handoff_steps SET desktop = ? WHERE id = ?",
                        (body.desktop, found["step"]),
                    )
                    db.execute(
                        "UPDATE handoff_browser SET phase = 'queued' WHERE step = ?",
                        (found["step"],),
                    )
                    return {"state": "queued"}
                return {"state": found["phase"]}

        @router.post("/sessions/{session}/desktop-jobs/{job_id}/browser/{decision}")
        def decide_browser(
            session: Id, job_id: Id, decision: Literal["approve", "cancel", "verify"]
        ):
            with self.transaction() as db:
                found = db.execute(
                    """SELECT b.step, b.phase FROM handoff_browser b
                       JOIN handoff_steps s ON s.id = b.step
                       JOIN handoff_jobs j ON j.id = s.job
                       WHERE j.id = ? AND j.session = ?""",
                    (job_id, session),
                ).fetchone()
                if found is None:
                    raise HTTPException(404, "Unknown browser job.")
                current = found["phase"]
                if decision == "approve" and current == "ready":
                    target = "approved"
                elif decision == "verify" and current == "uncertain":
                    target = "sending"  # Read-only check, never another submit.
                elif decision == "cancel" and current in ("queued", "sign_in", "ready", "approved"):
                    target = "cancelled"
                elif (
                    decision == "approve"
                    and current in ("approved", "sending", "delivered")
                    or decision == "cancel"
                    and current == "cancelled"
                ):
                    return {"state": current}
                else:
                    raise HTTPException(409, "That action is unavailable for this browser job.")
                db.execute(
                    "UPDATE handoff_browser SET phase = ? WHERE step = ?", (target, found["step"])
                )
            return {"state": target}

        @router.get(WORKER_PATH)
        def next_step(request: Request, browser: bool = False):
            with self.transaction() as db:
                desktop = self._worker(db, request)
                step = db.execute(
                    """SELECT s.id, s.job, j.request, j.sha256, b.phase
                       FROM handoff_steps s JOIN handoff_jobs j ON j.id = s.job
                       LEFT JOIN handoff_browser b ON b.step = s.id
                       WHERE s.desktop = ? AND s.platform IS NULL
                       AND (b.phase IS NULL OR (
                           ? AND b.phase IN ('queued','sign_in','approved','sending')
                       ))
                       AND NOT EXISTS (
                           SELECT 1 FROM handoff_steps p WHERE p.job = s.job
                           AND p.position < s.position AND p.platform IS NULL
                       ) ORDER BY s.rowid LIMIT 1""",
                    (desktop, browser),
                ).fetchone()
            if step is not None and step["phase"] is not None:
                body = Dispatch.model_validate_json(step["request"])
                assert body.browser is not None
                return {
                    "step_id": step["id"],
                    "phase": step["phase"],
                    "form": body.browser.model_dump(),
                    "file": body.file.model_dump(),
                    "sha256": step["sha256"],
                }
            return {"step_id": step["id"] if step else None}

        @router.get(WORKER_PATH + "/{step_id}/file")
        def download(step_id: Id, request: Request):
            with self.transaction() as db:
                step = self._step(db, request, step_id)
                if step["platform"] is not None:
                    raise HTTPException(409, "This step is complete.")
                if step["phase"] is not None and step["phase"] != "sending":
                    raise HTTPException(409, "Wait for the submission approval.")
                file = Dispatch.model_validate_json(step["request"]).file
                data = read_workspace_bytes(
                    self.workspace, file.path(step["session"]), limit=ATTACHMENT_BYTES
                )
            if data is None or hashlib.sha256(data).hexdigest() != step["sha256"]:
                raise HTTPException(409, "The selected attachment changed.")
            return Response(
                data,
                media_type="application/octet-stream",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )

        @router.put(WORKER_PATH)
        def complete(receipt: Receipt | BrowserUpdate, request: Request):
            with self.transaction() as db:
                step = self._step(db, request, receipt.step_id)
                if isinstance(receipt, BrowserUpdate):
                    return self._browser_update(db, step, receipt)
                if step["phase"] is not None:
                    raise HTTPException(409, "This step requires a browser result.")
                if step["sha256"] != receipt.sha256 or step["platform"] not in (
                    None,
                    receipt.platform,
                ):
                    raise HTTPException(409, "The desktop receipt does not match this step.")
                if step["platform"] is None:
                    self._publish(
                        step["session"],
                        f"handoff:{receipt.step_id}:complete",
                        f"{step['label']} ({receipt.platform}) verified the selected file. "
                        f"SHA-256: {receipt.sha256}",
                    )
                    db.execute(
                        "UPDATE handoff_steps SET platform = ? WHERE id = ?",
                        (receipt.platform, receipt.step_id),
                    )
            return {"accepted": True}

        return router

    def _worker(self, db: sqlite3.Connection, request: Request) -> str:
        token = request.headers.get("authorization", "").partition(" ")[2]
        desktop = self._desktop(db, token)
        if desktop is None:
            raise HTTPException(401, "Desktop authentication required.")
        return str(desktop[0])

    def _step(self, db: sqlite3.Connection, request: Request, step_id: str) -> sqlite3.Row:
        desktop = self._worker(db, request)
        step = db.execute(
            """SELECT s.platform, j.session, j.request, j.sha256, d.label, b.phase, j.id AS job
               FROM handoff_steps s JOIN handoff_jobs j ON j.id = s.job
               JOIN handoff_desktops d ON d.id = s.desktop
               LEFT JOIN handoff_browser b ON b.step = s.id
               WHERE s.id = ? AND s.desktop = ? AND NOT EXISTS (
                   SELECT 1 FROM handoff_steps p WHERE p.job = s.job
                   AND p.position < s.position AND p.platform IS NULL
               )""",
            (step_id, desktop),
        ).fetchone()
        if step is None:
            raise HTTPException(404, "Unknown desktop step.")
        return step

    def _browser_update(self, db: sqlite3.Connection, step: sqlite3.Row, update: BrowserUpdate):
        current, target = step["phase"], update.phase
        if current == target:
            return {"changed": False}
        allowed = {
            "sign_in": {"queued"},
            "ready": {"queued", "sign_in"},
            "sending": {"approved"},
            "uncertain": {"sending"},
            "delivered": {"sending", "uncertain"},
        }
        if current not in allowed[target]:
            raise HTTPException(409, "The browser job has moved to a different step.")
        messages = {
            "sign_in": f"{step['label']} needs a private sign-in or browser handback.",
            "ready": f"{step['label']} is ready. Review the file and site before approving.",
            "uncertain": (
                f"{step['label']} could not confirm the submission. Check for a receipt."
            ),
            "delivered": f"{step['label']} confirmed the browser receipt {update.step_id}.",
        }
        if target in messages:
            self._publish(
                step["session"],
                f"handoff:{update.step_id}:{target}",
                messages[target],
                job=step["job"],
            )
        db.execute("UPDATE handoff_browser SET phase = ? WHERE step = ?", (target, update.step_id))
        if target == "delivered":
            db.execute(
                "UPDATE handoff_steps SET platform = ? WHERE id = ?",
                (update.platform, update.step_id),
            )
        return {"changed": True}
