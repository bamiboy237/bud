"""Send an approved, typed app action to one paired Mac."""

import asyncio
import hashlib
import json
import secrets
import time
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bud.core.handoff import DesktopHandoffs, Id, PairDesktop

MAC_WORK_PATH = "/desktop-worker/mac-actions"
_HEARTBEAT_SECONDS = 12
_CLAIM_SECONDS = 8
_RUN_SECONDS = 45


class Result(BaseModel):
    id: Id
    state: Literal["ok", "failed", "uncertain"]
    text: str = Field(max_length=32_000)


class PairedApps:
    def __init__(self, handoffs: DesktopHandoffs) -> None:
        self.handoffs = handoffs
        with handoffs.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mac_endpoints (
                    desktop TEXT PRIMARY KEY REFERENCES handoff_desktops(id), seen REAL
                );
                CREATE TABLE IF NOT EXISTS mac_actions (
                    id TEXT PRIMARY KEY, desktop TEXT NOT NULL REFERENCES mac_endpoints(desktop),
                    action TEXT NOT NULL, args TEXT NOT NULL, expires REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'waiting', result TEXT
                );
                CREATE INDEX IF NOT EXISTS mac_actions_desktop ON mac_actions(desktop, state);
                """)

    def devices(self) -> list[dict]:
        cutoff = time.time() - _HEARTBEAT_SECONDS
        with self.handoffs.transaction() as db:
            return [
                {"id": row[0], "name": row[1], "online": row[2] is not None and row[2] >= cutoff}
                for row in db.execute(
                    "SELECT d.id, d.label, m.seen FROM mac_endpoints AS m "
                    "JOIN handoff_desktops AS d ON m.desktop = d.id WHERE NOT d.revoked"
                )
            ]

    async def dispatch(self, mac: str | None, action: str, args: list[str]) -> dict:
        device = next((device for device in self.devices() if device["id"] == mac), None)
        if device is None:
            return {"ok": False, "error": "Pair a Mac in Settings and use its ID from paired_macs."}
        name = device["name"]
        if not device["online"]:
            return {"ok": False, "error": f"{name} is offline. Open Bud on that Mac."}
        work_id = uuid4().hex
        with self.handoffs.transaction() as db:
            db.execute(
                "INSERT INTO mac_actions(id, desktop, action, args, expires) "
                "VALUES (?, ?, ?, ?, ?)",
                (work_id, mac, action, json.dumps(args), time.time() + _CLAIM_SECONDS),
            )
        deadline = time.monotonic() + _CLAIM_SECONDS + _RUN_SECONDS
        try:
            while time.monotonic() < deadline:
                with self.handoffs.transaction() as db:
                    row = db.execute(
                        "SELECT state, result, expires FROM mac_actions WHERE id = ?", (work_id,)
                    ).fetchone()
                    if row[0] == "waiting" and time.time() >= row[2]:
                        return {"ok": False, "error": f"{name} did not pick up the action."}
                if row[0] == "ok":
                    return {"ok": True, "result": row[1], "mac": name}
                if row[0] == "failed":
                    return {"ok": False, "error": row[1], "mac": name}
                if row[0] == "uncertain":
                    break
                await asyncio.sleep(0.25)
            return {
                "ok": False,
                "uncertain": True,
                "error": f"Check whether the action already happened on {name}. Do not retry it.",
            }
        finally:
            with self.handoffs.transaction() as db:
                db.execute("DELETE FROM mac_actions WHERE id = ?", (work_id,))

    def routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/macs")
        def list_macs() -> dict:
            return {"macs": self.devices()}

        @router.post("/macs", status_code=201)
        def pair(body: PairDesktop) -> JSONResponse:
            desktop, token = uuid4().hex, secrets.token_urlsafe(32)
            with self.handoffs.transaction() as db:
                db.execute(
                    "INSERT INTO handoff_desktops (id, label, token_hash) VALUES (?, ?, ?)",
                    (desktop, body.label, hashlib.sha256(token.encode()).hexdigest()),
                )
                db.execute("INSERT INTO mac_endpoints (desktop) VALUES (?)", (desktop,))
            return JSONResponse(
                {"id": desktop, "token": token},
                status_code=201,
                headers={"Cache-Control": "no-store"},
            )

        @router.post(MAC_WORK_PATH)
        def claim(request: Request) -> dict:
            now = time.time()
            with self.handoffs.transaction() as db:
                desktop = self.handoffs._worker(db, request)
                if not db.execute(
                    "UPDATE mac_endpoints SET seen = ? WHERE desktop = ?", (now, desktop)
                ).rowcount:
                    raise HTTPException(403, "This device is not paired for Mac apps.")
                work = db.execute(
                    """UPDATE mac_actions SET state = 'claimed' WHERE id = (
                           SELECT id FROM mac_actions WHERE desktop = ?
                           AND state = 'waiting' AND expires > ? ORDER BY rowid LIMIT 1
                       ) RETURNING id, action, args""",
                    (desktop, now),
                ).fetchone()
                db.execute(
                    "DELETE FROM mac_actions WHERE expires < ?",
                    (now - 2 * _RUN_SECONDS,),
                )
            if work is None:
                return {}
            return {"id": work[0], "action": work[1], "args": json.loads(work[2])}

        @router.put(MAC_WORK_PATH, status_code=204)
        def finish(request: Request, body: Result) -> None:
            with self.handoffs.transaction() as db:
                desktop = self.handoffs._worker(db, request)
                db.execute(
                    "UPDATE mac_actions SET state = ?, result = ?, args = '[]' "
                    "WHERE id = ? AND desktop = ? AND state = 'claimed'",
                    (body.state, body.text, body.id, desktop),
                )

        return router
