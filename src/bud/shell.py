"""bud's shell: commands run inside the workspace under the macOS sandbox (Seatbelt).

A command may write only inside the workspace and the system temp folder. It cannot read
the owner's home folder, apart from the workspace and the toolchains it needs (uv, cargo).
It gets a minimal environment, so no API key or token leaks into it.

The sandbox only knows "network on" or "network off", so a command reaches the network
through a local proxy that runs for that command. Package registries and code hosts pass.
Any other site goes to the decision model with the command and the owner's request; an
allowed site is remembered for the rest of the process. A site the model does not allow is
blocked and reported, and Bud can ask the owner with allow_network, which needs approval.
"""

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import SplitResult, urlsplit

from dbos import DBOS
from pydantic import Field
from pydantic_ai import RunContext, Tool

from bud.harness.review import ReviewRequest, ToolReviewer, Verdict

logger = logging.getLogger(__name__)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SHELL_TOOLS = {"run_command"}
NETWORK_TOOL = "allow_network"
OUTPUT_CHARS = 200_000
GUIDANCE = """run_command runs a shell command in your workspace, sandboxed: it can write only
there, cannot read the person's other files, and has no secrets. Use it to install packages
(`uv pip install python-docx`, `npm install`), run scripts with `python`, and create files of
any type, such as Word, Excel, PDF, or charts, with real libraries. Python packages go into
the workspace's own environment, which `python` uses. Websites other than package
registries and code hosts are checked first; a blocked site is listed in the result, and
allow_network asks the person to allow it.
"""

# Sites a command may always reach: package registries and code hosts.
ALWAYS = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    "crates.io",
    "static.crates.io",
    "index.crates.io",
    "github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "proxy.golang.org",
    "sum.golang.org",
    "rubygems.org",
)

Judge = Callable[[str], Awaitable[bool]]
# Run before each command, inside the sandbox: make the workspace's Python environment once.
PREPARE = "[ -x .venv/bin/python ] || uv venv -q .venv 2>/dev/null || python3 -m venv .venv\n"


def _quoted(path: Path | str) -> str:
    return json.dumps(str(path))  # SBPL strings use the same escapes as JSON


def toolchains() -> list[Path]:
    """Folders under the home folder a command needs to read to run its tools."""
    home = Path.home()
    found = [
        home / ".local" / "share" / "uv",
        home / ".local" / "bin",
        home / ".cargo",
        home / ".rustup",
    ]
    return [path.resolve() for path in found if path.exists()]


def profile(workspace: Path, port: int, private: Sequence[Path] = ()) -> str:
    """The Seatbelt rules for one command. Later rules win over earlier ones.

    `private` are folders outside the home folder a command must not read either, such as
    Bud's data folder and the checkout holding its .env.
    """
    hidden = " ".join(
        f"(subpath {_quoted(p)})" for p in [Path.home().resolve(), *map(Path.resolve, private)]
    )
    readable = " ".join(f"(subpath {_quoted(p)})" for p in [workspace, *toolchains()])
    return f"""(version 1)
(allow default)
(deny file-read-data {hidden})
(allow file-read-data {readable})
(deny file-write*)
(allow file-write* (subpath {_quoted(workspace)}) (subpath "/private/var/folders")
  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/dtracehelper"))
(deny network*)
(allow network-outbound (remote ip {_quoted(f"localhost:{port}")}))
(deny appleevent-send)
(deny process-exec (literal "/usr/bin/open") (literal "/usr/bin/osascript")
  (literal "/usr/bin/security"))
; No starting programs outside the sandbox: LaunchServices (open, copies of it) and launchd.
(deny mach-lookup (global-name "com.apple.coreservices.launchservicesd")
  (global-name-prefix "com.apple.lsd.") (global-name "com.apple.coreservices.quarantine-resolver"))
(deny job-creation)
"""


