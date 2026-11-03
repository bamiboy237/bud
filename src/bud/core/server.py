"""FastAPI app for the core.
It creates sessions, accepts messages, and streams session events as server-sent events.
`create_app` wires the log, hub, runner, and gateway together.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from bud.config import get_settings
from bud.core.events import Event
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.projection import Snapshot, snapshot
from bud.core.runner import AwaitingApproval, RunInProgress, Runner, UnknownApproval
from bud.core.tools import Outbox, build_tools
from bud.gateway import build_gateway


class NewMessage(BaseModel):
    text: str
    message_id: str | None = None


class Decision(BaseModel):
    approved: bool


def sse_frame(event: Event) -> str:
    return f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"


def create_app() -> FastAPI:
    db_path = get_settings().data_dir / "bud.db"
    log = EventLog(db_path)
    hub = Hub(log)
    runner = Runner(hub, build_gateway(), build_tools(Outbox(db_path)))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runner.recover()
        yield
        await runner.stop()

    app = FastAPI(title="bud core", lifespan=lifespan)

    def require_session(session_id: str) -> None:
        if not log.exists(session_id):
            raise HTTPException(404, "unknown session")

    @app.post("/sessions", status_code=201)
    def create_session() -> dict[str, str]:
        session_id = uuid4().hex
        hub.publish(session_id, "session.created")
        return {"session_id": session_id}

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> Snapshot:
        require_session(session_id)
        return snapshot(session_id, log.read(session_id))

    @app.post("/sessions/{session_id}/messages", status_code=202)
    async def post_message(session_id: str, message: NewMessage) -> dict[str, bool]:
        require_session(session_id)
        try:
            event = runner.start(session_id, message.text, message.message_id or uuid4().hex)
        except RunInProgress:
            raise HTTPException(409, "a run is already active") from None
        except AwaitingApproval:
            raise HTTPException(409, "an approval is pending") from None
        return {"duplicate": event is None}

    @app.post("/sessions/{session_id}/approvals/{approval_id}")
    async def decide(session_id: str, approval_id: str, decision: Decision) -> dict[str, bool]:
        require_session(session_id)
        try:
            return {"applied": runner.decide(session_id, approval_id, decision.approved)}
        except UnknownApproval:
            raise HTTPException(404, "unknown approval") from None

    @app.get("/sessions/{session_id}/events")
    async def stream_events(
        session_id: str,
        last_event_id: Annotated[int, Header()] = 0,
    ) -> StreamingResponse:
        require_session(session_id)

        async def frames() -> AsyncIterator[str]:
            async for event in hub.subscribe(session_id, after=last_event_id):
                yield ": keep-alive\n\n" if event is None else sse_frame(event)

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app
