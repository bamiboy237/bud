"""FastAPI app for the core.
It creates sessions, accepts messages, and streams session events as server-sent events.
`create_app` wires the log, hub, runner, and gateway together.
"""

import asyncio
import contextlib
import fcntl
import hashlib
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, TextIO
from uuid import uuid4

from dbos import DBOS
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr
from pydantic_ai import ModelRetry, Tool
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, FunctionToolset

from bud import __version__, browser
from bud.apps import CHANGES as APP_CHANGES
from bud.apps import GUIDANCE as APP_GUIDANCE
from bud.apps import OPEN_IN_APP, AppDispatch, quiet_open
from bud.apps import READS as APP_READS
from bud.apps import available as apps_available
from bud.apps import build_tools as build_app_tools
from bud.composio.binding import AccountBinding, valid_toolkit
from bud.composio.connection import ConnectionError, Connections
from bud.config import MOVE_FENCE, Settings, get_settings
from bud.core.auth import OwnerAuthentication, owner_token
from bud.core.background import GUIDANCE as BACKGROUND_GUIDANCE
from bud.core.background import STARTS as BACKGROUND_STARTS
from bud.core.background import TOOLS as BACKGROUND_TOOLS
from bud.core.background import Background
from bud.core.browser_computers import BrowserComputers
from bud.core.browser_delivery import local_routes
from bud.core.browser_view import BrowserView
from bud.core.composition import compose_runner, compose_world_harness
from bud.core.events import Event, MessageOrigin
from bud.core.handoff import DesktopHandoffs
from bud.core.hub import Hub
from bud.core.log import EventLog, QueueFull
from bud.core.onboarding import (
    ChatGPTLoginManager,
    DefaultModelChoice,
    OnboardingError,
    onboarding_snapshot,
    reload_settings,
    resolve_config_env_path,
    write_env_updates,
)
from bud.core.paired_apps import PairedApps
from bud.core.profiles import Mode, ProfileStore, apply_mode, as_mode, mode_summary
from bud.core.projection import (
    Attachment,
    RoutineBudget,
    RoutineDestination,
    Snapshot,
    parked_approvals,
    undecided_approvals,
)
from bud.core.routines import (
    DEFAULT_APPROVAL_EXPIRY_SECONDS,
    RECENT_RUN_LIMIT,
    MissedRunPolicy,
    OverlapPolicy,
    RoutineConflictError,
    RoutineNotFoundError,
    RoutineValidationError,
)
from bud.core.runner import (
    ACCOUNT_CHANGE,
    ApprovalExpired,
    DecisionRejected,
    Runner,
    StaleApproval,
    UnknownApproval,
)
from bud.core.runtime_receiver import create_receiver
from bud.core.setup import (
    SetupJournal,
    SetupRegistry,
    build_setup_toolset,
    check_setup,
    setup_registry,
)
from bud.core.workspace_home import display_path, workspace_home
from bud.gateway import Gateway, NoModelAvailable, build_gateway
from bud.gateway.chatgpt import ChatGPTError
from bud.gateway.router import provider_model_factory
from bud.harness.actions import Policy, load_policy
from bud.harness.agent import Harness
from bud.harness.ask import MAX_QUESTIONS, AskAnswer
from bud.harness.grants import Grants
from bud.harness.memory import build_memory
from bud.harness.operating_profile import (
    OperatingProfile,
    OperatingProfileStore,
    resolve_operating_profile,
)
from bud.harness.preferences import PreferenceStore
from bud.harness.review import ToolReviewer, build_reviewer
from bud.harness.taint import KnownAddresses
from bud.limits import MAX_MESSAGE_CHARS
from bud.logs import guard_logs
from bud.photon_config import PhotonConfig
from bud.release.move_archive import RuntimeMoveError
from bud.shell import GUIDANCE as SHELL_GUIDANCE
from bud.shell import NETWORK_TOOL, SHELL_TOOLS, NetworkGrants
from bud.shell import available as shell_available
from bud.shell import build_tools as build_shell_tools
from bud.shell import runner as shell_runner
from bud.web import GUIDANCE as WEB_GUIDANCE
from bud.web import READ_TOOLS as WEB_READ_TOOLS
from bud.web import build_tools as build_web_tools
from bud.workspace import (
    ATTACHMENT_BYTES,
    DOCUMENT_TOOL,
    LOCAL_GUIDANCE,
    LOCAL_TOOLS,
    describe_local,
    document_tool,
    local_blocked,
    local_refusal,
    local_tools,
    preview_document,
    read_workspace_bytes,
    read_workspace_text,
    save_attachment,
)
from bud.workspace import GUIDANCE as WORKSPACE_GUIDANCE
from bud.workspace import READ_TOOLS as WORKSPACE_READ_TOOLS
from bud.workspace import WRITE_TOOLS as WORKSPACE_WRITE_TOOLS
from bud.workspace import build_tools as build_workspace_tools
from bud.workspace import read_capabilities as workspace_read_capabilities
from bud.world.store import World

logger = logging.getLogger(__name__)
# Workspace reads that never ask: the harness FileSystem tools and read_document.
FILE_READS = WORKSPACE_READ_TOOLS | {DOCUMENT_TOOL}


def _noop() -> None:
    return None


class ModeRequest(BaseModel):
    mode: str


class OnboardingKeysRequest(BaseModel):
    keys: dict[str, str | None] = Field(default_factory=dict)


