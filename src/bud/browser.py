"""bud's browser: OpenCode Browse, which drives the person's own Chrome through an extension.

Bud runs its own Browse relay, with its own home folder in ~/.bud/browse and its own port,
so it can ask for Browse's small typed tools (navigate, snapshot, click, fill, ...) instead
of one tool that runs any JavaScript. The extension connects to one relay at a time:
pairing it with Bud's relay disconnects any other agent that used Browse.

Bud offers a chosen set of Browse's tools as `browser_*`, loaded only when the model searches
for them, and only after the owner turned the browser on. Reading a page runs unasked. Each
step that acts on a page (opening, clicking, typing) goes to the approval reviewer in auto
mode and to the owner in strict mode. Uploading a file always asks the owner. Running
arbitrary JavaScript, Browse's settings, and the other tools are never offered.

The owner turns the browser on once, after a caution that says what that allows
(enable_browser, which always asks and offers no standing grant).

The Mac app ships the `browse` program and copies it to ~/.bud/bin, where Browse keeps it up
to date with its own updater. A `browse` the owner installed is used when Bud's copy is
missing.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic_ai import RunContext, Tool
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, DeferredLoadingToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from bud.composio.binding import ConnectionError
from bud.core.setup import SetupEntry, SetupInitiation, SetupStatus
from bud.harness.actions import Policy

logger = logging.getLogger(__name__)

PREFIX = "browser"
# Read the page or wait for the person: these change nothing, so they run unasked.
READS = frozenset(
    f"{PREFIX}_{name}" for name in ("status", "snapshot", "screenshot", "scroll", "wait_for")
) | {f"{PREFIX}_handoff"}
# Act on a page: reviewed in auto mode, asked in strict mode.
STEPS = frozenset(
    f"{PREFIX}_{name}"
    for name in (
        "tabs",
        "navigate",
        "click",
        "fill",
        "type",
        "press",
        "select",
        "hover",
        "drag",
        "dialog",
    )
)
# Hands a local file to a website: the owner always decides.
UPLOAD = f"{PREFIX}_upload"
TABS = f"{PREFIX}_tabs"
TOOLS = READS | STEPS | {UPLOAD}
ENABLE = "enable_browser"
DISABLE = "disable_browser"
OPEN_CHROME = "open_chrome"
# How long to wait for the extension after Chrome opens.
CONNECT_SECONDS = 30
# Bud's relay port. Browse's own default is 19988; another port keeps the two apart.
PORT = 19989
# Beside Bud's own copy of browse, outside the workspace, so shell commands cannot touch the
# relay's token or pairing.
RELAY_HOME = Path.home() / ".bud" / "browse"
EXTENSION_URL = "https://chromewebstore.google.com/detail/mfnicocicmmlkpjnaffgihfjhdgjkdjg"
INSTALL = "curl -fsSL https://opencode.ai/browse/install | bash"
CAUTION = (
    "Bud will use your own Chrome, with your tabs and the sites you are signed in to. It can "
    "read those sites, open pages, click, type, and submit forms as you, including in Gmail "
    "and Google Calendar. Each step is checked first, and anything risky, such as paying, "
    "sending, deleting, or typing a password, asks you. A page Bud reads can still try to "
    "trick it, and a check can miss a step written to look harmless. While Bud uses the "
    "browse extension, other apps that use it lose their connection. Ask Bud to turn the "
    "browser off at any time."
)
GUIDANCE = """browser_* tools use the person's own Chrome, with their tabs and sign-ins. They
appear after the person turns the browser on: call enable_browser, which shows them what that
allows, then search for browser tools.
- Reuse tabs. Before opening a page, call browser_tabs with action "list"; if a tab you opened
  already shows the page, pass its id as tab. Pass the tab id from navigate to every later call.
- Do not set readOnly or allowedOrigins unless the person asks: a read-only tab cannot click.
- Work in small steps: one browser_snapshot to read a page, act on one element by its ref,
  then snapshot again to check. Take a screenshot only when the layout itself matters.
- When the task is done, close the tabs you opened with browser_tabs action "close".
- Use browser_handoff when only the person can act, such as signing in or paying.
- If browser_status says no browser is connected, call open_chrome; if Chrome is open and
  still not connected, call setup_capability with "browser".
