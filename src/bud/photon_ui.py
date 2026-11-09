"""Conversational iMessage presentation; permission mechanics stay server-side."""

import json


def _value(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def action_preview(approval: dict, *, decision: bool = True) -> str:
    args, tool = approval["args"], approval["tool"]
    target = args.get("path")
    titles = {
        "draft_reply": "Save this reply as a draft?",
        "GMAIL_CREATE_EMAIL_DRAFT": "Save this as an email draft?",
        "send_email": "Send this email?",
        "GMAIL_SEND_EMAIL": "Send this email?",
        "create_event": "Add this meeting to your calendar?",
        "GOOGLECALENDAR_CREATE_EVENT": "Add this meeting to your calendar?",
        "move_event": "Move this meeting?",
        "GOOGLECALENDAR_PATCH_EVENT": "Move this meeting?",
        "workspace_create": f"Create {target}?",
        "workspace_update": f"Replace the contents of {target}?",
    }
    title = titles.get(tool, tool.replace("_", " ").capitalize() + "?")
    if approval.get("reference"):
        title = f"Task {approval['reference']}: {title}"
    details = []
    for key, value in args.items():
        # Preconditions prevent races; they do not describe a user-visible change.
        # Their full values remain bound to the proposal and available in terminal status.
        if key in ("expected_etag", "expected_revision"):
            continue
        if key == "path" and tool in ("workspace_create", "workspace_update"):
            continue
        if key in ("content", "body", "message_body") and isinstance(value, str):
            details.append(value)
        else:
            label = {
                "recipient_email": "To",
                "recipient": "To",
                "recipients": "To",
                "thread_id": "Thread",
                "calendar_id": "Calendar",
                "start_datetime": "Start",
                "end_datetime": "End",
                "start_time": "Start",
                "end_time": "End",
                "send_updates": "Notify attendees",
            }.get(key, key.replace("_", " ").capitalize())
            details.append(f"{label}: {_value(value)}")
    parts = [title if decision else title.removesuffix("?")]
    if details:
        parts.append("\n".join(details))
    if decision:
        identity = approval.get("reference") or approval["approval_id"]
        parts.append(f"/approve {identity}\n/deny {identity}")
    return "\n\n".join(parts)


def status_text(state: dict) -> str:
    if state["status"] == "needs_review":
        return "I'm stopped because an earlier action needs checking. I haven't retried it."
    pending = state["pending_approvals"]
    if pending:
        return f"I'm waiting for your decision on {len(pending)} changes."
    count = len(state["queued_messages"])
    if state["queue_paused"]:
        return (
            f"Paused, with {count} request{'s' if count != 1 else ''} waiting."
            if count
            else "Paused."
        )
    if state["run_active"]:
        return "I'm on it." + (f" There are {count} more requests waiting." if count else "")
    jobs = [
        task
        for task in state.get("tasks", [])
        if task["status"] not in {"completed", "failed", "cancelled"}
    ]
    if jobs:
        return "\n".join(
            f"Task {task['task_id']}: {task['title']} ({task['status'].replace('_', ' ')})"
            for task in jobs
        )
    return "All caught up."