class DefaultModelRequest(BaseModel):
    model: str | None


class NewMessage(BaseModel):
    text: str = Field(max_length=MAX_MESSAGE_CHARS)
    attachments: list[Attachment] = Field(default_factory=list, max_length=5)
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
    refresh: bool = True


class NewRoutine(BaseModel):
    feed_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=120)
    skill: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=10_000)
    cron: str = Field(min_length=1, max_length=120)
    timezone: str = "UTC"
    missed_run_policy: MissedRunPolicy = "run_once"
    overlap_policy: OverlapPolicy = "skip"
    approval_expiry_seconds: int | None = Field(default=DEFAULT_APPROVAL_EXPIRY_SECONDS, ge=1)
    budget: RoutineBudget = Field(default_factory=RoutineBudget)
    destination: RoutineDestination | None = None
    source_id: str | None = Field(default=None, min_length=1, max_length=128)


class EditRoutine(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    skill: str | None = Field(default=None, min_length=1, max_length=64)
    prompt: str | None = Field(default=None, min_length=1, max_length=10_000)
    cron: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = None
    missed_run_policy: MissedRunPolicy | None = None
    overlap_policy: OverlapPolicy | None = None
    approval_expiry_seconds: int | None = Field(default=None, ge=1)
    clear_approval_expiry: bool = False
    budget: RoutineBudget | None = None
    destination: RoutineDestination | None = None


class RunNowRequest(BaseModel):
    trigger_id: str | None = Field(default=None, min_length=1, max_length=128)


class ResumeRoutineRequest(BaseModel):
    acknowledge_uncertain: bool = False


class ConfirmDeliveryRequest(BaseModel):
    receipt: str | None = Field(default=None, max_length=256)


def sse_frame(event: Event) -> str:
    # Live text has no stored position; only stored events move the reconnect cursor.
    cursor = "" if event.live else f"id: {event.seq}\n"
    payload = event.model_dump_json(exclude={"data": {"model_fallbacks"}})
    return f"{cursor}event: {event.type}\ndata: {payload}\n\n"


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
    app_dispatch: AppDispatch | None = None,
) -> FunctionToolset[None]:
    """File tools, and on macOS the sandboxed shell, whose network checks use `reviewer`.
    Shell commands cannot read `private` folders, such as Bud's data and its .env."""
    grants = NetworkGrants(state_dir / "network-grants.json")
    shell = build_shell_tools(root, grants, reviewer, private) if shell_available() else []
    apps = (
        build_app_tools(root, state_dir / "open-copies", dispatch=app_dispatch)
        if apps_available() or app_dispatch is not None
        else []
    )
    return FunctionToolset(
        [
            *build_workspace_tools(root, state_dir / "workspace-dispatches.jsonl"),
            document_tool(root),
            *local_tools(local_blocked(Path.home(), list(private), root)),
            *shell,
            *(background.tools() if background is not None else []),
            *apps,
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
    origin = {"channel": "api", "client_id": ACCOUNT_CHANGE}
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
    folders: list[Path] = [settings.data_dir]
    home = Path.home().resolve()
    cwd = Path.cwd()
    cwd_resolved = cwd.resolve()
    if cwd_resolved != home and not home.is_relative_to(cwd_resolved):
        folders.append(cwd)
    bud_home = os.environ.get("BUD_HOME", "").strip()
    if bud_home:
        candidate = Path(bud_home).expanduser()
        candidate_resolved = candidate.resolve()
        if (
            candidate_resolved != home
            and not home.is_relative_to(candidate_resolved)
            and candidate_resolved not in {path.resolve() for path in folders}
        ):
            folders.append(candidate)
    return folders


def file_guidance() -> str:
    return (
        WORKSPACE_GUIDANCE
        + LOCAL_GUIDANCE
        + (SHELL_GUIDANCE if shell_available() else "")
        + (APP_GUIDANCE if apps_available() else "")
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
    # Mac apps: reads run unasked; changes ask, or go to the reviewer in auto mode.
    policy.tools.update(dict.fromkeys(APP_READS, "allow"))
    policy.tools.update(dict.fromkeys(APP_CHANGES, "ask"))
    # A quiet document opens unasked (harness.harmless); one whose app can reach the network
    # asks every time.
    policy.tools[OPEN_IN_APP] = "ask"
    policy.no_grants = policy.no_grants | {OPEN_IN_APP}
    policy.tools[NETWORK_TOOL] = "ask"
    # Each read outside the workspace asks, in every mode, and offers no "always allow".
    policy.tools.update(dict.fromkeys(LOCAL_TOOLS, "ask"))
    # Each new site needs its own yes: "always allow" would let Bud add any site unasked.
    policy.no_grants = policy.no_grants | {NETWORK_TOOL} | LOCAL_TOOLS


def create_app(
    *,
    simulate: bool = False,
    model_id: str | None = None,
    operating_profile: OperatingProfile | None = None,
    activate_move: Callable[[], None] | None = None,
) -> FastAPI:
    settings = get_settings()
    token = owner_token(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # This lock spans all account generations, before any live sessions, tools,
    # recovery or runners can be constructed. A scoped DB lock alone is too late.
    live_lock = (
        None if simulate else hold_single_server_lock(settings.data_dir / "live-server.lock")
    )
    try:
        if (settings.data_dir / MOVE_FENCE).exists():
            raise RuntimeMoveError("This Bud runtime is stopped for a move to another server.")
        if settings.receive_move and settings.received_move_id is None:
            if live_lock is None:
                raise RuntimeMoveError("A simulated server cannot receive a Bud runtime.")
            return create_receiver(settings, token, live_lock, activate_move)
        return _create_app(
            settings,
            token,
            live_lock,
            simulate=simulate,
            model_id=model_id,
            operating_profile=operating_profile,
        )
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
    operating_profile: OperatingProfile | None = None,
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
    browse_home = (
        settings.browse_home.expanduser()
        if settings.browse_home is not None
        else browser.RELAY_HOME
    )
    relay = browser.Relay(browse_home, port=settings.browse_port)
    pairing = browser.Pairing(relay)
    browser_view: BrowserView | None = None

    # Set by the account branch: reloads the tool list after a new app connects.
    refresh_tools: Callable[[], None] = _noop
    refresh_lock = Lock()
    loaded: dict[str, Any] = {}
    account_toolset: (
        Callable[[AccountBinding | None, Path | None], CombinedToolset[None]] | None
    ) = None
    paired_apps: PairedApps

    def paired_macs() -> list[dict]:
        """List paired Macs and their IDs. Use the exact ID in each Mac app action."""
        return paired_apps.devices()

    async def remote_app_action(
        mac: str | None, action: str, args: list[str], file: bytes | None
    ) -> dict:
        return await paired_apps.dispatch(mac, action, args, file)

    # The approval mode applies to the harness policy after composition (the
    # Harness unions memory/skill rules in), one seam for every tool class.
    profile_store = ProfileStore(settings.data_dir / "approval-mode.json")
    approval_mode = profile_store.load()
    active_profile = operating_profile or resolve_operating_profile(
        settings.operating_profile,
        store=OperatingProfileStore(settings.data_dir / "operating-profile.json"),
    )

    def build_runtime_gateway(current: Settings) -> Gateway:
        providers = (
            None if simulate else {"openai-chatgpt", "openai", "meta", "mistral", "deepseek"}
        )
        if model_id is not None or current.default_model is None:
            return build_gateway(current, providers=providers, model_id=model_id)
        # Keep the full catalog for Settings and use DEFAULT_MODEL only as the default route.
        candidate = build_gateway(
            current.model_copy(update={"default_model": None}), providers=providers
        )
        candidate.selected_model = current.default_model
        return candidate

    retention = (
        timedelta(hours=settings.tool_result_retention_hours)
        if settings.tool_result_retention_hours != 24
        else None
    )
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
        gateway = build_runtime_gateway(settings)
        tools_policy = policy()
        tools_policy.tools.update(dict.fromkeys(WEB_READ_TOOLS, "allow"))
        allow_file_tools(tools_policy)
        use_paired_mac = not apps_available()
        if use_paired_mac:
            tools_policy.tools.update({"paired_macs": "allow", "open_app": "ask"})
            tools_policy.always_ask |= APP_CHANGES | {"open_app", OPEN_IN_APP}
        tools_policy.tools["setup_capability"] = "allow"
        browsing = settings.browser_tools
        consent = browser.Consent(settings.data_dir / "browser-consent.json")
        if browsing:
            browser_view = BrowserView(settings.data_dir / "browser-private", consent, relay)
        setup = setup_registry(connections)
        if browsing:
            browser.allow_tools(tools_policy)
            setup.register("browser", browser.setup_entry(pairing))
        setup_journal = SetupJournal()
        loaded = {
            "binding": binding,
            "accounts": frozenset(accounts),
            "browse": browser.executable() if browsing else None,
        }

        def browser_toolsets(browse: Path | None) -> list[AbstractToolset[None]]:
            if browse is None:
                return []
            return [
                browser.toolset(browse, consent, relay, browser_view),
                FunctionToolset(browser.control_tools(consent, relay, view=browser_view)),
            ]

        def _account_toolset(
            binding: AccountBinding | None, browse: Path | None
        ) -> CombinedToolset[None]:
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
                        app_dispatch=remote_app_action if use_paired_mac else None,
                    ),
                    *([FunctionToolset([Tool(paired_macs)])] if use_paired_mac else []),
                    build_setup_toolset(setup, setup_journal, refresh=refresh_tools),
                    *browser_toolsets(browse),
                ]
            )

        account_toolset = _account_toolset

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
                replacement = _account_toolset(binding, browse)
                connections.adopt(binding)
                harness.swap_tools(replacement)
                previous = loaded["binding"]
                loaded.update(binding=binding, browse=browse, accounts=frozenset(binding.accounts))
                if previous is None or binding.generation != previous.generation:
                    # After the swap, so a run resumed by a denial enters the new tools.
                    deny_stale_approvals(binding.generation)

        refresh_tools = refresh_account_tools
        memory_path = session_dir / "memory.db"
        memory = build_memory(memory_path, namespace="owner")
        harness = Harness(
            _account_toolset(binding, loaded["browse"]),
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
            + (
                "\nMac app actions run on a paired Mac. Call paired_macs to get the Mac's ID. "
                "Specify that ID in every app action. If a result is uncertain, verify what "
                "happened instead of repeating it.\n"
                if use_paired_mac
                else ""
            )
            + (browser.GUIDANCE if browsing else "")
            + f"Your workspace is the folder {display_path(workspace)} on the machine running "
            "the Bud server. Local file paths resolve on that machine, even when the person "
            "chats from another device.\n",
            memory=memory,
            preferences=PreferenceStore(memory_path, memory=memory),
            state_dir=session_dir,
            result_path=db_path,
            read_tools=READ_TOOLS
            | WEB_READ_TOOLS
            | WORKSPACE_READ_TOOLS
            | {DOCUMENT_TOOL, "current_time"},
            result_retention=retention,
            capabilities=workspace_read_capabilities(
                workspace, deferred=active_profile.harness.defer_capabilities
            ),
            operating_profile=active_profile,
        )
        harness.known = KnownAddresses(session_dir / "known-addresses")
        tools_policy = harness.policy
        if use_paired_mac:

            def describe_mac(args: dict) -> str:
                name = next(
                    (mac["name"] for mac in paired_apps.devices() if mac["id"] == args.get("mac")),
                    None,
                )
                return f"On {name}" if name else "No paired Mac selected"

            harness.describers.update(
                dict.fromkeys(APP_CHANGES | {"open_app", OPEN_IN_APP}, describe_mac)
            )
        if browsing:
            harness.describers.update(browser.describers(relay, view=browser_view))
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
        gateway = build_runtime_gateway(settings)
        world = World(settings.data_dir / "world.db")
        tools_policy = load_policy()
        allow_file_tools(tools_policy)
        harness_ref: list[Harness] = []
        harness = compose_world_harness(
            world,
            gateway,
            settings,
            state_dir=settings.data_dir,
            result_path=db_path,
            memory_path=settings.data_dir / "world-memory.db",
            operating_profile=active_profile,
            extra_toolsets=[
                workspace_tools(
                    workspace,
                    settings.data_dir,
                    lambda: harness_ref[0].reviewer,
                    private_folders(settings),
                    background,
                )
            ],
            policy=tools_policy,
            extra_read_tools=FILE_READS,
            prompt_suffix=WEB_GUIDANCE + file_guidance(),
            result_retention=retention,
            capabilities=workspace_read_capabilities(
                workspace, deferred=active_profile.harness.defer_capabilities
            ),
            known_addresses=KnownAddresses(settings.data_dir / "known-addresses"),
        )
        harness_ref.append(harness)
    # The card names the real file a local read opens, which a link cannot disguise.
    if apps_available():
        harness.harmless[OPEN_IN_APP] = quiet_open
    outside = local_blocked(Path.home(), private_folders(settings), workspace)
    harness.describers.update(
        dict.fromkeys(LOCAL_TOOLS, lambda tool_args: describe_local(tool_args, outside))
    )
    harness.refuses.update(
        dict.fromkeys(LOCAL_TOOLS, lambda tool_args: local_refusal(tool_args, outside))
    )
    reviewer = build_reviewer(settings)
    has_reviewer = reviewer is not None
    # Standing permissions the owner granted from an approval card. The policy reads them on
    # every call, so a new grant or a revoke applies without a restart.
    grants = Grants(settings.data_dir / "grants.json")
    live_mode: dict[str, Mode] = {"mode": approval_mode}
    allow_unconfigured = not simulate and model_id is None
    if not allow_unconfigured:
        gateway.model(
            active_profile.model.tier,
            require_tools=True,
            selected=active_profile.model.model_id,
        )
    else:
        try:
            gateway.model(
                active_profile.model.tier,
                require_tools=True,
                selected=active_profile.model.model_id,
            )
        except NoModelAvailable:
            logger.info("No model configured yet; starting server in onboarding mode.")
    log = EventLog(db_path)
    hub = Hub(log)
    background.bind(hub)
    runner = compose_runner(
        hub,
        gateway,
        harness,
        log,
        settings,
        approval_mode=approval_mode,
        recall_path=db_path.parent / ("world-recall.db" if world is not None else "recall.db"),
        reviewer=reviewer,
        grants=grants.tools,
        durable=True,
        jobs=True,
        workspace=workspace,
        include_history_search=True,
        mode_holder=live_mode,
        runner_cls=Runner,
        allow_unconfigured=allow_unconfigured,
    )
    config_env_path = resolve_config_env_path(settings)
    model_choice = DefaultModelChoice(settings.data_dir / "default-model")
    model_locked = bool(model_id or active_profile.model.model_id)

    def default_model_for(candidate: Gateway) -> str | None:
        choice = None if model_locked else model_choice.read()
        if choice:
            with contextlib.suppress(NoModelAvailable):
                return candidate.validate_model(choice, require_tools=True).id
        return None

    runner.owner_model = default_model_for(gateway)

    def reload_runtime_config(
        *, cleared_keys: set[str] | None = None, chatgpt_auth_changed: bool = False
    ) -> None:
        nonlocal settings, gateway, has_reviewer, config_env_path
        with refresh_lock:
            old_gateway = gateway
            new_gateway = None
            try:
                new_settings = reload_settings(settings, config_env_path, cleared_keys=cleared_keys)
                new_gateway = build_runtime_gateway(new_settings)
                chatgpt = old_gateway.chatgpt
                if (
                    chatgpt is not None
                    and new_gateway.chatgpt is not None
                    and chatgpt.folder == new_gateway.chatgpt.folder
                ):
                    # Reuse the live store's credential lock while validating the candidate.
                    new_gateway.chatgpt = chatgpt
                    new_gateway.build_model = provider_model_factory(new_settings, chatgpt)
                new_reviewer = build_reviewer(new_settings)
                new_config_env_path = resolve_config_env_path(new_settings)
                policy = harness.policy.model_copy(update={"tools": harness.policy.tools.copy()})
                apply_mode(policy, live_mode["mode"], reviewer=new_reviewer is not None)
                runner.reload_gateway(
                    new_gateway,
                    reviewer=new_reviewer,
                    owner_model=default_model_for(new_gateway),
                    policy_tools=policy.tools,
                )
            except Exception:
                if new_gateway is not None and new_gateway is not old_gateway:
                    new_gateway.close(preserve_chatgpt=old_gateway.chatgpt)
                raise OnboardingError("Bud could not reload the model configuration.") from None
            settings = new_settings
            gateway = new_gateway
            has_reviewer = new_reviewer is not None
            config_env_path = new_config_env_path
            if old_gateway is not new_gateway:
                old_gateway.close(preserve_chatgpt=new_gateway.chatgpt)
            if connections is not None and account_toolset is not None:
                connections.settings = new_settings
                current_binding = connections.binding or connections.stored()
                harness.swap_tools(account_toolset(current_binding, loaded.get("browse")))
            if chatgpt_auth_changed:
                try:
                    choice = model_choice.read()
                    connected = new_gateway.chatgpt is not None and new_gateway.chatgpt.connected
                    if not connected and choice and choice.startswith("openai-chatgpt:"):
                        model_choice.write(None)
                    elif connected and not model_locked and choice is None:
                        selected = next(
                            (
                                model
                                for model in new_gateway.selectable_models(require_tools=True)
                                if model.startswith("openai-chatgpt:")
                            ),
                            None,
                        )
                        if selected is not None:
                            model_choice.write(selected)
                            runner.owner_model = selected
                except OSError:
                    raise OnboardingError("Bud could not save the default model.") from None

    chatgpt_login = ChatGPTLoginManager(
        settings.data_dir,
        on_before_auth=lambda: gateway.close(),
        on_complete=lambda: reload_runtime_config(chatgpt_auth_changed=True),
    )
    if setup_journal is not None:
        # The setup toolset was built before the log, hub, and runner existed; one seam
        # hands its journal over so every setup event is journaled and wakes the runner.
        setup_journal.bind(hub, on_wakeup=runner.wake_setup)

    routine_wake = asyncio.Event()
    runner.bind_routine_wake(routine_wake)

    async def every_minute() -> None:
        loop = asyncio.get_running_loop()
        last_housekeeping = loop.time() - 60.0
        while True:
            routine_wake.clear()
            now_mono = loop.time()
            if now_mono - last_housekeeping >= 60.0:
                last_housekeeping = now_mono
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
            try:
                runner.tick_routines()
            except Exception:
                logger.exception("Routine tick failed")
            delay = min(runner.next_routine_delay(default_seconds=30.0), 30.0)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(routine_wake.wait(), timeout=delay)

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
            await runner.recover()
            if connections is not None and connections.binding is not None:
                # Cards asked under an account replaced while Bud was stopped close now. After
                # recovery, so a run a denial resumes is launched once. A recovered run that
                # reaches such a card meanwhile is refused at dispatch (stale_approval).
                deny_stale_approvals(connections.binding.generation)
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
                    await chatgpt_login.shutdown()
                    # Stop accepting/sending messages before stopping core work.
                    await services.aclose()
                finally:
                    if cleanup is not None:
                        cleanup.cancel()
                        await asyncio.gather(cleanup, return_exceptions=True)
                    await background.close()
                    if browser_view is not None:
                        await browser_view.stop()
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
    app.state.chatgpt_login = chatgpt_login
    app.state.reload_runtime_config = reload_runtime_config
    if browser_view is not None:
        app.include_router(browser_view.routes())
        app.include_router(
            BrowserComputers(browser_view, pairing, settings.browse_peer_ip).routes()
        )
        app.include_router(local_routes(browser_view))
    handoffs = DesktopHandoffs(db_path.with_suffix(".handoff.db"), workspace, hub)
    app.include_router(handoffs.routes())
    paired_apps = PairedApps(handoffs)
    app.include_router(paired_apps.routes())
    app.add_middleware(
        OwnerAuthentication, token=token, scope_id=scope_id, desktop_worker=handoffs.authorized
    )

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
        has_model = runner.is_configured
        return JSONResponse(
            {
                "version": __version__,
                "mode": "simulate" if simulate else "accounts",
                "scope": scope_id,
                "received_move_id": settings.received_move_id,
                "has_model": has_model,
                "needs_onboarding": not simulate and not has_model,
                "restart_required": connections is not None and connections.restart_required(),
                "messaging": messaging_state["status"],
                "approval_mode": profile_store.load(),
                "operating_profile": active_profile.attribution(),
            },
            headers={"Cache-Control": "no-store"},
        )

    def _onboarding_payload() -> JSONResponse:
        manager: ChatGPTLoginManager = app.state.chatgpt_login
        with refresh_lock:
            payload = onboarding_snapshot(
                settings,
                gateway,
                manager,
                has_model=runner.is_configured,
                simulate=simulate,
                active_model=runner.active_model_id,
                default_model=runner.owner_model,
                model_locked=model_locked,
            )
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    def _validate_model(model_id: str | None) -> None:
        if model_id is not None:
            try:
                gateway.validate_model(model_id, require_tools=True)
            except NoModelAvailable as exc:
                raise HTTPException(422, str(exc)) from None

    @app.get("/onboarding")
    def get_onboarding() -> JSONResponse:
        return _onboarding_payload()

    @app.put("/onboarding/model")
    def select_default_model(body: DefaultModelRequest) -> JSONResponse:
        if model_locked:
            raise HTTPException(409, "The server configuration sets the default model.")
        with refresh_lock:
            _validate_model(body.model)
            model_choice.write(body.model)
            runner.owner_model = body.model
        return _onboarding_payload()

    @app.post("/onboarding/keys")
    def update_onboarding_keys(body: OnboardingKeysRequest) -> JSONResponse:
        if not body.keys:
            return _onboarding_payload()
        try:
            cleared = write_env_updates(config_env_path, body.keys)
            reload_runtime_config(cleared_keys=cleared)
        except OnboardingError as exc:
            raise HTTPException(422, str(exc)) from None
        return _onboarding_payload()

    @app.post("/onboarding/chatgpt/start")
    async def start_chatgpt_onboarding() -> JSONResponse:
        manager: ChatGPTLoginManager = app.state.chatgpt_login
        try:
            await manager.start()
        except (ChatGPTError, OnboardingError) as exc:
            raise HTTPException(503, str(exc)) from None
        return _onboarding_payload()

    @app.post("/onboarding/chatgpt/cancel")
    async def cancel_chatgpt_onboarding() -> JSONResponse:
        manager: ChatGPTLoginManager = app.state.chatgpt_login
        await manager.cancel()
        return _onboarding_payload()

    @app.post("/onboarding/chatgpt/logout")
    async def logout_chatgpt_onboarding() -> JSONResponse:
        manager: ChatGPTLoginManager = app.state.chatgpt_login
        try:
            await manager.logout()
        except (ChatGPTError, OnboardingError) as exc:
            raise HTTPException(503, str(exc)) from None
        return _onboarding_payload()

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
        with refresh_lock:
            profile_store.save(target)
            overlay = apply_mode(harness.policy, target, reviewer=has_reviewer)
            live_mode["mode"] = target
            summary = mode_summary(target, reviewer=has_reviewer)
        auto = sum(1 for action in overlay.values() if action == "review")
        return JSONResponse(
            {
                "mode": target,
                "auto_writes": auto,
                "summary": summary,
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
            statuses, accounts = current.inventory()
            refresh_tools()  # any change, a reconnect included, loads in this chat
            restart_required = current.restart_required()
        except ConnectionError as exc:
            raise HTTPException(503, str(exc)) from None
        return JSONResponse(
            {
                "statuses": {"gmail": statuses["gmail"], "calendar": statuses["googlecalendar"]},
                "accounts": accounts,
                "restart_required": restart_required,
                "refresh_boundary": "process_restart" if restart_required else "next_message",
            },
            headers={"Cache-Control": "no-store"},
        )

    def connection_toolkit(toolkit: str) -> str:
        name = {"calendar": "googlecalendar"}.get(toolkit, toolkit)
        if not valid_toolkit(name):
            raise HTTPException(400, "Use a valid app id.")
        return name

    @app.post("/connections/{toolkit}/connect")
    def connect_account(toolkit: str, request: ConnectRequest | None = None) -> JSONResponse:
        current = live_connections()
        name = connection_toolkit(toolkit)
        resume = bool(request and request.resume)
        try:
            url = current.pending_url(name) if resume else None
            if not resume:
                url = current.connect(name)
            elif url is None and request and request.refresh:
                url = current.connect(name, replace_active=False)
            refresh_tools()  # a reconnect retires the old account at once, in place
            restart_required = current.restart_required()
        except ConnectionError as exc:
            raise HTTPException(503, str(exc)) from None
        # Keep sign-in links out of session events, model history, status metadata and logs.
        return JSONResponse(
            {
                "url": url,
                "expired": url is None,
                "restart_required": restart_required,
                "refresh_boundary": "process_restart" if restart_required else "next_message",
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/connections/{toolkit}/disconnect")
    def disconnect_account(toolkit: str) -> JSONResponse:
        current = live_connections()
        name = connection_toolkit(toolkit)
        try:
            removed = current.disconnect(name)
            refresh_tools()
            restart_required = current.restart_required()
        except ConnectionError as exc:
            raise HTTPException(503, str(exc)) from None
        return JSONResponse(
            {
                "disconnected": removed,
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
        version = setup_journal.status_version(session_id, capability)
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
        if not setup_journal.update_status(
            session_id,
            data,
            version,
            key=f"owner-setup-cancel:{uuid4().hex}",
        ):
            current = setup_journal.pending(session_id).get(capability)
            if current is not None:
                return JSONResponse(
                    {
                        "capability": capability,
                        "label": current.label,
                        "status": current.status,
                        **({"reason": current.reason} if current.reason else {}),
                    },
                    headers={"Cache-Control": "no-store"},
                )
            latest = next(
                (
                    event.data
                    for event in reversed(log.read(session_id))
                    if event.type == "capability.status"
                    and event.data.get("capability") == capability
                ),
                None,
            )
            if latest is not None:
                return JSONResponse(
                    {
                        "capability": capability,
                        "label": latest.get("label", card.label),
                        "status": latest.get("status", "cancelled"),
                    },
                    headers={"Cache-Control": "no-store"},
                )
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.get("/models")
    def list_models() -> list[dict]:
        with refresh_lock:
            return [
                {
                    "id": status.id,
                    "tier": status.tier,
                    "billing": status.billing,
                    "configured": status.selectable,
                }
                for status in gateway.catalog(require_tools=True)
            ]

    def _apply_model(session_id: str, model_id: str | None) -> None:
        try:
            runner.set_model(session_id, model_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/sessions")
    def conversation_history(offset: int = 0) -> dict:
        if offset < 0:
            raise HTTPException(422, "Invalid history page.")
        return {"conversations": log.conversation_history(offset)}

    @app.post("/sessions/primary")
    def primary_session(request: NewSession | None = None) -> dict[str, str]:
        require_current_binding()
        with refresh_lock:
            _validate_model(request.model if request else None)
            try:
                session_id = log.primary_session(request.session_id if request else None)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            if request is not None and request.model is not None:
                _apply_model(session_id, request.model)
        hub.wake(session_id)
        return {"session_id": session_id}

    @app.post("/sessions", status_code=201)
    def create_session(request: NewSession | None = None) -> dict[str, str]:
        require_current_binding()
        session_id = (request.session_id if request else None) or uuid4().hex
        # A transport persists its chosen ID before this request. A lost HTTP
        # response must not create a second conversation on retry.
        with refresh_lock:
            _validate_model(request.model if request else None)
            created = log.append_once(
                session_id, "session.created", key=f"session-created:{session_id}"
            )
            if request is not None and request.model is not None:
                _apply_model(session_id, request.model)
        if created is not None:
            hub.wake(session_id)
        return {"session_id": session_id}

    @app.post("/sessions/{session_id}/model")
    def set_session_model(session_id: str, request: NewSession) -> dict[str, str]:
        require_session(session_id)
        if "model" in request.model_fields_set:
            with refresh_lock:
                _apply_model(session_id, request.model)
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

    @app.get("/workspace/source")
    def get_workspace_source(path: str) -> dict:
        """A cited workspace file's text, enforcing workspace privacy and link rules."""
        found = read_workspace_text(workspace, path)
        if found is None:
            raise HTTPException(404, "unknown source")
        content, info = found
        return {
            "kind": "workspace",
            "path": path,
            "date": datetime.fromtimestamp(info.st_mtime_ns / 1e9, tz=UTC).date().isoformat(),
            "content": content,
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

    @app.put("/sessions/{session_id}/attachments/{attachment_id}", status_code=204)
    async def upload_attachment(session_id: str, attachment_id: str, name: str, request: Request):
        require_session(session_id)
        require_current_binding()
        try:
            attachment = Attachment(id=attachment_id, name=name)
        except ValueError as exc:
            raise HTTPException(422, "Invalid attachment ID or filename.") from exc
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > ATTACHMENT_BYTES:
                raise HTTPException(413, "Attachments are limited to 25 MiB each.")
            content.extend(chunk)
        try:
            await asyncio.to_thread(
                save_attachment, workspace, attachment.path(session_id), bytes(content)
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return Response(status_code=204)

    @app.get("/sessions/{session_id}/attachments/{attachment_id}")
    def get_attachment(session_id: str, attachment_id: str, name: str, preview: bool = False):
        require_session(session_id)
        try:
            attachment = Attachment(id=attachment_id, name=name)
        except ValueError as exc:
            raise HTTPException(404, "Unknown attachment.") from exc
        data = read_workspace_bytes(workspace, attachment.path(session_id), limit=ATTACHMENT_BYTES)
        if data is None:
            raise HTTPException(404, "Unknown attachment.")
        if preview:
            converted = preview_document(data, name)
            if not converted["ok"]:
                raise HTTPException(422, converted["error"])
            return JSONResponse(converted)
        return Response(
            data,
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    # Routes that write to the log are plain `def`, so FastAPI runs them on its thread pool and
    # a slow write never stalls the event loop that serves every stream. The runner hands
    # anything that launches work back to the loop itself (Runner._on_loop).
    @app.post("/sessions/{session_id}/messages", status_code=202)
    def post_message(session_id: str, message: NewMessage) -> dict[str, bool]:
        require_session(session_id)
        require_current_binding()
        if any(
            read_workspace_bytes(workspace, file.path(session_id), limit=ATTACHMENT_BYTES) is None
            for file in message.attachments
        ):
            raise HTTPException(422, "An attachment is unavailable in this conversation.")
        try:
            event = runner.start(
                session_id,
                message.text,
                message.message_id or uuid4().hex,
                message.origin.model_dump(),
                attachments=message.attachments,
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

    @app.post("/sessions/{session_id}/stop-response", status_code=202)
    async def stop_session_response(session_id: str) -> dict:
        require_session(session_id)
        return await runner.stop_session(session_id, include_children=False)

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
        except (DecisionRejected, ApprovalExpired, StaleApproval) as exc:
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
        except (DecisionRejected, ApprovalExpired) as exc:
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
        except (DecisionRejected, ApprovalExpired) as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    def _require_routines():
        if runner.routines is None:
            raise HTTPException(409, "Routines are disabled on this server.")
        return runner.routines

    @app.post("/routines", status_code=201)
    def create_routine(body: NewRoutine) -> dict[str, Any]:
        store = _require_routines()
        require_current_binding()
        try:
            routine = runner.create_routine(
                feed_id=body.feed_id,
                title=body.title,
                skill=body.skill,
                prompt=body.prompt,
                cron=body.cron,
                timezone=body.timezone,
                missed_run_policy=body.missed_run_policy,
                overlap_policy=body.overlap_policy,
                approval_expiry_seconds=body.approval_expiry_seconds,
                budget=body.budget,
                destination=body.destination,
                source_id=body.source_id,
            )
        except RoutineValidationError as exc:
            raise HTTPException(422, str(exc)) from None
        except RoutineConflictError as exc:
            raise HTTPException(409, str(exc)) from None
        return store.view(routine).model_dump()

    @app.get("/routines")
    def list_routines(feed_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        store = _require_routines()
        return {"routines": [view.model_dump() for view in store.views(feed_id)]}

    @app.get("/routines/{routine_id}")
    def get_routine(
        routine_id: str,
        offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = RECENT_RUN_LIMIT,
    ) -> dict[str, Any]:
        store = _require_routines()
        try:
            routine = store.get(routine_id)
        except RoutineNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        recent, has_more = store.occurrence_page(routine_id, limit=limit, offset=offset)
        return {
            **store.view(routine, recent=recent, offset=offset).model_dump(),
            "has_more": has_more,
        }

    @app.get("/routines/{routine_id}/occurrences")
    def list_routine_occurrences(
        routine_id: str,
        offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = RECENT_RUN_LIMIT,
    ) -> dict[str, Any]:
        store = _require_routines()
        try:
            store.get(routine_id)
        except RoutineNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        occurrences, has_more = store.occurrence_page(routine_id, limit=limit, offset=offset)
        return {
            "occurrences": [store.run_view(occ).model_dump() for occ in occurrences],
            "has_more": has_more,
        }

    @app.get("/routines/{routine_id}/occurrences/{occurrence_id}/report")
    def get_routine_report(routine_id: str, occurrence_id: str) -> dict[str, str]:
        store = _require_routines()
        try:
            occurrence = store.occurrence(occurrence_id)
        except RoutineNotFoundError:
            raise HTTPException(404, "Unknown routine run.") from None
        if occurrence.routine_id != routine_id or not occurrence.report_path:
            raise HTTPException(404, "No report for this routine run.")
        relative = Path(occurrence.report_path)
        if relative.is_absolute():
            try:
                relative = relative.relative_to(workspace)
            except ValueError:
                raise HTTPException(404, "Report is outside Bud's workspace.") from None
        found = read_workspace_text(workspace, relative.as_posix())
        if found is None:
            raise HTTPException(404, "The saved report is unavailable.")
        return {"title": relative.name, "content": found[0]}

    @app.patch("/routines/{routine_id}")
    def edit_routine(routine_id: str, body: EditRoutine) -> dict[str, Any]:
        store = _require_routines()
        require_current_binding()
        changes = body.model_dump(exclude_unset=True)
        try:
            routine = runner.edit_routine(routine_id, **changes)
        except RoutineNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except RoutineValidationError as exc:
            raise HTTPException(422, str(exc)) from None
        except RoutineConflictError as exc:
            raise HTTPException(409, str(exc)) from None
        return store.view(routine).model_dump()

    @app.post("/routines/{routine_id}/run-now", status_code=202)
    def run_routine_now(routine_id: str, body: RunNowRequest | None = None) -> dict[str, Any]:
        store = _require_routines()
        require_current_binding()
        try:
            occ = runner.run_routine_now(routine_id, trigger_id=body.trigger_id if body else None)
        except RoutineNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except RoutineConflictError as exc:
            raise HTTPException(409, str(exc)) from None
        return store.run_view(occ).model_dump()

    @app.post("/routines/{routine_id}/pause")
    async def pause_routine(routine_id: str) -> dict[str, Any]:
        store = _require_routines()
        try:
            sessions = store.pause_for_stop(routine_id, now=runner._clock_utc())
        except RoutineNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        runner.wake_routines()
        for session_id in sessions:
            await runner.stop_session(session_id)
        active = store.active_occurrences(routine_id=routine_id)
        return {
            **store.view(store.get(routine_id)).model_dump(),
            "stop_requested": bool(sessions),
            "active_run_stopped": bool(sessions)
            and all(occ.session_id not in sessions for occ in active),
        }

    @app.post("/routines/{routine_id}/resume")
    def resume_routine(routine_id: str, body: ResumeRoutineRequest | None = None) -> dict[str, Any]:
        store = _require_routines()
        require_current_binding()
        try:
            routine = runner.resume_routine(
                routine_id,
                acknowledge_uncertain=bool(body and body.acknowledge_uncertain),
            )
        except RoutineNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except RoutineConflictError as exc:
            raise HTTPException(409, str(exc)) from None
        return store.view(routine).model_dump()

    @app.post("/routines/{routine_id}/stop", status_code=202)
    async def stop_routine(routine_id: str) -> dict[str, Any]:
        _require_routines()
        try:
            return await runner.stop_routine(routine_id)
        except RoutineNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/routines/{routine_id}/occurrences/{occurrence_id}/confirm-delivery")
    def confirm_routine_delivery(
        routine_id: str,
        occurrence_id: str,
        body: ConfirmDeliveryRequest | None = None,
    ) -> dict[str, Any]:
        store = _require_routines()
        try:
            occ = store.occurrence(occurrence_id)
            if occ.routine_id != routine_id:
                raise HTTPException(404, "unknown occurrence for routine")
            updated = runner.confirm_routine_delivery(
                occurrence_id, receipt=body.receipt if body else None
            )
        except RoutineNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        return store.run_view(updated).model_dump()

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
