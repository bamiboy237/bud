"""Owner commands shared by conversation interfaces.

Commands call the existing authenticated core API. They never approve a batch
implicitly, pass connection credentials to the model, or maintain another history.
"""

import json
import re
from uuid import uuid4

import httpx

HELP = (
    "/status /pause /resume /stop /cancel <message-id> "
    "/edit <message-id> <text> /approve <approval-id> /deny <approval-id> "
    "/connect gmail|calendar /connections /refresh"
)
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


async def session_snapshot(client: httpx.AsyncClient, session_id: str) -> dict:
    response = await client.get(f"/sessions/{session_id}")
    response.raise_for_status()
    return response.json()


def approval_text(approval: dict) -> str:
    identity = approval["approval_id"]
    return (
        f"[approval {identity}] {approval['tool']}\n"
        f"{json.dumps(approval['args'], ensure_ascii=False)}\n"
        f"/approve {identity}\n/deny {identity}"
    )


def connection_text(state: dict) -> str:
    lines = [f"[{toolkit}] {value}" for toolkit, value in state["statuses"].items()]
    lines.append(
        "[restart required] Stop and restart bud serve, then start a new conversation."
        if state["restart_required"]
        else "[connections current] Tool refresh requires a full server process restart."
    )
    return "\n".join(lines)


def status_text(state: dict) -> str:
    queue = "paused" if state["queue_paused"] else "open"
    active = "yes" if state["run_active"] else "no"
    lines = [
        f"[status] {state['status']}; queue {queue}; run active: {active}",
        "[queue]",
    ]
    for message in state["queued_messages"]:
        preview = " ".join(message["text"].split())
        lines.append(f"  {message['message_id']}: {preview[:80]}")
    if not state["queued_messages"]:
        lines.append("  empty")
    lines.append("[pending approvals]")
    lines.extend(approval_text(approval) for approval in state["pending_approvals"])
    if not state["pending_approvals"]:
        lines.append("  none")
    if state["recent_runs"]:
        lines.append("[recent runs]")
        for run in state["recent_runs"]:
            reason = f"; {run['reason']}" if run.get("reason") else ""
            phase = run["status"]
            if phase == "awaiting_approval" and run is not state["recent_runs"][-1]:
                phase = "deferred (historical phase; not a pending approval)"
            lines.append(f"  {run['run_id']}: {phase}{reason}")
    return "\n".join(lines)


async def submit(
    client: httpx.AsyncClient,
    session_id: str,
    text: str,
    *,
    message_id: str | None = None,
) -> str | None:
    """Handle a command or enqueue text. Provider adapters supply a stable message ID."""
    if not text.strip():
        return None
    parts = text.split(maxsplit=2)
    command = parts[0]
    path = f"/sessions/{session_id}"
    if command in ("/help", "/?"):
        return HELP
    if command in ("/approve", "/deny"):
        if len(parts) != 2 or not IDENTIFIER.fullmatch(parts[1]):
            return f"[usage] {command} <approval-id>; use /status to see each exact action."
        state = await session_snapshot(client, session_id)
        if parts[1] not in {item["approval_id"] for item in state["pending_approvals"]}:
            return "[not pending] No permission was granted. Use /status for current actions."
        response = await client.post(
            f"{path}/approvals/{parts[1]}", json={"approved": command == "/approve"}
        )
        response.raise_for_status()
        label = "approved" if command == "/approve" else "denied"
        return f"[{label}] {parts[1]}" if response.json()["applied"] else "[already decided]"
    if command == "/connect":
        if len(parts) != 2 or parts[1] not in ("gmail", "calendar"):
            return "[usage] /connect gmail|calendar"
        response = await client.post(f"/connections/{parts[1]}/connect")
        response.raise_for_status()
        # Immediate owner UI only: no model messages, memory or event log.
        return (
            f"[sign in to {parts[1]}] {response.json()['url']}\n"
            "[restart required] After sign-in, /refresh; restart bud serve; start a new chat."
        )
    if command in ("/connections", "/refresh"):
        if len(parts) != 1:
            return f"[usage] {command}"
        response = (
            await client.get("/connections")
            if command == "/connections"
            else await client.post("/connections/refresh")
        )
        response.raise_for_status()
        return connection_text(response.json())
    if command in ("/status", "/pause", "/resume", "/stop"):
        if len(parts) != 1:
            return f"[usage] {command}"
        if command == "/status":
            return status_text(await session_snapshot(client, session_id))
        if command == "/stop":
            response = await client.post(f"{path}/stop")
            response.raise_for_status()
            return (
                "[stop requested; queue paused; authorized actions may still finish]"
                if response.json()["stop_requested"]
                else "[queue paused; no active run to stop]"
            )
        response = await client.post(f"{path}/queue/{command[1:]}")
        response.raise_for_status()
        return "[queue paused]" if command == "/pause" else "[queue resumed]"
    if command in ("/cancel", "/edit"):
        count = 2 if command == "/cancel" else 3
        if len(parts) != count or not IDENTIFIER.fullmatch(parts[1]):
            tail = "" if command == "/cancel" else " <replacement text>"
            return f"[usage] {command} <message-id>{tail}"
        response = (
            await client.delete(f"{path}/queue/{parts[1]}")
            if command == "/cancel"
            else await client.patch(f"{path}/queue/{parts[1]}", json={"text": parts[2]})
        )
        response.raise_for_status()
        if command == "/edit":
            return f"[message edited] {parts[1]}"
        return (
            f"[message cancelled] {parts[1]}"
            if response.json()["cancelled"]
            else f"[message is no longer queued] {parts[1]}"
        )
    if command.startswith("/"):
        return f"[unknown command] {HELP}"
    response = await client.post(
        f"{path}/messages", json={"text": text, "message_id": message_id or uuid4().hex}
    )
    response.raise_for_status()
    return "[duplicate message]" if response.json().get("duplicate") else None
