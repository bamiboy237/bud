"""FastAPI app for the core.
It creates sessions, accepts messages, and streams session events as server-sent events.
`create_app` wires the log, hub, runner, and gateway together.
"""

import asyncio
import fcntl
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Annotated, TextIO
from uuid import uuid4

from dbos import DBOS
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from bud.config import get_settings
from bud.core.events import Event
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.projection import Snapshot, snapshot
from bud.core.runner import AwaitingApproval, RunInProgress, Runner, UnknownApproval
from bud.gateway import build_gateway
from bud.harness.actions import load_policy
from bud.harness.agent import Harness
from bud.harness.memory import build_memory
from bud.world.store import World
from bud.world.tools import READ_TOOLS as WORLD_READ_TOOLS
from bud.world.tools import build_tools

logger = logging.getLogger(__name__)


class NewMessage(BaseModel):
    text: str
    message_id: str | None = None


class Decision(BaseModel):
    approved: bool


def sse_frame(event: Event) -> str:
    return f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"


def hold_single_server_lock(path: Path) -> TextIO:
    """One server owns recovery and approval dispatch for this session database."""
    lock = path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"Another bud server is already using {path.parent}.") from None
    return lock


def create_app(*, simulate: bool = False, model_id: str | None = None) -> FastAPI:
    settings = get_settings()
    if not simulate:
        from bud.composio.connection import Connections
        from bud.composio.tools import PROFILE, READ_TOOLS, build_toolset, policy

        connections = Connections(settings)
        accounts = connections.active_accounts()
        # Reconnecting a different account must not resume the old account's approvals.
        scope = sha256(repr(sorted(accounts.items())).encode()).hexdigest()[:16]
        session_dir = connections.folder / "sessions" / scope
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        db_path = session_dir / "bud.db"
        gateway = build_gateway(
            settings, providers={"openai-chatgpt", "openai", "meta"}, model_id=model_id
        )
        harness = Harness(
            build_toolset(connections, accounts, session_dir),
            policy(),
            gateway.registry,
            PROFILE,
            memory=build_memory(session_dir / "memory.db", namespace="owner"),
            spend_path=session_dir / "spend.db",
            result_path=db_path,
            read_tools=READ_TOOLS | {"current_time"},
            result_retention=timedelta(hours=settings.tool_result_retention_hours),
        )
    else:
        db_path = settings.data_dir / "bud.db"
        gateway = build_gateway(settings, model_id=model_id)
        world = World(settings.data_dir / "world.db")
        harness = Harness(
            build_tools(world),
            load_policy(),
            gateway.registry,
            world.profile(),
            memory=build_memory(settings.data_dir / "world-memory.db", namespace="world"),
            spend_path=settings.data_dir / "world-spend.db",
            result_path=db_path,
            read_tools=WORLD_READ_TOOLS,
            result_retention=timedelta(hours=settings.tool_result_retention_hours),
        )
    gateway.model("fast")
    log = EventLog(db_path)
    hub = Hub(log)
    runner = Runner(hub, gateway, harness, durable=True)

    async def cleanup_results() -> None:
        while True:
            try:
                deleted = await harness.results.prune()
                if deleted:
                    logger.info("Removed %d expired temporary tool results", deleted)
            except Exception:
                logger.exception("Temporary tool-result cleanup failed")
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        lock = hold_single_server_lock(db_path.with_suffix(".lock"))
        cleanup = None
        try:
            hub.bind_loop()
            DBOS(
                config={
                    "name": "bud",
                    "system_database_url": "sqlite:///"
                    + str(db_path.with_name("runtime.db").resolve()),
                    "executor_id": "bud-core",
                    "application_version": runner.version,
                    "enable_otlp": False,
                    "log_level": "WARNING",
                }
            )
            DBOS.launch()
            runner.recover()
            cleanup = asyncio.create_task(cleanup_results())
            yield
        finally:
            if cleanup is not None:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
            await runner.stop()
            await asyncio.to_thread(DBOS.destroy, workflow_completion_timeout_sec=5)
            gateway.close()
            lock.close()

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