def environment(workspace: Path, port: int) -> dict[str, str]:
    """A minimal environment: tools, caches, and the proxy. Nothing from Bud's own."""
    proxy = f"http://127.0.0.1:{port}"
    tools = [str(path) for path in toolchains() if path.name in {"bin"}]
    cargo = Path.home() / ".cargo" / "bin"
    venv = workspace / ".venv"
    return {
        "PATH": ":".join(
            [str(venv / "bin"), *tools, *([str(cargo)] if cargo.exists() else [])]
            + ["/opt/homebrew/bin"]
            + ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        ),
        "HOME": str(workspace),
        "LANG": "en_US.UTF-8",
        "TMPDIR": str(workspace / ".tmp"),
        "XDG_CACHE_HOME": str(workspace / ".cache"),
        "UV_CACHE_DIR": str(workspace / ".cache" / "uv"),
        "PIP_CACHE_DIR": str(workspace / ".cache" / "pip"),
        "npm_config_cache": str(workspace / ".cache" / "npm"),
        # `uv pip install` and `python` share the workspace's environment.
        "VIRTUAL_ENV": str(venv),
        **{name: proxy for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")},
        "NO_PROXY": "",
    }


_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")


def site(authority: str, default_port: int) -> tuple[str, int]:
    """The host and port of `host[:port]`, IPv6 in brackets. Raises ValueError otherwise."""
    parts = urlsplit("//" + authority)
    if parts.username is not None or parts.password is not None or parts.path or parts.query:
        raise ValueError(f"not a plain host: {authority!r}")
    host = (parts.hostname or "").rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOST.fullmatch(host):
            raise ValueError(f"not a host name: {authority!r}") from None
    return host, parts.port or default_port


def always_allowed(host: str) -> bool:
    return any(host == site or host.endswith("." + site) for site in ALWAYS)


class Proxy:
    """An HTTP proxy for one command: CONNECT for HTTPS, absolute URLs for plain HTTP."""

    def __init__(self, judge: Judge) -> None:
        self.judge = judge
        self.blocked: set[str] = set()
        self._decided: dict[str, asyncio.Task[bool]] = {}
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    async def allowed(self, host: str) -> bool:
        if always_allowed(host):
            return True
        # One decision per host per command, even when several connections ask at once.
        if host not in self._decided:
            self._decided[host] = asyncio.ensure_future(self.judge(host))
        allowed = await self._decided[host]
        if not allowed:
            self.blocked.add(host)
        return allowed

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ")
            if method == "CONNECT":
                host, port = site(target, 443)
            else:
                url = urlsplit(target)
                if url.scheme != "http":
                    raise ValueError("only absolute http:// URLs and CONNECT")
                host, port = site(url.netloc, 80)
            if not await self.allowed(host):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            if method == "CONNECT":
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), 30
                )
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
                return
            # Plain HTTP: exactly one request per connection, so a reused connection cannot
            # carry a second request to a site that was never checked.
            request = await _one_request(reader, lines, method, url, version)
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), 30
            )
            upstream_writer.write(request)
            await _pipe(upstream_reader, writer)
        except (
            OSError,
            ValueError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
        ):
            with contextlib.suppress(Exception):
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        finally:
            with contextlib.suppress(Exception):
                writer.close()


async def _one_request(
    reader: asyncio.StreamReader, lines: list[str], method: str, url: SplitResult, version: str
) -> bytes:
    """The request to send upstream, with its body, asking the server to close after it."""
    headers = [line for line in lines[1:] if line]
    names = {line.split(":", 1)[0].strip().lower(): line for line in headers}
    if "transfer-encoding" in names:
        raise ValueError("chunked request bodies are not supported")
    length = int(names["content-length"].split(":", 1)[1]) if "content-length" in names else 0
    body = await reader.readexactly(length) if length else b""
    kept = [
        line
        for line in headers
        if line.split(":", 1)[0].strip().lower() not in {"connection", "proxy-connection"}
    ]
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
    head = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close", "", ""])
    return head.encode("latin-1") + body


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while chunk := await source.read(65536):
            sink.write(chunk)
            await sink.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(Exception):
            sink.close()


def _kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


