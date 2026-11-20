"""FastAPI app for the core.
It creates sessions, accepts messages, and streams session events as server-sent events.
`create_app` wires the log, hub, runner, and gateway together.
"""

import asyncio
import contextlib
import fcntl
import hashlib
import logging
import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, TextIO
from uuid import uuid4

from dbos import DBOS
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr
from pydantic_ai import ModelRetry
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, FunctionToolset

from bud import browser
from bud.composio.binding import AccountBinding, valid_toolkit
from bud.composio.connection import ConnectionError, Connections
from bud.config import Settings, get_settings
from bud.core.auth import OwnerAuthentication, owner_token
from bud.core.background import GUIDANCE as BACKGROUND_GUIDANCE
from bud.core.background import STARTS as BACKGROUND_STARTS
from bud.core.background import TOOLS as BACKGROUND_TOOLS
from bud.core.background import Background
from bud.core.events import Event, MessageOrigin
from bud.core.history import conversation_search
from bud.core.hub import Hub
from bud.core.log import EventLog, QueueFull
from bud.core.profiles import Mode, ProfileStore, apply_mode, as_mode, mode_summary
from bud.core.profiles import guidance as MODE_GUIDANCE
from bud.core.projection import Snapshot, parked_approvals, undecided_approvals
from bud.core.runner import DecisionRejected, Runner, UnknownApproval
from bud.core.setup import (
    SetupJournal,
    SetupRegistry,
    build_setup_toolset,
    check_setup,
    setup_registry,
)
from bud.core.workspace_home import display_path, workspace_home
from bud.gateway import build_gateway
from bud.harness.actions import Policy, load_policy
from bud.harness.agent import Harness
from bud.harness.ask import MAX_QUESTIONS, AskAnswer
from bud.harness.grants import Grants
from bud.harness.learning import Learner
from bud.harness.memory import build_memory
from bud.harness.recall import Recall, embedder
from bud.harness.review import ToolReviewer, build_reviewer
from bud.limits import MAX_MESSAGE_CHARS
from bud.logs import guard_logs
from bud.photon_config import PhotonConfig
from bud.shell import GUIDANCE as SHELL_GUIDANCE
from bud.shell import NETWORK_TOOL, SHELL_TOOLS, NetworkGrants
from bud.shell import available as shell_available
from bud.shell import build_tools as build_shell_tools
from bud.shell import runner as shell_runner
from bud.web import GUIDANCE as WEB_GUIDANCE
from bud.web import READ_TOOLS as WEB_READ_TOOLS
from bud.web import build_tools as build_web_tools
from bud.workspace import (
    DOCUMENT_TOOL,
    LOCAL_GUIDANCE,
    LOCAL_TOOLS,
    describe_local,
    document_tool,
    local_blocked,
    local_refusal,
    local_tools,
)
from bud.workspace import GUIDANCE as WORKSPACE_GUIDANCE
from bud.workspace import READ_TOOLS as WORKSPACE_READ_TOOLS
from bud.workspace import WRITE_TOOLS as WORKSPACE_WRITE_TOOLS
from bud.workspace import build_tools as build_workspace_tools
from bud.workspace import read_capabilities as workspace_read_capabilities
from bud.world.describe import build_describers, build_notifies_others
from bud.world.store import World
from bud.world.tools import READ_TOOLS as WORLD_READ_TOOLS
from bud.world.tools import build_tools

logger = logging.getLogger(__name__)
# Workspace reads that never ask: the harness FileSystem tools and read_document.
FILE_READS = WORKSPACE_READ_TOOLS | {DOCUMENT_TOOL}


def _noop() -> None:
    return None


class ModeRequest(BaseModel):
    mode: str


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    origin: MessageOrigin = Field(default_factory=MessageOrigin)
    message_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )


class NewSession(BaseModel):
    session_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    model: str | None = None


