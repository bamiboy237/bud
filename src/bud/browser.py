"""bud's browser: OpenCode Browse, which drives the person's own Chrome through an extension.

Bud starts `browse mcp` itself and offers its tools as `browser_*`, loaded only when the
model searches for them. Status and tabs run without asking. browser_execute, which runs
JavaScript in a tab, asks the owner, or in auto mode the approval reviewer. A tool a later
Browse adds is refused. Each call is one recorded DBOS step, so recovery does not repeat it.

The Mac app ships the `browse` program and copies it to ~/.bud/bin, where it keeps
updating itself. A `browse` the owner installed is used when Bud's own copy is missing.
"""

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from dbos import DBOS
from pydantic_ai import RunContext
from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.toolsets import AbstractToolset, DeferredLoadingToolset

from bud.core.setup import SetupEntry, SetupInitiation, SetupStatus
from bud.harness.actions import Policy
from bud.harness.writes import BROWSER_EXECUTE

PREFIX = "browser"
EXTENSION_URL = "https://chromewebstore.google.com/detail/mfnicocicmmlkpjnaffgihfjhdgjkdjg"
INSTALL = "curl -fsSL https://opencode.ai/browse/install | bash"
# Browse's MCP server offers three tools. Listing status and tabs, and opening a page,
# change nothing in the owner's accounts, so they run unasked.
READ_TOOLS = {f"{PREFIX}_status", f"{PREFIX}_tabs"}
# execute runs JavaScript that can read a page or click "Buy". It asks the owner; in auto
# mode the approval reviewer judges the exact code against the owner's request instead.
ACTION_TOOLS = {BROWSER_EXECUTE}
GUIDANCE = """browser_* tools use the person's own Chrome, with their tabs and sign-ins. Search
for them when a task needs a website. browser_execute runs JavaScript in a tab: keep each call
to one clear step, so it can be checked against what the person asked. Use handoff() when only
the person can act, such as signing in. If no browser is connected, call setup_capability with
"browser".
"""


def allow_tools(policy: Policy) -> None:
    """Reading runs unasked; acting asks. A tool a later Browse adds stays refused."""
    policy.tools.update(dict.fromkeys(READ_TOOLS, "allow"))
    policy.tools.update(dict.fromkeys(ACTION_TOOLS, "ask"))


def executable() -> Path | None:
    """Bud's own copy of browse, or one the owner installed."""
    own = Path.home() / ".bud" / "bin" / "browse"
    if own.is_file():
        return own
    found = shutil.which("browse")
    return Path(found) if found else None


async def _durable_call(ctx: RunContext[Any], call_tool, name: str, tool_args: dict) -> Any:
    if DBOS.workflow_id is not None and DBOS.step_id is None:
        # One recorded step: recovery returns the result instead of clicking again.
        return await DBOS.run_step_async(
            {"name": f"bud.browser.{name}", "retries_allowed": False}, call_tool, name, tool_args
        )
    return await call_tool(name, tool_args)


def toolset(path: Path) -> AbstractToolset[None]:
    """Browse's tools as browser_*, deferred until the model searches for them."""
    remote = MCPToolset(
        StdioTransport(str(path), ["mcp"]),
        id="browse",
        init_timeout=30,
        read_timeout=180,
        max_retries=0,
        tool_error_behavior="error",
        include_instructions=False,
        prefer_tasks=False,
        process_tool_call=_durable_call,
    )
    return DeferredLoadingToolset(remote.prefixed(PREFIX))


def connected(path: Path) -> bool:
    try:
        result = subprocess.run(
            [str(path), "status", "--json"], capture_output=True, text=True, timeout=10
        )
        return bool(json.loads(result.stdout).get("extension", {}).get("connected"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
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


def setup_entry(find=executable) -> SetupEntry:
    """The "Connect your browser" card: install the extension, then pair it with a code."""
    pairing: dict[str, subprocess.Popen] = {}
    lock = threading.Lock()

    def initiate() -> SetupInitiation:
        path = find()
        if path is None:
            return SetupInitiation(
                kind="steps",
                steps=[
                    f"Install browse in a terminal: {INSTALL}",
                    f"Add the browse extension to Chrome: {EXTENSION_URL}",
                    "Then ask Bud to connect your browser again.",
                ],
            )
        with lock:
            if (old := pairing.pop("process", None)) is not None:
                old.kill()
            # It prints a one-time code, then waits up to ten minutes for the extension.
            process = subprocess.Popen(
                [str(path), "pair", "--wait-seconds", "600"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            pairing["process"] = process
        first = _first_line(process, timeout=15)
        code = first.removeprefix("Pairing code:").strip()
        if not first.startswith("Pairing code:") or not code:
            raise RuntimeError("browse did not print a pairing code")
        return SetupInitiation(
            kind="steps",
            steps=[
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
