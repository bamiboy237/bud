"""FastAPI app for the core.
It creates sessions, accepts messages, and streams session events as server-sent events.
`create_app` wires the log, hub, runner, and gateway together.
"""

import asyncio
import fcntl
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated, TextIO
from uuid import uuid4

from dbos import DBOS
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr
from pydantic_ai.toolsets import CombinedToolset, FunctionToolset

from bud.composio.connection import ConnectionError, Connections
from bud.config import Settings, get_settings
from bud.core.auth import OwnerAuthentication, owner_token
from bud.core.events import Event, MessageOrigin
from bud.core.hub import Hub
from bud.core.log import EventLog, QueueFull
from bud.core.projection import Snapshot, snapshot
from bud.core.runner import Runner, UnknownApproval
from bud.gateway import build_gateway
from bud.harness.actions import load_policy
from bud.harness.agent import Harness
from bud.harness.memory import build_memory
from bud.photon_config import PhotonConfig
from bud.web import GUIDANCE as WEB_GUIDANCE
from bud.web import READ_TOOLS as WEB_READ_TOOLS
from bud.web import build_tools as build_web_tools
from bud.workspace import GUIDANCE as WORKSPACE_GUIDANCE
from bud.workspace import READ_TOOLS as WORKSPACE_READ_TOOLS
from bud.workspace import WRITE_TOOLS as WORKSPACE_WRITE_TOOLS
from bud.workspace import build_tools as build_workspace_tools
from bud.world.store import World
from bud.world.tools import READ_TOOLS as WORLD_READ_TOOLS
from bud.world.tools import build_tools

logger = logging.getLogger(__name__)


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    origin: MessageOrigin = Field(default_factory=MessageOrigin)
    message_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )


