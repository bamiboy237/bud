"""Terminal client for a running bud server.
It streams a session's events over server-sent events, prints them, and sends each line the
user types as a message.
"""

import asyncio
import json
from uuid import uuid4

import httpx


def render(event: dict, seen_requests: set[str]) -> None:
    data = event["data"]
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
            print(data["text"], end="", flush=True)
        case "tool.call":
            print(f"\n[tool] {data['tool']}({data['args']})", flush=True)
        case "tool.result":
            print(f"[result] {data['result'][:200]}", flush=True)
        case "approval.requested":
            print(f"\n[approval needed] {data['tool']}({data['args']})", flush=True)
            print("Type /approve or /deny.", flush=True)
        case "approval.granted":
            print("[approved]", flush=True)
        case "approval.denied":
            print("[denied]", flush=True)
        case "run.waiting" | "run.completed":
            print(flush=True)
        case "run.failed":
            print(f"\n[run failed] {data['reason']}", flush=True)


async def refresh_pending(client: httpx.AsyncClient, session_id: str, pending: set[str]) -> dict:
    response = await client.get(f"/sessions/{session_id}")
    response.raise_for_status()
    snapshot = response.json()
    pending.clear()
    pending.update(approval["approval_id"] for approval in snapshot["pending_approvals"])
    return snapshot


def print_http_error(error: httpx.HTTPStatusError) -> None:
    response = error.response
    try:
        detail = response.json().get("detail", response.reason_phrase)
    except (ValueError, AttributeError):
        detail = response.reason_phrase
    print(f"[HTTP {response.status_code}] {detail}", flush=True)


async def follow(client: httpx.AsyncClient, session_id: str, pending: set[str]) -> None:
    last_seq = 0
    seen_requests: set[str] = set()
    while True:
        try:
            snapshot = await refresh_pending(client, session_id, pending)
            approval_seq = snapshot["last_seq"]
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
                        render(event, seen_requests)
                        if event["seq"] <= approval_seq:
                            continue
                        if event["type"] == "approval.requested":
                            pending.add(event["data"]["approval_id"])
                        elif event["type"] in ("approval.granted", "approval.denied"):
                            pending.discard(event["data"]["approval_id"])
                        elif event["type"] == "run.cancelled":
                            pending.clear()
                            snapshot = await refresh_pending(client, session_id, pending)
                            approval_seq = snapshot["last_seq"]
        except httpx.HTTPStatusError as error:
            print_http_error(error)
            await asyncio.sleep(1)
        except httpx.RequestError:
            print("\n[reconnecting]", flush=True)
            await asyncio.sleep(1)


async def resolve(
    client: httpx.AsyncClient, session_id: str, pending: set[str], approved: bool
) -> None:
    if not pending:
        print("[nothing is waiting for approval]")
    for approval_id in list(pending):
        response = await client.post(
            f"/sessions/{session_id}/approvals/{approval_id}", json={"approved": approved}
        )
        response.raise_for_status()


async def status(client: httpx.AsyncClient, session_id: str, pending: set[str]) -> None:
    snapshot = await refresh_pending(client, session_id, pending)
    queue_state = "paused" if snapshot["queue_paused"] else "open"
    active = "yes" if snapshot["run_active"] else "no"
    print(f"[status] {snapshot['status']}; queue {queue_state}; run active: {active}")
    print("[queue]")
    for message in snapshot["queued_messages"]:
        preview = " ".join(message["text"].split())
        if len(preview) > 80:
            preview = preview[:77] + "..."
        print(f"  {message['message_id']}: {preview}")
    if not snapshot["queued_messages"]:
        print("  empty")
    print(f"[pending approvals] {', '.join(sorted(pending)) or 'none'}")
    if snapshot["recent_runs"]:
        print("[recent runs]")
        for run in snapshot["recent_runs"]:
            reason = f"; {run['reason']}" if run.get("reason") else ""
            print(f"  {run['run_id']}: {run['status']}{reason}")


async def submit(client: httpx.AsyncClient, session_id: str, pending: set[str], text: str) -> None:
    parts = text.split(maxsplit=2)
    command = parts[0]
    path = f"/sessions/{session_id}"
    match command:
        case "/approve" | "/deny" | "/status" | "/pause" | "/resume" | "/stop":
            if len(parts) != 1:
                print(f"[usage] {command}")
                return
            match command:
                case "/approve" | "/deny":
                    await resolve(client, session_id, pending, command == "/approve")
                    return
                case "/status":
                    await status(client, session_id, pending)
                    return
                case "/pause" | "/resume":
                    response = await client.post(f"{path}/queue/{command[1:]}")
                    label = "queue paused" if command == "/pause" else "queue resumed"
                case "/stop":
                    response = await client.post(f"{path}/stop")
                    response.raise_for_status()
                    label = (
                        "stop requested; queue paused; authorized actions may still finish"
                        if response.json()["stop_requested"]
                        else "queue paused; no active run to stop"
                    )
        case "/cancel":
            if len(parts) != 2:
                print("[usage] /cancel <message-id>")
                return
            response = await client.delete(f"{path}/queue/{parts[1]}")
            response.raise_for_status()
            label = (
                f"message cancelled: {parts[1]}"
                if response.json()["cancelled"]
                else f"message is no longer queued: {parts[1]}"
            )
        case "/edit":
            if len(parts) != 3:
                print("[usage] /edit <message-id> <replacement text>")
                return
            response = await client.patch(f"{path}/queue/{parts[1]}", json={"text": parts[2]})
            label = f"message edited: {parts[1]}"
        case _:
            if command.startswith("/"):
                print("[unknown command] /status /pause /resume /stop /cancel /edit /approve /deny")
                return
            response = await client.post(
                f"{path}/messages", json={"text": text, "message_id": uuid4().hex}
            )
            response.raise_for_status()
            if response.json().get("duplicate"):
                print("[duplicate message]")
            return
    response.raise_for_status()
    print(f"[{label}]")


async def chat(base_url: str, session_id: str | None) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        if session_id is None:
            response = await client.post("/sessions")
            response.raise_for_status()
            session_id = response.json()["session_id"]
        print(f"session {session_id}\nCtrl-D to leave. /status for state and queue.")

        pending: set[str] = set()
        follower = asyncio.create_task(follow(client, session_id, pending))
        try:
            while True:
                text = await asyncio.to_thread(input)
                if not text.strip():
                    continue
                try:
                    await submit(client, session_id, pending, text)
                except httpx.HTTPStatusError as error:
                    print_http_error(error)
                except httpx.RequestError as error:
                    print(f"[request failed] {error}")
        except EOFError:
            pass
        finally:
            follower.cancel()
