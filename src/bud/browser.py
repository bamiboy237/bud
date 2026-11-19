"""bud's browser: OpenCode Browse, which drives the person's own Chrome through an extension.

Bud starts `browse mcp` itself and offers its tools as `browser_*`, loaded only when the
model searches for them, and only after the owner turned the browser on. Browse's MCP
server has three tools: status, tabs, and execute, which runs JavaScript in a tab.

The owner turns the browser on once, after a caution that says what that allows
(enable_browser, which always asks and offers no standing grant). From then on, status runs
unasked, and every tabs or execute call goes to the approval reviewer first: a call it judges
safe and clearly requested runs, and anything else, or any reviewer failure, asks the owner
with the reason. The reviewer sees facts Bud looks up itself, such as the real address of the
tab a script drives. A tool a later Browse adds is refused until Bud names it.

The Mac app ships the `browse` program and copies it to ~/.bud/bin, where Browse keeps it up
to date with its own updater. A `browse` the owner installed is used when Bud's copy is
missing.
"""

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic_ai import Tool
from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.toolsets import AbstractToolset, DeferredLoadingToolset

from bud.core.setup import SetupEntry, SetupInitiation, SetupStatus
from bud.harness.actions import Policy

PREFIX = "browser"
STATUS = f"{PREFIX}_status"
TABS = f"{PREFIX}_tabs"
EXECUTE = f"{PREFIX}_execute"
TOOLS = {STATUS, TABS, EXECUTE}
ENABLE = "enable_browser"
DISABLE = "disable_browser"
EXTENSION_URL = "https://chromewebstore.google.com/detail/mfnicocicmmlkpjnaffgihfjhdgjkdjg"
INSTALL = "curl -fsSL https://opencode.ai/browse/install | bash"
CAUTION = (
    "Bud will use your own Chrome, with your tabs and the sites you are signed in to. It can "
    "read those sites, open pages, click, type, and submit forms as you. Each step is checked "
    "first, and anything risky, such as paying, sending, deleting, or typing a password, asks "
    "you. A page Bud reads can still try to trick it, and a check can miss a step written to "
    "look harmless. Ask Bud to turn the browser off at any time."
)
GUIDANCE = """browser_* tools use the person's own Chrome, with their tabs and sign-ins. They
appear after the person turns the browser on: call enable_browser, which shows them what that
allows. Keep each browser_execute script to one clear step, so it can be checked against what
the person asked. Use handoff() when only the person can act, such as signing in. If no
browser is connected, call setup_capability with "browser". Call disable_browser when the
person asks to stop.
"""


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
    """status runs unasked; tabs and execute go to the reviewer, never to a standing grant;
    turning the browser on always asks; turning it off never does."""
    policy.tools.update({STATUS: "allow", TABS: "review", EXECUTE: "review", DISABLE: "allow"})
    policy.tools[ENABLE] = "ask"
    policy.no_grants = policy.no_grants | {TABS, EXECUTE, ENABLE}


def control_tools(consent: Consent) -> list[Tool[None]]:
    def enable_browser() -> str:
        """Ask the person to let you use their own Chrome. The approval shows them what that
        allows. Call it once, before you need a browser tool."""
        consent.set(True)
        return "The browser is on. Search for browser tools to use it."

    def disable_browser() -> str:
        """Turn the browser off, when the person asks you to stop using it."""
        consent.set(False)
        return "The browser is off."

    return [Tool(enable_browser), Tool(disable_browser)]


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


def toolset(path: Path, consent: Consent) -> AbstractToolset[None]:
    """Browse's tools as browser_*: deferred until the model searches, and hidden while the
    browser is off. Bud's gateway already runs each call as one recorded DBOS step."""
    remote = MCPToolset(
        StdioTransport(str(path), ["mcp"]),
        id="browse",
        init_timeout=30,
        read_timeout=180,
        max_retries=0,
        tool_error_behavior="error",
        include_instructions=False,
        prefer_tasks=False,
    )
    visible = remote.prefixed(PREFIX).filtered(lambda ctx, tool: consent.on())
    return DeferredLoadingToolset(visible)


def _tabs(path: Path) -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            [str(path), "tabs"], capture_output=True, text=True, timeout=10, check=True
        )
        tabs = json.loads(result.stdout).get("tabs", [])
        return tabs if isinstance(tabs, list) else []
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return []


def describers(find: Callable[[], Path | None] = executable) -> dict[str, Callable]:
    """Facts for the reviewer that a script cannot fake: where its tab really is."""

    def where(tool_args: dict[str, Any]) -> str:
        path = find()
        tab = tool_args.get("tab")
        opening = f"Opens {tool_args['url']}." if tool_args.get("url") else ""
        if path is None or tab is None:
            return opening or "No tab named; Browse uses or opens one."
        for entry in _tabs(path):
            if str(tab) in (str(entry.get("tab")), str(entry.get("id"))):
                return f"{opening} Tab {tab} shows {entry.get('url', 'an unknown page')}.".strip()
        return f"{opening} Tab {tab} is not one Browse knows.".strip()

    return {TABS: where, EXECUTE: where}


def connected(path: Path) -> bool:
    try:
        result = subprocess.run(
            [str(path), "status", "--json"], capture_output=True, text=True, timeout=10
        )
        return bool(json.loads(result.stdout).get("extension", {}).get("connected"))
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
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

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def code(self, path: Path) -> str:
        """Starts pairing and returns its one-time code. Raises ConnectionError on failure."""
        with self._lock:
            self._stop()
            try:
                self._process = subprocess.Popen(
                    [str(path), "pair", "--wait-seconds", "600"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
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

    def _stop(self) -> None:
        if self._process is None:
            return
        self._process.kill()
        self._process.wait()
        if self._process.stdout is not None:
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
                f"In the extension, open Options, choose Pair with a code, and enter {code}. "
                "The code works once, for ten minutes.",
            ],
        )

    def status() -> SetupStatus:
        path = find()
        if path is not None and connected(path):
            return SetupStatus(state="connected")
        return SetupStatus(state="waiting")

    return SetupEntry(label="Browser", kind="steps", initiate=initiate, status=status)