class NewSession(BaseModel):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class EditMessage(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class Decision(BaseModel):
    approved: bool
    origin: MessageOrigin = Field(default_factory=MessageOrigin)


def sse_frame(event: Event) -> str:
    return f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"


def hold_single_server_lock(path: Path) -> TextIO:
    """One server owns recovery and approval dispatch for this session database."""
    lock = path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit(f"Another bud server is already using {path.parent}.") from None
    except OSError:
        lock.close()
        raise
    return lock


def workspace_tools(directory: Path) -> FunctionToolset[None]:
    root = directory / "workspace"
    root.mkdir(exist_ok=True, mode=0o700)
    return FunctionToolset(build_workspace_tools(root, directory / "workspace-dispatches.jsonl"))


def create_app(*, simulate: bool = False, model_id: str | None = None) -> FastAPI:
    settings = get_settings()
    token = owner_token(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # This lock spans all account generations, before any live sessions, tools,
    # recovery or runners can be constructed. A scoped DB lock alone is too late.
    live_lock = (
        None if simulate else hold_single_server_lock(settings.data_dir / "live-server.lock")
    )
    try:
        return _create_app(settings, token, live_lock, simulate=simulate, model_id=model_id)
    except BaseException:
        if live_lock is not None:
            live_lock.close()
        raise


def _create_app(
    settings: Settings,
    token: SecretStr,
    live_lock: TextIO | None,
    *,
    simulate: bool,
    model_id: str | None,
) -> FastAPI:
    connections = None
    photon = None
    if not simulate and any(
        (
            settings.photon_api_key,
            settings.photon_project_id,
            settings.photon_owner_handle,
            settings.photon_agent_handle,
        )
    ):
        # Partial/invalid configuration fails before account construction/recovery.
        photon = PhotonConfig.load(settings)
    messaging_state = {
        "status": "disabled_in_simulation"
        if simulate
        else "starting"
        if photon
        else "not_configured"
    }
    if not simulate:
        from bud.composio.tools import PROFILE, READ_TOOLS, build_toolset, policy

        connections = Connections(settings)
        binding = connections.capture_binding()
        accounts = binding.accounts
        # Neither old approvals nor memory cross an owner/generation boundary.
        session_dir = connections.folder / "sessions" / binding.scope
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        db_path = session_dir / "bud.db"
        gateway = build_gateway(
            settings, providers={"openai-chatgpt", "openai", "meta"}, model_id=model_id
        )
        tools_policy = policy()
        tools_policy.tools.update(dict.fromkeys(WEB_READ_TOOLS | WORKSPACE_READ_TOOLS, "allow"))
        tools_policy.tools.update(dict.fromkeys(WORKSPACE_WRITE_TOOLS, "ask"))
        harness = Harness(
            CombinedToolset(
                [
                    build_toolset(connections, accounts, session_dir),
                    build_web_tools(settings),
                    workspace_tools(session_dir),
                ]
            ),
            tools_policy,
            gateway.registry,
            PROFILE
            + "\nConnected apps: "
            + (", ".join(sorted(accounts)) or "none")
            + "\nUse /connect gmail or /connect calendar for a missing/disconnected app. "
            "Then /refresh and restart the bud server process; start a new conversation. "
            "Connection commands are user controls, not model tools."
            + WEB_GUIDANCE
            + WORKSPACE_GUIDANCE,
            memory=build_memory(session_dir / "memory.db", namespace="owner"),
            spend_path=session_dir / "spend.db",
            result_path=db_path,
            read_tools=READ_TOOLS | WEB_READ_TOOLS | WORKSPACE_READ_TOOLS | {"current_time"},
            result_retention=timedelta(hours=settings.tool_result_retention_hours),
        )
    else:
        db_path = settings.data_dir / "bud.db"
        gateway = build_gateway(settings, model_id=model_id)
        world = World(settings.data_dir / "world.db")
        tools_policy = load_policy()
        tools_policy.tools.update(dict.fromkeys(WORKSPACE_READ_TOOLS, "allow"))
        tools_policy.tools.update(dict.fromkeys(WORKSPACE_WRITE_TOOLS, "ask"))
        harness = Harness(
            CombinedToolset(
                [FunctionToolset(build_tools(world)), workspace_tools(settings.data_dir)]
            ),
            tools_policy,
            gateway.registry,
            world.profile() + WORKSPACE_GUIDANCE,
            memory=build_memory(settings.data_dir / "world-memory.db", namespace="world"),
            spend_path=settings.data_dir / "world-spend.db",
            result_path=db_path,
            read_tools=WORLD_READ_TOOLS | WORKSPACE_READ_TOOLS,
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
        lock = live_lock or hold_single_server_lock(db_path.with_suffix(".lock"))
        cleanup = None
        services = AsyncExitStack()
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
            if connections is not None and connections.restart_required():
                raise ConnectionError("Account binding changed during startup. Restart bud.")
            await runner.recover()
            if photon is not None:
                from bud.photon_chat import messaging

                try:
                    await services.enter_async_context(
                        messaging(
                            settings,
                            photon,
                            app,
                            token,
                            {"scope": scope_id, "mode": "accounts"},
                            messaging_events,
                            lambda status: messaging_state.update(status=status),
                        )
                    )
                except Exception:
                    # Keep authenticated status/Stop available, but never advertise
                    # a failed messaging connection as ready or restart uncertain sends.
                    messaging_state["status"] = "failed"
                    logger.error(
                        "iMessage could not start. Inspect its configuration/state "
                        "and restart bud. "
                        "The authenticated core remains available; sends were not retried."
                    )
            cleanup = asyncio.create_task(cleanup_results())
            yield
        finally:
            try:
                try:
                    # Stop accepting/sending messages before stopping core work.
                    await services.aclose()
                finally:
                    if cleanup is not None:
                        cleanup.cancel()
                        await asyncio.gather(cleanup, return_exceptions=True)
                    await runner.stop()
                    await asyncio.to_thread(DBOS.destroy, workflow_completion_timeout_sec=5)
            finally:
                try:
                    gateway.close()
                finally:
                    lock.close()

    scope_id = (
        binding.scope
        if connections is not None
        else hashlib.sha256(str(db_path.resolve()).encode()).hexdigest()
    )
    app = FastAPI(title="bud core", lifespan=lifespan)
    app.add_middleware(OwnerAuthentication, token=token, scope_id=scope_id)

    async def messaging_events(session_id: str, after: int) -> AsyncIterator[dict]:
        # The server-owned adapter reads this scope's hub directly. ASGITransport
        # is used for finite commands, not an indefinitely buffered SSE response.
        if not log.exists(session_id):
            raise RuntimeError("Saved iMessage conversation is unavailable.")
        async for event in hub.subscribe(session_id, after=after):
            if event is not None:
                yield event.model_dump(mode="json")

    @app.get("/runtime")
    def runtime() -> JSONResponse:
        """Authenticated transport handshake; no keys or account identifiers."""
        return JSONResponse(
            {
                "mode": "simulate" if simulate else "accounts",
                "scope": scope_id,
                "restart_required": connections is not None and connections.restart_required(),
                "messaging": messaging_state["status"],
            },
            headers={"Cache-Control": "no-store"},
        )

    def live_connections() -> Connections:
        if connections is None:
            raise HTTPException(409, "Account controls are disabled in --simulate mode.")
        return connections

    @app.get("/connections")
    @app.post("/connections/refresh")
    def connection_status() -> JSONResponse:
        current = live_connections()
        try:
            statuses = current.statuses()
            restart_required = current.restart_required()
        except ConnectionError as exc:
            raise HTTPException(503, str(exc)) from None
        return JSONResponse(
            {
                "statuses": {"gmail": statuses["gmail"], "calendar": statuses["googlecalendar"]},
                "restart_required": restart_required,
                "refresh_boundary": "process_restart",
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/connections/{toolkit}/connect")
    def connect_account(toolkit: str) -> JSONResponse:
        current = live_connections()
        name = {"gmail": "gmail", "calendar": "googlecalendar"}.get(toolkit)
        if name is None:
            raise HTTPException(400, "Use /connect gmail or /connect calendar.")
        try:
            url = current.connect(name)
        except ConnectionError as exc:
            raise HTTPException(503, str(exc)) from None
        # The sign-in link exists only in this immediate authenticated response,
        # not in session events, model input/history, status metadata or logs.
        return JSONResponse(
            {"url": url, "restart_required": True, "refresh_boundary": "process_restart"},
            headers={"Cache-Control": "no-store"},
        )

    def require_session(session_id: str) -> None:
        if not log.exists(session_id):
            raise HTTPException(404, "unknown session")

    def require_current_binding() -> None:
        if connections is not None and connections.restart_required():
            raise HTTPException(409, "Account binding changed. Restart bud and start a new chat.")

    @app.post("/sessions/primary")
    def primary_session(request: NewSession | None = None) -> dict[str, str]:
        require_current_binding()
        try:
            session_id = log.primary_session(request.session_id if request else None)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        hub.wake(session_id)
        return {"session_id": session_id}

    @app.post("/sessions", status_code=201)
    def create_session(request: NewSession | None = None) -> dict[str, str]:
        require_current_binding()
        session_id = request.session_id if request else uuid4().hex
        # A transport persists its chosen ID before this request. A lost HTTP
        # response must not create a second conversation on retry.
        if (
            log.append_once(session_id, "session.created", key=f"session-created:{session_id}")
            is not None
        ):
            hub.wake(session_id)
        return {"session_id": session_id}

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> Snapshot:
        require_session(session_id)
        return snapshot(session_id, log.read(session_id))

    @app.post("/sessions/{session_id}/messages", status_code=202)
    async def post_message(session_id: str, message: NewMessage) -> dict[str, bool]:
        require_session(session_id)
        require_current_binding()
        try:
            event = runner.start(
                session_id,
                message.text,
                message.message_id or uuid4().hex,
                message.origin.model_dump(),
            )
        except QueueFull as exc:
            raise HTTPException(429, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"duplicate": event is None}

    @app.patch("/sessions/{session_id}/queue/{message_id}")
    async def edit_queued_message(session_id: str, message_id: str, message: EditMessage) -> dict:
        require_session(session_id)
        try:
            runner.edit_message(session_id, message_id, message.text)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"edited": True}

    @app.delete("/sessions/{session_id}/queue/{message_id}")
    async def cancel_queued_message(session_id: str, message_id: str) -> dict:
        require_session(session_id)
        return {"cancelled": runner.cancel_message(session_id, message_id)}

    @app.post("/sessions/{session_id}/queue/pause")
    async def pause_queue(session_id: str) -> dict:
        require_session(session_id)
        return {"changed": runner.pause_queue(session_id, True), "queue_paused": True}

    @app.post("/sessions/{session_id}/queue/resume")
    async def resume_queue(session_id: str) -> dict:
        require_session(session_id)
        require_current_binding()
        return {"changed": runner.pause_queue(session_id, False), "queue_paused": False}

    @app.post("/sessions/{session_id}/stop", status_code=202)
    async def stop_session(session_id: str) -> dict:
        require_session(session_id)
        return await runner.stop_session(session_id)

    @app.post("/sessions/{session_id}/approvals/{approval_id}")
    async def decide(session_id: str, approval_id: str, decision: Decision) -> dict[str, bool]:
        require_session(session_id)
        if decision.approved:
            require_current_binding()
        try:
            return {
                "applied": runner.decide(
                    session_id, approval_id, decision.approved, decision.origin.model_dump()
                )
            }
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
