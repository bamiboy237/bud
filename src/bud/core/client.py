"""Terminal client for a running bud server.
It streams a session's events over server-sent events, prints them, and sends each line the
user types as a message.
"""

import asyncio
import json
import sys
from uuid import uuid4

import httpx
from rich.console import Console
from rich.prompt import Prompt

from bud.config import get_settings
from bud.core.auth import OwnerAuthError, owner_token
from bud.core.controls import HELP, approval_text, session_snapshot
from bud.core.controls import submit as submit_control
from bud.core.projection import reply_text


def render(
    event: dict,
    seen_requests: set[str],
    streamed: dict[str, str] | None = None,
    *,
    historical_approval: bool = False,
) -> None:
    data = event["data"]
    streamed = {} if streamed is None else streamed
    match event["type"]:
        case "user.message":
            print(f"\nyou: {data['text']}")
        case "message.queued":
            print(f"\n[queued] {data['message_id']}: {data['text']}", flush=True)
        case "message.edited":
            print(f"\n[edited] {data['message_id']}: {data['text']}", flush=True)
        case "message.cancelled":
            print(f"\n[message cancelled] {data['message_id']}", flush=True)
        case "queue.paused":
            print("\n[queue paused]", flush=True)
        case "queue.resumed":
            print("\n[queue resumed]", flush=True)
        case "run.stop_requested":
            print(
                "\n[stop requested; queue paused; authorized actions may still finish]", flush=True
            )
        case "run.cancelled":
            streamed.pop(data["run_id"], None)
            uncertain = data.get("uncertain_tools", [])
            if uncertain:
                print(f"\n[stopped; check the outcome of: {', '.join(uncertain)}]", flush=True)
            else:
                print("\n[run cancelled]", flush=True)
        case "run.started":
            print("bud: ", end="", flush=True)
        case "run.recovered":
            print("\n[recovering interrupted work]", flush=True)
        case "text.reset":
            request = data["request"]
            if request in seen_requests:
                print(
                    "\n[discard the partial response above; generating its replacement]\nbud: ",
                    end="",
                    flush=True,
                )
            seen_requests.add(request)
        case "text.delta":
            run_id = data["run_id"]
            streamed[run_id] = streamed.get(run_id, "") + data["text"]
            print(data["text"], end="", flush=True)
        case "tool.call":
            print(f"\n[tool] {data['tool']}({data['args']})", flush=True)
        case "tool.result":
            print(f"[result] {data['result'][:200]}", flush=True)
        case "approval.requested" | "task.approval":
            if historical_approval:
                print(
                    f"\n[historical approval; no longer pending] {data['approval_id']}: "
                    f"{data['tool']}",
                    flush=True,
                )
            else:
                print(f"\n{approval_text(data)}", flush=True)
        case "approval.granted":
            print(f"[approved] {data['approval_id']}", flush=True)
        case "approval.denied":
            print(f"[denied] {data['approval_id']}", flush=True)
        case "run.waiting" | "run.completed":
            # Live text can be lost on disconnect or backlog overflow. Reconcile with
            # the stored reply instead of assuming one received token means all arrived.
            text = reply_text(data.get("messages", []))
            printed = streamed.pop(data["run_id"], "")
            if text.startswith(printed):
                print(text[len(printed) :], end="", flush=True)
            elif text:
                print(f"\n[complete reply]\n{text}", end="", flush=True)
            print(flush=True)
        case "run.failed":
            streamed.pop(data["run_id"], None)
            print(f"\n[run failed] {data['reason']}", flush=True)
        case "task.completed" | "task.failed" | "task.cancelled":
            print(f"\nTask {data['task_id']}: {data['text']}", flush=True)


def print_http_error(error: httpx.HTTPStatusError) -> None:
    response = error.response
    try:
        detail = response.json().get("detail", response.reason_phrase)
    except (ValueError, AttributeError):
        detail = response.reason_phrase
    print(f"[HTTP {response.status_code}] {detail}", flush=True)


async def follow(client: httpx.AsyncClient, session_id: str) -> None:
    last_seq = 0
    seen_requests: set[str] = set()
    streamed: dict[str, str] = {}
    while True:
        try:
            state = await session_snapshot(client, session_id)
            watermark = state["last_seq"]
            pending = {item["approval_id"] for item in state["pending_approvals"]}
            async with client.stream(
                "GET",
                f"/sessions/{session_id}/events",
                headers={"Last-Event-ID": str(last_seq)},
                timeout=httpx.Timeout(5, read=None),
            ) as response:
                if response.is_error:
                    await response.aread()
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        event = json.loads(line.removeprefix("data: "))
                        last_seq = event["seq"]
                        render(
                            event,
                            seen_requests,
                            streamed,
                            historical_approval=(
                                event["type"] in ("approval.requested", "task.approval")
                                and event["seq"] <= watermark
                                and event["data"]["approval_id"] not in pending
                            ),
                        )
        except httpx.HTTPStatusError as error:
            print_http_error(error)
            if error.response.status_code in (401, 404):
                return
            await asyncio.sleep(1)
        except httpx.RequestError:
            print("\n[reconnecting]", flush=True)
            await asyncio.sleep(1)