async def run(
    workspace: Path,
    command: str,
    timeout: float,
    judge: Judge,
    private: Sequence[Path] = (),
) -> dict[str, Any]:
    """Runs one command in the sandbox and reports its output and any blocked sites."""
    workspace = workspace.resolve()  # Seatbelt matches real paths, not links
    proxy = Proxy(judge)
    port = await proxy.start()
    (workspace / ".tmp").mkdir(exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        SANDBOX_EXEC,
        "-p",
        profile(workspace, port, private),
        "/bin/zsh",
        "-c",
        PREPARE + command,
        cwd=workspace,
        env=environment(workspace, port),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        timed_out = True
        _kill(process.pid)
        stdout, stderr = await process.communicate()
    finally:
        if process.returncode is None:
            _kill(process.pid)  # cancelled while it ran: stop it with its children
        await proxy.close()
    result: dict[str, Any] = {
        "exit_code": process.returncode,
        "stdout": stdout.decode("utf-8", "replace")[-OUTPUT_CHARS:],
        "stderr": stderr.decode("utf-8", "replace")[-OUTPUT_CHARS:],
    }
    if timed_out:
        result["timed_out"] = f"Stopped after {timeout:g} seconds."
    if proxy.blocked:
        result["blocked_sites"] = sorted(proxy.blocked)
        result["note"] = "To use a blocked site, call allow_network so the person can allow it."
    return result


class NetworkGrants:
    """Sites the owner allowed for shell commands, kept across restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def sites(self) -> set[str]:
        try:
            return set(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            return set()

    def add(self, host: str) -> None:
        sites = sorted(self.sites() | {host})
        partial = self.path.with_suffix(".tmp")
        partial.write_text(json.dumps(sites))
        os.replace(partial, self.path)


def available() -> bool:
    return sys.platform == "darwin" and Path(SANDBOX_EXEC).exists()


async def judge_site(
    reviewer: ToolReviewer | None, host: str, command: str, owner_request: str
) -> bool:
    """Whether the decision model allows a command to reach `host`. No reviewer, no owner
    request, or any doubt means no: the site is blocked and Bud can ask the owner."""
    if reviewer is None or not owner_request:
        return False
    result = await reviewer.review(
        ReviewRequest(
            tool="network_access",
            args={"host": host, "command": command[:2_000]},
            description="A sandboxed shell command in Bud's workspace wants to connect to "
            "this website. Allow it when the owner's request needs that site.",
            owner_request=owner_request,
        )
    )
    return result.verdict is Verdict.ALLOW


def build_tools(
    workspace: Path,
    grants: NetworkGrants,
    reviewer: Callable[[], ToolReviewer | None],
    private: Sequence[Path] = (),
) -> list[Tool[None]]:
    """run_command and allow_network. `reviewer` returns the decision model, if any.
    `private` are folders outside the home folder that commands must not read."""
    workspace = workspace.resolve()
    remembered: set[str] = set()

    async def run_command(
        ctx: RunContext[None],
        command: str,
        timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 120,
    ) -> dict[str, Any]:
        """Run a shell command (zsh) in your workspace, sandboxed. Output comes back in full
        for small results; large output is saved and previewed.

        Args:
            command: The command line, run from the workspace folder.
            timeout_seconds: Seconds before the command is stopped, from 1 to 600.
        """
        request = (ctx.metadata or {}).get("bud_request")
        owner_request = request if isinstance(request, str) else ""

        async def judge(host: str) -> bool:
            if host in remembered or host in grants.sites():
                return True
            if await judge_site(reviewer(), host, command, owner_request):
                remembered.add(host)
                return True
            return False

        async def execute() -> dict[str, Any]:
            return await run(workspace, command, timeout_seconds, judge, private)

        if DBOS.workflow_id is not None and DBOS.step_id is None:
            # One recorded step: crash recovery returns this output instead of running the
            # command again.
            return await DBOS.run_step_async(
                {"name": "bud.shell", "retries_allowed": False}, execute
            )
        return await execute()

    def allow_network(host: str, reason: str) -> str:
        """Ask the person to let shell commands reach a website that was blocked.

        Args:
            host: The site's host name, such as api.example.com.
            reason: One sentence on why the task needs it.
        """
        try:
            name, _ = site(host.strip().lower(), 443)
        except ValueError:
            return "Give only the host name, such as api.example.com."
        grants.add(name)
        return f"Shell commands may now reach {name}."

    return [Tool(run_command), Tool(allow_network)]
