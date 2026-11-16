"""Owner commands shared by conversation interfaces.

Commands call the existing authenticated core API. They never approve a batch
implicitly, pass connection credentials to the model, or maintain another history.
"""

import json
import re
from uuid import uuid4

import httpx

from bud.core.events import MessageOrigin

HELP = (
    "/status /pause /resume /stop /cancel <message-id> "
    "/edit <message-id> <text> /approve <approval-id> /deny <approval-id> "
    "/answer <approval-id> <text> /connect gmail|calendar /connections /refresh"
)
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


async def session_snapshot(client: httpx.AsyncClient, session_id: str) -> dict:
    response = await client.get(f"/sessions/{session_id}")
    response.raise_for_status()
    return response.json()


def approval_text(approval: dict) -> str:
    """Render every supplied argument; presentation never changes the approved payload."""
    identity = approval.get("reference") or approval["approval_id"]
    titles = {
        "draft_reply": "Save a reply draft (not send)",
        "GMAIL_CREATE_EMAIL_DRAFT": "Save an email draft (not send)",
        "send_email": "Send an email",
        "GMAIL_SEND_EMAIL": "Send an email",
        "create_event": "Create a meeting",
        "GOOGLECALENDAR_CREATE_EVENT": "Create a meeting",
        "move_event": "Move a meeting",
        "GOOGLECALENDAR_PATCH_EVENT": "Move a meeting",
        "workspace_create": "Create a workspace file",
        "workspace_update": "Update a workspace file",
    }
    task = f"Task {approval['reference']}: " if approval.get("reference") else ""
    if approval["tool"] == "ask_user":
        lines = [f"{task}Question: {approval['args'].get('question', '')}"]
        for index, choice in enumerate(approval["args"].get("choices") or [], start=1):
            lines.append(f"  {index}. {choice}")
        lines.append(f"/answer {identity} <your reply>")
        return "\n".join(lines)
    lines = [f"{task}Permission needed: {titles.get(approval['tool'], approval['tool'])}"]
    for name, value in approval["args"].items():
        label = name.replace("_", " ").capitalize()
        if name in ("body", "message_body", "content") and isinstance(value, str):
            lines.append(f"{label} (each line quoted):")
            lines.extend("│ " + line for line in value.split("\n"))
        else:
            lines.append(f"{label}: {json.dumps(value, ensure_ascii=False)}")
    lines.extend((f"/approve {identity}", f"/deny {identity}"))
    return "\n".join(lines)


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
    if state.get("tasks"):
        lines.append("[jobs]")
        lines.extend(
            f"  Task {task['task_id']}: {task['title']} ({task['status']})"
            for task in state["tasks"]
        )
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
    origin: MessageOrigin | None = None,
) -> str | None:
    """Handle a command or enqueue text. Provider adapters supply a stable message ID."""
    if not text.strip():
        return None
    parts = text.split(maxsplit=2)
    source = (origin or MessageOrigin(channel="terminal", client_id="terminal")).model_dump()
    command = parts[0]
    path = f"/sessions/{session_id}"
    if command in ("/help", "/?"):
        return HELP
    if command in ("/approve", "/deny"):
        if len(parts) != 2 or not IDENTIFIER.fullmatch(parts[1]):
            return f"[usage] {command} <approval-id>; use /status to see each exact action."
        state = await session_snapshot(client, session_id)
        references = {
            identity
            for item in state["pending_approvals"]
            for identity in (item["approval_id"], item.get("reference"))
            if identity is not None
        }
        if parts[1] not in references:
            return "[not pending] No permission was granted. Use /status for current actions."
        response = await client.post(
            f"{path}/approvals/{parts[1]}",
            json={"approved": command == "/approve", "origin": source},
        )
        if response.status_code == 409:
            return f"[needs an answer] {parts[1]} is a question; /answer {parts[1]} <reply>."
        response.raise_for_status()
        label = "approved" if command == "/approve" else "denied"
        return f"[{label}] {parts[1]}" if response.json()["applied"] else "[already decided]"
    if command == "/answer":
        if len(parts) != 3 or not IDENTIFIER.fullmatch(parts[1]):
            return "[usage] /answer <approval-id> <reply>; use /status to see the question."
        state = await session_snapshot(client, session_id)
        references = {
            identity
            for item in state["pending_approvals"]
            if item["tool"] == "ask_user"
            for identity in (item["approval_id"], item.get("reference"))
            if identity is not None
        }
        if parts[1] not in references:
            return "[not a pending question] Use /status for current questions."
        response = await client.post(
            f"{path}/approvals/{parts[1]}/answer",
            json={"text": parts[2], "origin": source},
        )
        if response.status_code == 409:
            return f"[needs a decision] {parts[1]} is an action; /approve or /deny it."
        response.raise_for_status()
        return f"[answered] {parts[1]}" if response.json().get("answered") else "[already answered]"
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
        f"{path}/messages",
        json={"text": text, "message_id": message_id or uuid4().hex, "origin": source},
    )
    response.raise_for_status()
    return "[duplicate message]" if response.json().get("duplicate") else None