async def submit(client: httpx.AsyncClient, session_id: str, text: str) -> None:
    result = await submit_control(client, session_id, text)
    if result:
        print(result, flush=True)


def owner_client(base_url: str) -> httpx.AsyncClient:
    """Never send the owner secret through a proxy, redirect, URL or remote plaintext HTTP."""
    url = httpx.URL(base_url)
    if (
        url.scheme not in ("http", "https")
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or (url.scheme == "http" and url.host not in ("127.0.0.1", "::1", "localhost"))
    ):
        raise OwnerAuthError(
            "Use a plain server origin: loopback HTTP or trusted HTTPS, "
            "without credentials, query parameters or a path."
        )
    token = owner_token(get_settings())
    return httpx.AsyncClient(
        base_url=url,
        headers={"Authorization": f"Bearer {token.get_secret_value()}"},
        timeout=10,
        trust_env=False,
        follow_redirects=False,
    )


def _read_key() -> str:
    """Read one keypress in raw mode; arrow keys come back as escape sequences.
    A lone ESC is returned as-is: the follow-up bytes are awaited with a short
    timeout so a bare ESC keypress cannot block forever."""
    import os
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # Read everything through the unbuffered fd: sys.stdin.read would pull the
        # whole escape sequence into its buffer and starve the select/os.read below.
        ch = os.read(fd, 1).decode(errors="replace")
        if ch == "\x1b":
            seq = ""
            while select.select([fd], [], [], 0.05)[0]:
                seq += os.read(fd, 1).decode(errors="replace")
            ch += seq
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch


def pick_model(models: list[dict]) -> str | None:
    """Interactive dropdown: arrows move, Enter selects, q/ESC aborts. Falls back to a
    typed prompt when the terminal is not interactive."""
    if not models:
        return None
    configured = [m for m in models if m["configured"]]
    options = configured or models
    console = Console()
    if not (sys.stdin.isatty() and console.is_terminal):
        choices = [m["id"] for m in options]
        default = next((m["id"] for m in options if m["tier"] == "fast"), choices[0])
        return Prompt.ask("[bold]Select a model[/bold]", choices=choices, default=default)
    index = next((i for i, m in enumerate(options) if m["tier"] == "fast"), 0)
    while True:
        console.clear()
        console.print("[bold]Select a model[/bold]  (up/down + Enter, q/ESC to quit)\n")
        for i, m in enumerate(options):
            style = "reverse" if i == index else ""
            console.print(f"  {m['id']:<42} {m['tier']:<9} {m['billing']}", style=style)
        key = _read_key()
        if key in ("\x1b[A", "\x1bOA"):
            index = (index - 1) % len(options)
        elif key in ("\x1b[B", "\x1bOB"):
            index = (index + 1) % len(options)
        elif key in ("\r", "\n"):
            console.clear()
            return options[index]["id"]
        elif key in ("q", "\x03", "\x1b"):
            console.clear()
            return None


async def select_model(client: httpx.AsyncClient) -> str | None:
    """First-screen provider picker: configured models first, fast tier as default."""
    response = await client.get("/models")
    response.raise_for_status()
    return await asyncio.to_thread(pick_model, response.json())


async def chat(base_url: str, session_id: str | None, *, new: bool = False) -> None:
    async with owner_client(base_url) as client:
        response = await client.get("/runtime")
        response.raise_for_status()
        client.headers["X-Bud-Scope"] = response.json()["scope"]
        if session_id is None:
            model = await select_model(client)
            if model is None:
                print("No model selected; not starting a session.")
                return
            if new:
                response = await client.post(
                    "/sessions", json={"session_id": uuid4().hex, "model": model}
                )
            else:
                response = await client.post("/sessions/primary", json={"model": model})
            response.raise_for_status()
            session_id = response.json()["session_id"]
        else:
            # Authenticate before showing state or starting an endless SSE retry loop.
            await session_snapshot(client, session_id)
        assert session_id is not None
        print(
            f"session {session_id}\nCtrl-D to leave. {HELP}\n"
            "Approval commands always require the exact displayed approval ID."
        )

        follower = asyncio.create_task(follow(client, session_id))
        try:
            while True:
                text = await asyncio.to_thread(input)
                if not text.strip():
                    continue
                try:
                    await submit(client, session_id, text)
                except httpx.HTTPStatusError as error:
                    print_http_error(error)
                except httpx.RequestError as error:
                    print(f"[request failed] {error}")
        except EOFError:
            pass
        finally:
            follower.cancel()