- Call disable_browser when the person asks to stop.
"""


@dataclass(frozen=True)
class Relay:
    """Bud's own Browse relay: its home folder and port, passed to every browse process."""

    home: Path
    port: int = PORT

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "OPENCODE_BROWSER_HOME": str(self.home),
            "OPENCODE_BROWSER_PORT": str(self.port),
            "OPENCODE_BROWSER_TOOLS": "plain",
        }

    def run(self, path: Path, *args: str, timeout: float) -> str:
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        return subprocess.run(
            [str(path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
            env=self.env(),
        ).stdout


class Consent:
    """Whether the owner turned the browser on, kept across restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def on(self) -> bool:
        try:
            return json.loads(self.path.read_text()).get("on") is True
        except (OSError, ValueError, AttributeError):
            return False

    def set(self, on: bool) -> None:
        partial = self.path.with_suffix(".tmp")
        partial.write_text(json.dumps({"on": on}))
        os.replace(partial, self.path)


def allow_tools(policy: Policy) -> None:
    """Reads run unasked. Steps and uploads ask the owner; auto mode hands steps to the
    reviewer (bud.core.profiles). No standing grant covers a step, an upload, or turning the
    browser on. Opening Chrome asks, and may be always allowed. Turning it off never asks."""
    policy.tools.update(dict.fromkeys(READS | {DISABLE}, "allow"))
    policy.tools.update(dict.fromkeys(STEPS | {UPLOAD, ENABLE, OPEN_CHROME}, "ask"))
    policy.no_grants = policy.no_grants | STEPS | {UPLOAD, ENABLE}


def launch_chrome() -> None:
    """Opens Chrome, or brings it forward, the way the Dock does."""
    subprocess.run(["open", "-a", "Google Chrome"], check=True, capture_output=True, timeout=15)


def control_tools(
    consent: Consent,
    relay: Relay,
    find: Callable[[], Path | None] | None = None,
    launch: Callable[[], None] = launch_chrome,
) -> list[Tool[None]]:
    locate = find or executable

    def enable_browser() -> str:
        """Ask the person to let you use their own Chrome. The approval shows them what that
        allows. Call it once, before you need a browser tool."""
        consent.set(True)
        return "The browser is on. Search for browser tools, such as browser_navigate."

    def disable_browser() -> str:
        """Turn the browser off, when the person asks you to stop using it."""
        consent.set(False)
        return "The browser is off."

    async def open_chrome() -> str:
        """Open the person's Chrome when browser_status says no browser is connected, then
        wait for the browse extension to connect."""
        if not consent.on():
            return "The browser is off. Call enable_browser first."
        path = locate()
        if path is None:
            return 'Browse is not installed. Call setup_capability with "browser".'
        if await connected_now(path, relay):
            return "Chrome is already connected."
        try:
            await asyncio.to_thread(launch)
        except (OSError, subprocess.SubprocessError):
            return "Chrome could not be opened. Ask the person to open it."
        deadline = asyncio.get_running_loop().time() + CONNECT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(1)
            if await connected_now(path, relay):
                return "Chrome is open and connected. Continue with the browser tools."
        return (
            "Chrome is open, but the browse extension did not connect. Call "
            'setup_capability with "browser" to pair it.'
        )

    async def while_on(ctx: RunContext[None], tool_def: ToolDefinition) -> ToolDefinition | None:
        # Offered only while the browser is on, so an approval never ends in "it's off".
        return tool_def if consent.on() else None

    return [Tool(enable_browser), Tool(disable_browser), Tool(open_chrome, prepare=while_on)]


def executable() -> Path | None:
    """Bud's own copy of browse, then the installer's default, then one on PATH."""
    for candidate in (
        Path.home() / ".bud" / "bin" / "browse",
        Path.home() / ".local" / "bin" / "browse",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("browse")
    return Path(found) if found else None


# Page actions that need the tab they act on. Without one, Browse acts on a tab the reviewer
# cannot see, so Bud refuses the call instead.
NEEDS_TAB = (STEPS - {f"{PREFIX}_navigate", f"{PREFIX}_tabs"}) | {UPLOAD}


@dataclass
class BrowseTools(AbstractToolset[None]):
    """Browse's chosen tools as browser_*, offered and callable only while the owner has the
    browser on. Each listing and each call opens its own short connection to `browse mcp`
    and closes it in the same task, so no connection outlives a step, races another run, or
    goes stale when Browse restarts. Bud's relay keeps the tabs between calls."""

    path: Path
    consent: Consent
    relay: Relay

    @property
    def id(self) -> str | None:
        return "browse"

    def _connect(self) -> AbstractToolset[None]:
        self.relay.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        remote = MCPToolset(
            StdioTransport(str(self.path), ["mcp"], env=self.relay.env()),
            init_timeout=30,
            read_timeout=180,
            max_retries=0,
            # A stale ref or a closed Chrome is the model's to handle, not a failed run.
            tool_error_behavior="failed",
            include_instructions=False,
            prefer_tasks=False,
        )
        return remote.prefixed(PREFIX).filtered(lambda ctx, tool: tool.name in TOOLS)

    async def get_tools(self, ctx: RunContext[None]) -> dict[str, ToolsetTool[None]]:
        if not self.consent.on():
            return {}
        try:
            async with self._connect() as connection:
                tools = await connection.get_tools(ctx)
        except Exception as exc:
            # browser_status cannot explain a Browse that will not start; the tools stay away.
            logger.warning("browse mcp did not list its tools: %s", type(exc).__name__)
            return {}
        return {name: replace(tool, toolset=self) for name, tool in tools.items()}

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[None], tool: ToolsetTool[None]
    ) -> Any:
        if not self.consent.on():
            return "The browser is off, so nothing was done."
        if name in NEEDS_TAB and tool_args.get("tab") is None:
            return "Nothing was done: pass the tab id from browser_navigate as tab."
        try:
            async with self._connect() as connection:
                tools = await connection.get_tools(ctx)
                if name not in tools:
                    return f"{name} is not available right now, so nothing was done."
                return await connection.call_tool(name, tool_args, ctx, tools[name])
        except ToolFailed:
            raise  # Browse's own error message, which the model sees and can act on
        except Exception as exc:
            logger.warning("browse mcp failed during %s: %s", name, type(exc).__name__)
            return "Browse could not be reached, so nothing was done. Call browser_status."


def toolset(path: Path, consent: Consent, relay: Relay) -> AbstractToolset[None]:
    """Browse's chosen tools, deferred until the model searches for them."""
    return DeferredLoadingToolset(BrowseTools(path, consent, relay))


def _site(url: object) -> str:
    """A page's scheme, host, and path, without the query or fragment where pages put
    tracking data or injected text, on one short line."""
    parts = urlsplit(str(url))
    shown = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else "an unknown page"
    return " ".join(shown.split())[:160]


async def _tabs(path: Path, relay: Relay) -> list[dict[str, Any]]:
    """Browse's tab list, without blocking Bud while Browse answers."""
    relay.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        process = await asyncio.create_subprocess_exec(
            str(path),
            "tabs",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=relay.env(),
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
            return []
        tabs = json.loads(out).get("tabs", [])
        return [tab for tab in tabs if isinstance(tab, dict)] if isinstance(tabs, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def describers(relay: Relay, find: Callable[[], Path | None] = executable) -> dict[str, Callable]:
    """Facts for the reviewer that the model cannot fake: which site a tab really shows.
    Nothing the model wrote is repeated here; the reviewer sees the arguments separately."""

    async def where(tool_args: dict[str, Any]) -> str:
        tab = tool_args.get("tab")
        if tab is None:
            return "Opens a new tab."
        path = find()
        if path is None:
            return "The tab could not be looked up."
        for entry in await _tabs(path, relay):
            if str(tab) in {str(entry[key]) for key in ("tab", "id") if entry.get(key)}:
                return f"The tab shows {_site(entry.get('url'))}."
        return "The tab is not one Browse knows."

    async def tabs(tool_args: dict[str, Any]) -> str:
        action = tool_args.get("action")
        if action == "close" and tool_args.get("tab") is not None:
            return f"Closes a tab. {await where(tool_args)}"
        if action == "close":
            return "Closes tabs Bud opened."
        if action == "new":
            return "Opens a new tab."
        return "Changes Bud's browser tabs."

    def caution(tool_args: dict[str, Any]) -> str:
        return CAUTION

    # Every client shows the caution, because it rides the approval as a looked-up fact.
    return {**dict.fromkeys(STEPS | {UPLOAD}, where), TABS: tabs, ENABLE: caution}


def reads_only() -> dict[str, Callable[[dict[str, Any]], bool]]:
    """Calls that only read, judged in code: listing tabs changes nothing."""
    return {TABS: lambda tool_args: tool_args.get("action") == "list"}


async def connected_now(path: Path, relay: Relay) -> bool:
    """Whether the extension is connected, asked without blocking Bud and given up after a
    few seconds."""
    try:
        process = await asyncio.create_subprocess_exec(
            str(path),
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=relay.env(),
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
            return False
        return bool(json.loads(out).get("extension", {}).get("connected"))
    except (OSError, ValueError, AttributeError):
        return False


def connected(path: Path, relay: Relay) -> bool:
    try:
        status = json.loads(relay.run(path, "status", "--json", timeout=10))
        return bool(status.get("extension", {}).get("connected"))
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return False


def _first_line(process: subprocess.Popen, timeout: float) -> str:
    """The process's first output line, or "" if it prints nothing in time."""
    lines: list[str] = []
    reader = threading.Thread(
        target=lambda: lines.append(process.stdout.readline() if process.stdout else ""),
        daemon=True,
    )
    reader.start()
    reader.join(timeout)
    return lines[0] if lines else ""


class Pairing:
    """The one `browse pair` process that waits for the extension, if any."""

    def __init__(self, relay: Relay) -> None:
        self.relay = relay
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def code(self, path: Path) -> str:
        """Starts pairing and returns its one-time code. Raises Bud's ConnectionError, which
        the setup card shows as a failed sign-in."""
        with self._lock:
            self._stop()
            self.relay.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                self._process = subprocess.Popen(
                    [str(path), "pair", "--wait-seconds", "600"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    env=self.relay.env(),
                )
            except OSError as exc:
                raise ConnectionError("Browse could not start. Try again.") from exc
            first = _first_line(self._process, timeout=15)
            code = first.removeprefix("Pairing code:").strip()
            if not first.startswith("Pairing code:") or not code:
                self._stop()
                raise ConnectionError("Browse did not start pairing. Try again.")
            return code

    def stop(self) -> None:
        with self._lock:
            self._stop()

    def waiting(self) -> bool:
        """True while a pairing code is live and `browse pair` waits for the extension."""
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def _stop(self) -> None:
        if self._process is None:
            return
        self._process.kill()
        self._process.wait()
        if self._process.stdout is not None:
            with contextlib.suppress(OSError):
                self._process.stdout.close()
        self._process = None


def setup_entry(pairing: Pairing, find: Callable[[], Path | None] = executable) -> SetupEntry:
    """The "Connect your browser" card: the caution, the extension, then a pairing code."""

    def initiate() -> SetupInitiation:
        path = find()
        if path is None:
            return SetupInitiation(
                kind="steps",
                steps=[
                    CAUTION,
                    f"Install browse in a terminal: {INSTALL}",
                    f"Add the browse extension to Chrome: {EXTENSION_URL}",
                    "Then ask Bud to connect your browser again.",
                ],
            )
        code = pairing.code(path)
        return SetupInitiation(
            kind="steps",
            steps=[
                CAUTION,
                f"Add the browse extension to Chrome: {EXTENSION_URL}",
                "In the extension, open Options and choose Pair with a code. Enter "
                f"{code} as the code and localhost:{pairing.relay.port} as the relay address, "
                "then click Connect. The code works once, for ten minutes.",
            ],
        )

    def status() -> SetupStatus:
        path = find()
        if path is not None and connected(path, pairing.relay):
            return SetupStatus(state="connected")
        if pairing.waiting():
            return SetupStatus(state="waiting")
        # No live code: a failed card lets the next "connect my browser" start over.
        reason = (
            "Browse is not installed yet."
            if path is None
            else "The pairing code expired or was cancelled."
        )
        return SetupStatus(
            state="failed", reason=f"{reason} Ask Bud to connect your browser again."
        )

    return SetupEntry(label="Browser", kind="steps", initiate=initiate, status=status)