class EditMessage(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class Decision(BaseModel):
    approved: bool
    origin: MessageOrigin = Field(default_factory=MessageOrigin)
    # Also record a standing permission for this tool, so later calls do not ask.
    remember: bool = False


class Answer(BaseModel):
    """A typed reply (`text`), or one answer per question from a card (`answers`)."""

    text: str | None = Field(default=None, min_length=1, max_length=MAX_MESSAGE_CHARS)
    answers: list[AskAnswer] | None = Field(default=None, min_length=1, max_length=MAX_QUESTIONS)
    origin: MessageOrigin = Field(default_factory=MessageOrigin)


class ConnectRequest(BaseModel):
    resume: bool = False


def sse_frame(event: Event) -> str:
    # Live text has no stored position; only stored events move the reconnect cursor.
    cursor = "" if event.live else f"id: {event.seq}\n"
    return f"{cursor}event: {event.type}\ndata: {event.model_dump_json()}\n\n"


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


def workspace_root(directory: Path) -> Path:
    # The data folder is resolved once, here, because /tmp and /var are symlinks on macOS and
    # the workspace walk refuses symlinks. The workspace root below it is not resolved, so a
    # symlink swapped in at that level is still refused.
    root = directory.resolve() / "workspace"
    root.mkdir(exist_ok=True, mode=0o700)
    return root


def workspace_tools(
    root: Path,
    state_dir: Path,
    reviewer: Callable[[], ToolReviewer | None],
    private: Sequence[Path] = (),
    background: Background | None = None,
) -> FunctionToolset[None]:
    """File tools, and on macOS the sandboxed shell, whose network checks use `reviewer`.
    Shell commands cannot read `private` folders, such as Bud's data and its .env."""
    grants = NetworkGrants(state_dir / "network-grants.json")
    shell = build_shell_tools(root, grants, reviewer, private) if shell_available() else []
    return FunctionToolset(
        [
            *build_workspace_tools(root, state_dir / "workspace-dispatches.jsonl"),
            document_tool(root),
            *local_tools(local_blocked(Path.home(), list(private), root)),
            *shell,
            *(background.tools() if background is not None else []),
        ]
    )


def build_background(root: Path, state_dir: Path, private: Sequence[Path] = ()) -> Background:
    """Reminders and long commands, run by the server in run_command's sandbox. A command
    that runs for hours gets no reviewer to allow new sites: only package sites and sites the
    owner allowed. Any other site is blocked and listed in its result."""
    grants = NetworkGrants(state_dir / "network-grants.json")
    run = shell_runner(root, grants, lambda: None, private) if shell_available() else None
    return Background(state_dir / "background.db", root, run)


def deny_stale(
    log: EventLog, decide: Callable[[str, str, bool, dict], bool], generation: int
) -> list[str]:
    """Deny every waiting approval card asked under another account generation, in every chat
    and job. The cards close; the account tools also refuse such an approval at dispatch
    (bud.composio.tools.stale_approval), whether it was granted, parked, or recovered after a
    restart. An account card with no stamp, from before stamps existed, is denied too: its
    account is unknown. Returns the denied approval ids."""
    origin = {"channel": "api", "client_id": "account-change"}
    denied = []
    for session_id in log.sessions_with_requests():
        events = log.read(session_id)
        for request in undecided_approvals(events) + parked_approvals(events):
            review = request.get("review")
            stamp = review.get("account_generation") if isinstance(review, dict) else None
            if stamp == generation:
                continue
            if not isinstance(stamp, int) and not ACCOUNT_TOOL.match(str(request.get("tool"))):
                continue  # not an account tool: no account to go stale
            with contextlib.suppress(UnknownApproval, DecisionRejected):
                if decide(session_id, request["approval_id"], False, origin):
                    denied.append(request["approval_id"])
    return denied


# Account tools are named TOOLKIT_ACTION in capitals, such as GMAIL_SEND_EMAIL.
ACCOUNT_TOOL = re.compile(r"[A-Z][A-Z0-9]*_[A-Z0-9_]+")


def private_folders(settings: Settings) -> list[Path]:
    """Folders a shell command must never read: Bud's data and the folder holding its .env."""
    return [settings.data_dir, Path.cwd()]


def file_guidance() -> str:
    return (
        WORKSPACE_GUIDANCE
        + LOCAL_GUIDANCE
        + (SHELL_GUIDANCE if shell_available() else "")
        + BACKGROUND_GUIDANCE
    )


def allow_file_tools(policy: Policy) -> None:
    """Reads, writes in Bud's own workspace, and sandboxed commands run unasked; each new
    network site asks. Strict mode makes workspace writes ask (bud.core.profiles)."""
    policy.tools.update(dict.fromkeys(FILE_READS | SHELL_TOOLS | WORKSPACE_WRITE_TOOLS, "allow"))
    # Starting a reminder or a background command asks; auto mode hands it to the reviewer
    # (bud.core.profiles). Listing and cancelling only touch the owner's own items.
    policy.tools.update(dict.fromkeys(BACKGROUND_TOOLS - BACKGROUND_STARTS, "allow"))
    policy.tools.update(dict.fromkeys(BACKGROUND_STARTS, "ask"))
    # No "always allow": one click must not let every later start skip approval.
    policy.no_grants = policy.no_grants | BACKGROUND_STARTS
    policy.tools[NETWORK_TOOL] = "ask"
    # Each read outside the workspace asks, in every mode, and offers no "always allow".
    policy.tools.update(dict.fromkeys(LOCAL_TOOLS, "ask"))
    # Each new site needs its own yes: "always allow" would let Bud add any site unasked.
    policy.no_grants = policy.no_grants | {NETWORK_TOOL} | LOCAL_TOOLS


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
    binding = None
    photon = None
    setup_journal = None
    setup: SetupRegistry | None = None
    world: World | None = None
    account_scope: str | None = None
    # Bud's own Browse relay, and a `browse pair` waiting for the extension; the pairing
    # stops when the server stops.
    relay = browser.Relay(browser.RELAY_HOME)
    pairing = browser.Pairing(relay)

    # Set by the account branch: reloads the tool list after a new app connects.
    refresh_tools: Callable[[], None] = _noop

    # The approval mode applies to the harness policy after composition (the
    # Harness unions memory/skill rules in), one seam for every tool class.
    profile_store = ProfileStore(settings.data_dir / "approval-mode.json")
    approval_mode = profile_store.load()
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
        from bud.composio.tools import (
            PROFILE,
            READ_TOOLS,
            build_toolset,
            policy,
        )

        connections = Connections(settings)
        binding = connections.capture_binding()
        accounts = binding.accounts
        # One session folder across account generations; approvals carry their generation.
        account_scope = connections.store.session_scope(binding)
        session_dir = connections.folder / "sessions" / account_scope
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        db_path = session_dir / "bud.db"
        workspace = workspace_home(settings)
        background = build_background(workspace, session_dir, private_folders(settings))
        gateway = build_gateway(
            settings,
            providers={"openai-chatgpt", "openai", "meta", "mistral", "deepseek"},
            model_id=model_id,
        )
        tools_policy = policy()
        tools_policy.tools.update(dict.fromkeys(WEB_READ_TOOLS, "allow"))
        allow_file_tools(tools_policy)
        tools_policy.tools["setup_capability"] = "allow"
        browsing = settings.browser_tools
        consent = browser.Consent(settings.data_dir / "browser-consent.json")
        setup = setup_registry(connections)
        if browsing:
            browser.allow_tools(tools_policy)
            setup.register("browser", browser.setup_entry(pairing))
        setup_journal = SetupJournal()
        loaded: dict[str, Any] = {
            "binding": binding,
            "accounts": frozenset(accounts),
            "browse": browser.executable() if browsing else None,
        }
        refresh_lock = Lock()

        def browser_toolsets() -> list[AbstractToolset[None]]:
            if loaded["browse"] is None:
                return []
            return [
                browser.toolset(loaded["browse"], consent, relay),
                FunctionToolset(browser.control_tools(consent, relay)),
            ]

        def account_toolset(binding: AccountBinding | None) -> CombinedToolset[None]:
            return CombinedToolset(
                [
                    build_toolset(connections, binding, session_dir),
                    build_web_tools(settings),
                    workspace_tools(
                        workspace,
                        session_dir,
                        lambda: harness.reviewer,
                        private_folders(settings),
                        background,
                    ),
                    build_setup_toolset(setup, setup_journal, refresh=refresh_tools),
                    *browser_toolsets(),
                ]
            )

        def refresh_account_tools() -> None:
            """Swap in the account tools after any change, in the same chat: a new app, a
            reconnect, or a removal. The next message sees them; no restart is needed."""
            with refresh_lock:
                binding = connections.stored()
                # Browse installed after Bud started, such as from the setup card, loads too.
                browse = browser.executable() if browsing else None
                if (
                    binding == loaded["binding"] == connections.binding
                    and browse == loaded["browse"]
                ):
                    return
                # Build first and adopt last: if anything fails, nothing changed, and the next
                # request tries again. Old tools keep refusing to dispatch meanwhile.
                replacement = account_toolset(binding)
                connections.adopt(binding)
                harness.swap_tools(replacement)
                previous = loaded["binding"]
                loaded.update(binding=binding, browse=browse, accounts=frozenset(binding.accounts))
                if previous is None or binding.generation != previous.generation:
                    # After the swap, so a run resumed by a denial enters the new tools.
                    deny_stale_approvals(binding.generation)

        refresh_tools = refresh_account_tools
        harness = Harness(
            account_toolset(binding),
            tools_policy,
            gateway.registry,
            PROFILE + "\nComposio is your library of app integrations. Use discover_tools to check "
            "whether the library supports an app and whether it is connected. Use find_app_actions "
            "to load only the actions you need from a connected app. When no app is needed, "
            "answer without searching the library. To connect a supported app, call "
            "setup_capability with its slug: the person gets a sign-in card in chat and "
            "authorizes in their browser. Never send the person to a command line. The "
            "connection is ready on the next message; no restart is needed."
            + WEB_GUIDANCE
            + file_guidance()
            + (browser.GUIDANCE if browsing else "")
            + f"Your workspace is the folder {display_path(workspace)} on the person's Mac.\n",
            memory=build_memory(session_dir / "memory.db", namespace="owner"),
            state_dir=session_dir,
            result_path=db_path,
            read_tools=READ_TOOLS
            | WEB_READ_TOOLS
            | WORKSPACE_READ_TOOLS
            | {DOCUMENT_TOOL, "current_time"},
            result_retention=timedelta(hours=settings.tool_result_retention_hours),
            capabilities=workspace_read_capabilities(workspace, deferred=True),
        )
        tools_policy = harness.policy
        if browsing:
            harness.describers.update(browser.describers(relay))
            harness.harmless.update(browser.harmless(consent))
        harness.live_instructions.append(
            lambda: (
                "\nConnected apps: "
                + (
                    ", ".join(sorted(loaded["accounts"]))
                    or "none yet; setup_capability can connect a supported app"
                )
            )
        )
    else:
        db_path = settings.data_dir / "bud.db"
        workspace = workspace_root(settings.data_dir)
        background = build_background(workspace, settings.data_dir, private_folders(settings))
        gateway = build_gateway(settings, model_id=model_id)
        world = World(settings.data_dir / "world.db")
        tools_policy = load_policy()
        # The simulated world gets the production web tools: research is read-only, and a
        # mock search engine would teach habits that break against the real web.
        tools_policy.tools.update(dict.fromkeys(WEB_READ_TOOLS, "allow"))
        allow_file_tools(tools_policy)
        harness = Harness(
            CombinedToolset(
                [
                    FunctionToolset(build_tools(world)),
                    build_web_tools(settings),
                    workspace_tools(
                        workspace,
                        settings.data_dir,
                        lambda: harness.reviewer,
                        private_folders(settings),
                        background,
                    ),
                ]
            ),
            tools_policy,
            gateway.registry,
            world.profile() + WEB_GUIDANCE + file_guidance(),
            memory=build_memory(settings.data_dir / "world-memory.db", namespace="world"),
            state_dir=settings.data_dir,
            result_path=db_path,
            read_tools=WORLD_READ_TOOLS | WEB_READ_TOOLS | FILE_READS,
            result_retention=timedelta(hours=settings.tool_result_retention_hours),
            capabilities=workspace_read_capabilities(workspace, deferred=True),
        )
    # One seam for both branches: memory/skill rules are unioned inside the
    # Harness, so the profile overlay lands on the composed policy. The
    # guards hold this Policy by reference and re-read decide() per call.
    # Auto mode reviews each write when a reviewer exists (an OpenAI key, review not turned
    # off). Set before the Runner builds its agent, which captures the reviewer.
    harness.reviewer = build_reviewer()
    # Standing permissions the owner granted from an approval card. The policy reads them on
    # every call, so a new grant or a revoke applies without a restart.
    grants = Grants(settings.data_dir / "grants.json")
    harness.policy.grants = grants.tools
    if world is not None:
        harness.describers = build_describers(world)
        harness.notifies_others = build_notifies_others(world)
    # The card names the real file a local read opens, which a link cannot disguise.
    outside = local_blocked(Path.home(), private_folders(settings), workspace)
    harness.describers.update(
        dict.fromkeys(LOCAL_TOOLS, lambda tool_args: describe_local(tool_args, outside))
    )
    harness.refuses.update(
        dict.fromkeys(LOCAL_TOOLS, lambda tool_args: local_refusal(tool_args, outside))
    )
    has_reviewer = harness.reviewer is not None
    apply_mode(harness.policy, approval_mode, reviewer=has_reviewer)
    live_mode: dict[str, Mode] = {"mode": approval_mode}
    harness.live_instructions.append(
        lambda: MODE_GUIDANCE(live_mode["mode"], reviewer=has_reviewer)
    )

    harness.clock = (
        (lambda: datetime.fromisoformat(world.now))
        if world is not None
        else (lambda: datetime.now().astimezone())
    )
    gateway.model("fast", require_tools=True)
    log = EventLog(db_path)
    # Before the runner builds the agent, which reads the harness's capabilities.
    harness.capabilities.append(conversation_search(log, deferred=True))
    hub = Hub(log)
    if setup_journal is not None:
        # The setup toolset was built before the log and hub existed; one seam
        # hands its journal over so every setup event is a journaled event.
        setup_journal.bind(hub)
    background.bind(hub)
    learner = (
        Learner(
            harness.memory,
            lambda session_id: gateway.model("fast", f"{session_id}:learn", require_tools=True),
        )
        if harness.memory is not None
        else None
    )
    recall = Recall(
        db_path.parent / ("world-recall.db" if world is not None else "recall.db"),
        embedder=embedder(settings),
        memory=harness.memory,
    )
    runner = Runner(
        hub,
        gateway,
        harness,
        durable=True,
        jobs=True,
        learner=learner,
        recall=recall,
        workspace=workspace,
    )

    async def every_minute() -> None:
        while True:
            try:
                deleted = await harness.results.prune()
                if deleted:
                    logger.info("Removed %d expired temporary tool results", deleted)
            except Exception:
                logger.exception("Temporary tool-result cleanup failed")
            try:
                await asyncio.to_thread(runner.watch_jobs)
            except Exception:
                logger.exception("Checking for stalled jobs failed")
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
            guard_logs()  # after DBOS adds its handler; uvicorn's exist already
            DBOS.launch()
            if connections is not None and connections.restart_required():
                raise ConnectionError("Account binding changed during startup. Restart bud.")
            # Settle work from before the restart first, so a recovered run's new start is
            # never mistaken for one the restart cut off.
            await background.resume()
            if connections is not None and connections.binding is not None:
                # Cards asked under an account replaced while Bud was stopped close before
                # recovery can resume any of them.
                deny_stale_approvals(connections.binding.generation)
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
            cleanup = asyncio.create_task(every_minute())
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
                    await background.close()
                    await runner.stop()
                    await asyncio.to_thread(DBOS.destroy, workflow_completion_timeout_sec=5)
            finally:
                try:
                    pairing.stop()
                    gateway.close()
                finally:
                    lock.close()

    scope_id = (
        account_scope
        if account_scope is not None
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
            if event is not None and not event.live:
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
                "approval_mode": profile_store.load(),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/mode")
    def switch_mode(body: ModeRequest) -> JSONResponse:
        """Switch the approval mode for the next tool call, and persist it.

        The policy object is shared with the dispatch guard and re-read per
        call, so this applies without a server restart. Real-account writes
        keep their ``ask`` classification in every mode (bud.core.profiles).
        """
        target = as_mode(body.mode)
        if target is None:
            raise HTTPException(400, "Unknown mode; one of strict/auto.")
        profile_store.save(target)
        overlay = apply_mode(harness.policy, target, reviewer=has_reviewer)
        live_mode["mode"] = target
        auto = sum(1 for action in overlay.values() if action == "review")
        return JSONResponse(
            {
                "mode": target,
                "auto_writes": auto,
                "summary": mode_summary(target, reviewer=has_reviewer),
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
            refresh_tools()  # any change, a reconnect included, loads in this chat
            restart_required = current.restart_required()
        except ConnectionError as exc:
            raise HTTPException(503, str(exc)) from None
        return JSONResponse(
            {
                "statuses": {"gmail": statuses["gmail"], "calendar": statuses["googlecalendar"]},
                "restart_required": restart_required,
                "refresh_boundary": "process_restart" if restart_required else "next_message",
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/connections/{toolkit}/connect")
    def connect_account(toolkit: str, request: ConnectRequest | None = None) -> JSONResponse:
        current = live_connections()
        name = {"calendar": "googlecalendar"}.get(toolkit, toolkit)
        if not valid_toolkit(name):
            raise HTTPException(400, "Use a valid app id.")
        try:
            url = current.pending_url(name) if request is not None and request.resume else None
            if url is None:
                url = current.connect(name)
            refresh_tools()  # a reconnect retires the old account at once, in place
            restart_required = current.restart_required()
        except ConnectionError as exc:
            raise HTTPException(503, str(exc)) from None
        # The sign-in link exists only in this immediate authenticated response,
        # not in session events, model input/history, status metadata or logs.
        return JSONResponse(
            {
                "url": url,
                "restart_required": restart_required,
                "refresh_boundary": "process_restart" if restart_required else "next_message",
            },
            headers={"Cache-Control": "no-store"},
        )

    def require_session(session_id: str) -> None:
        if not log.exists(session_id):
            raise HTTPException(404, "unknown session")

    def require_current_binding() -> None:
        """A changed account loads in place, in the same chat."""
        if connections is not None and connections.restart_required():
            try:
                refresh_tools()
            except ConnectionError as exc:
                raise HTTPException(409, str(exc)) from None

    def deny_stale_approvals(generation: int) -> None:
        deny_stale(log, runner.decide, generation)

    @app.post("/sessions/{session_id}/setup/{capability}/check")
    def check_capability(session_id: str, capability: str) -> JSONResponse:
        require_session(session_id)
        require_current_binding()
        if setup is None or setup_journal is None:
            raise HTTPException(409, "Account setup is unavailable in this mode.")
        entry = setup.entry(capability)
        if entry is None or capability not in setup_journal.pending(session_id):
            raise HTTPException(404, "unknown setup card")
        result = check_setup(
            entry,
            setup_journal,
            session_id,
            capability,
            key=f"owner-setup-check:{uuid4().hex}",
            refresh=refresh_tools,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/sessions/{session_id}/setup/{capability}/cancel")
    def cancel_capability(session_id: str, capability: str) -> JSONResponse:
        require_session(session_id)
        if setup_journal is None:
            raise HTTPException(404, "unknown setup card")
        card = setup_journal.pending(session_id).get(capability)
        if card is None:
            return JSONResponse(
                {"capability": capability, "status": "cancelled"},
                headers={"Cache-Control": "no-store"},
            )
        status = "cancelled"
        if capability == "browser":
            pairing.stop()  # the cancelled code stops working at once
        if connections is not None:
            name = {"calendar": "googlecalendar"}.get(capability, capability)
            removed = connections.store.cancel_connect(name)
            if not removed and name in connections.accounts():
                try:
                    refresh_tools()
                except ConnectionError as exc:
                    raise HTTPException(503, str(exc)) from None
                status = "connected"
        data = {"capability": capability, "label": card.label, "status": status}
        hub.publish(session_id, "capability.status", data)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.get("/models")
    def list_models() -> list[dict]:
        keys = {
            "deepseek": settings.deepseek_api_key,
            "openai": settings.openai_api_key,
            "meta": settings.muse_api_key,
            "mistral": settings.mistral_api_key,
        }
        out: list[dict] = []
        seen: set[str] = set()
        for spec in gateway.registry.models:
            if spec.id in seen:
                continue
            seen.add(spec.id)
            ready = (
                spec.billing != "api"
                or settings.model_api_keys.get(spec.provider) is not None
                or keys.get(spec.provider) is not None
            )
            out.append(
                {
                    "id": spec.id,
                    "tier": spec.tier,
                    "billing": spec.billing,
                    "configured": bool(ready),
                }
            )
        return out

    def _apply_model(session_id: str, request: NewSession | None) -> None:
        if request is None or request.model is None:
            return
        try:
            runner.set_model(session_id, request.model)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/sessions/primary")
    def primary_session(request: NewSession | None = None) -> dict[str, str]:
        require_current_binding()
        try:
            session_id = log.primary_session(request.session_id if request else None)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        _apply_model(session_id, request)
        hub.wake(session_id)
        return {"session_id": session_id}

    @app.post("/sessions", status_code=201)
    def create_session(request: NewSession | None = None) -> dict[str, str]:
        require_current_binding()
        session_id = (request.session_id if request else None) or uuid4().hex
        # A transport persists its chosen ID before this request. A lost HTTP
        # response must not create a second conversation on retry.
        if (
            log.append_once(session_id, "session.created", key=f"session-created:{session_id}")
            is not None
        ):
            hub.wake(session_id)
        _apply_model(session_id, request)
        return {"session_id": session_id}

    @app.post("/sessions/{session_id}/model")
    def set_session_model(session_id: str, request: NewSession) -> dict[str, str]:
        require_session(session_id)
        _apply_model(session_id, request)
        hub.wake(session_id)
        return {"session_id": session_id, "model": request.model or ""}

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> Snapshot:
        require_session(session_id)
        return runner.snapshot(session_id)

    @app.get("/sessions/{session_id}/source/{seq}")
    def get_source(session_id: str, seq: int) -> dict:
        """A cited message's whole conversation, for a client to open at that message."""
        require_session(session_id)
        events = log.read(session_id)
        chat = [
            turn for turn in log.snapshot(session_id, events).transcript if turn.task_id is None
        ]
        if not any(turn.seq == seq for turn in chat):
            raise HTTPException(404, "unknown source")
        cited = next(event for event in events if event.seq == seq)
        return {
            "session_id": session_id,
            "seq": seq,
            "date": cited.created_at.date().isoformat(),
            "turns": [turn.model_dump() for turn in chat],
        }

    @app.get("/sessions/{session_id}/tasks/{task_id}")
    def get_task(session_id: str, task_id: int) -> dict:
        require_session(session_id)
        assert runner.work_items is not None
        try:
            item = runner.work_items.get(session_id, task_id)
        except ModelRetry:
            raise HTTPException(404, "unknown task") from None
        return {
            "brief": item.brief.model_dump(),
            "task": runner.work_items.view(item).model_dump(),
            "conversation": runner.snapshot(item.session_id).model_dump(),
        }

    # Routes that write to the log are plain `def`, so FastAPI runs them on its thread pool and
    # a slow write never stalls the event loop that serves every stream. The runner hands
    # anything that launches work back to the loop itself (Runner._on_loop).
    @app.post("/sessions/{session_id}/messages", status_code=202)
    def post_message(session_id: str, message: NewMessage) -> dict[str, bool]:
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
    def edit_queued_message(session_id: str, message_id: str, message: EditMessage) -> dict:
        require_session(session_id)
        try:
            runner.edit_message(session_id, message_id, message.text)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"edited": True}

    @app.delete("/sessions/{session_id}/queue/{message_id}")
    def cancel_queued_message(session_id: str, message_id: str) -> dict:
        require_session(session_id)
        return {"cancelled": runner.cancel_message(session_id, message_id)}

    @app.post("/sessions/{session_id}/queue/pause")
    def pause_queue(session_id: str) -> dict:
        require_session(session_id)
        return {"changed": runner.pause_queue(session_id, True), "queue_paused": True}

    @app.post("/sessions/{session_id}/queue/resume")
    def resume_queue(session_id: str) -> dict:
        require_session(session_id)
        require_current_binding()
        return {"changed": runner.pause_queue(session_id, False), "queue_paused": False}

    @app.post("/sessions/{session_id}/stop", status_code=202)
    async def stop_session(session_id: str) -> dict:
        require_session(session_id)
        background.stop_commands(session_id)
        return await runner.stop_session(session_id)

    @app.post("/sessions/{session_id}/approvals/{approval_id}")
    def decide(session_id: str, approval_id: str, decision: Decision) -> dict[str, bool]:
        require_session(session_id)
        if decision.approved:
            require_current_binding()
        grant_tool = None
        if decision.approved and decision.remember:
            grant_tool = next(
                (
                    request.tool
                    for request in runner.snapshot(session_id).pending_approvals
                    if request.approval_id == approval_id
                ),
                None,
            )
            if grant_tool is None:
                raise HTTPException(404, "unknown approval")
            if not runner.harness.policy.grantable(grant_tool):
                raise HTTPException(409, "This action can never run without asking.")
        try:
            applied = runner.decide(
                session_id, approval_id, decision.approved, decision.origin.model_dump()
            )
        except UnknownApproval:
            raise HTTPException(404, "unknown approval") from None
        except DecisionRejected as exc:
            raise HTTPException(409, str(exc)) from None
        if applied and grant_tool is not None and grants.add(grant_tool):
            runner.harness.policy.grants = grants.tools
        return {"applied": applied}

    @app.get("/grants")
    def list_grants() -> dict[str, list[str]]:
        """Tools the owner allowed to run without asking again."""
        return {"tools": sorted(grants.tools)}

    @app.delete("/grants/{tool}")
    async def revoke_grant(tool: str) -> dict[str, bool]:
        """Revoke one standing permission; the tool asks again from the next call."""
        removed = grants.remove(tool)
        runner.harness.policy.grants = grants.tools
        return {"removed": removed}

    @app.post("/sessions/{session_id}/approvals/{approval_id}/revise")
    def revise(session_id: str, approval_id: str, message: NewMessage) -> dict[str, bool]:
        require_session(session_id)
        require_current_binding()
        try:
            return {
                "applied": runner.revise(
                    session_id,
                    approval_id,
                    message.text,
                    message.message_id or uuid4().hex,
                    message.origin.model_dump(),
                )
            }
        except QueueFull as exc:
            raise HTTPException(429, str(exc)) from None
        except DecisionRejected as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/sessions/{session_id}/approvals/{approval_id}/answer")
    def answer(session_id: str, approval_id: str, answer: Answer) -> dict[str, bool]:
        require_session(session_id)
        require_current_binding()
        try:
            return {
                "answered": runner.answer(
                    session_id,
                    approval_id,
                    answer.text,
                    answer.origin.model_dump(),
                    answer.answers,
                )
            }
        except UnknownApproval:
            raise HTTPException(404, "unknown approval") from None
        except DecisionRejected as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/sessions/{session_id}/events")
    async def stream_events(
        session_id: str,
        last_event_id: Annotated[int, Header()] = 0,
    ) -> StreamingResponse:
        require_session(session_id)

        async def frames() -> AsyncIterator[str]:
            # The response starts with its first chunk, so send one at once: a client
            # learns it is connected now, not at the first event or keep-alive.
            yield ": connected\n\n"
            async for event in hub.subscribe(session_id, after=last_event_id):
                yield ": keep-alive\n\n" if event is None else sse_frame(event)

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app
