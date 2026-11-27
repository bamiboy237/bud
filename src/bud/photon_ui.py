"""Conversational iMessage presentation; permission mechanics stay server-side."""

import json
import re
from pathlib import Path

from bud.harness.ask import question_lines


def _value(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _routine_schedule(cron: str) -> str:
    fields = cron.lower().split()
    fallback = f"Custom cron: {cron}"
    if len(fields) != 5:
        return fallback
    minute, hour, day, month, weekday = fields
    if month != "*" or not re.fullmatch(r"[0-9]{1,2} [0-9]{1,2}", f"{hour} {minute}"):
        return fallback
    if int(hour) > 23 or int(minute) > 59:
        return fallback
    time = f"{int(hour):02d}:{int(minute):02d}"
    if day == "*" and weekday == "*":
        return f"Daily at {time}"
    if day == "*" and weekday in ("1-5", "mon-fri", "1,2,3,4,5"):
        return f"Weekdays at {time}"
    if day == "*":
        weekdays = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
        for number, name in enumerate(weekdays):
            if weekday in (str(number), name[:3].lower()) or (number == 0 and weekday == "7"):
                return f"Every {name} at {time}"
    if weekday == "*" and re.fullmatch(r"[0-9]{1,2}", day) and 1 <= int(day) <= 31:
        return f"Monthly on day {int(day)} at {time}"
    return fallback


def action_preview(approval: dict, *, decision: bool = True) -> str:
    args, tool = approval["args"], approval["tool"]
    if tool == "ask_user":
        parts = ["\n".join(question_lines(args))]
        if approval.get("reference"):
            parts[0] = f"Task {approval['reference']}: {parts[0]}"
        identity = approval.get("reference") or approval["approval_id"]
        parts.append(f"Reply here, or /answer {identity} <reply>")
        return "\n\n".join(parts)
    target = args.get("path")
    # Bud asks in its own voice, the same "Can I ...?" the Mac app uses.
    titles = {
        "draft_reply": "Can I save this reply as a draft?",
        "GMAIL_CREATE_EMAIL_DRAFT": "Can I save this as an email draft?",
        "send_email": "Can I send this email?",
        "GMAIL_SEND_EMAIL": "Can I send this email?",
        "create_event": "Can I add this meeting to your calendar?",
        "GOOGLECALENDAR_CREATE_EVENT": "Can I add this meeting to your calendar?",
        "move_event": "Can I move this meeting?",
        "GOOGLECALENDAR_PATCH_EVENT": "Can I move this meeting?",
        "workspace_create": f"Can I save {target}?",
        "workspace_update": f"Can I replace {target}?",
        "enable_browser": "Can I use your Chrome?",
        "open_chrome": "Can I open Chrome?",
        "music_control": {
            "play": "Can I play music?",
            "pause": "Can I pause the music?",
            "next": "Can I skip to the next song?",
            "previous": "Can I go back a song?",
        }.get(str(args.get("action")), "Can I control Music?"),
        "open_in_app": f"Can I open {Path(str(args.get('path', ''))).name or 'a document'}?",
        "notes_create": f"Can I create the note “{args.get('title', '')}”?",
        "reminders_add": f"Can I add the reminder “{args.get('title', '')}”?",
        "read_local_file": f"Can I read {Path(str(target or '')).name or 'one of your files'}?",
        "list_local_folder": f"Can I look in {Path(str(target or '')).name or 'a folder'}?",
    }
    title = titles.get(tool, "Can I " + tool.replace("_", " ").lower() + "?")
    details = []
    if tool == "create_routine":
        args = {
            key: value for key, value in args.items() if isinstance(value, str) and value.strip()
        }
        name = args.pop("title", "").strip()
        title = f"Can I schedule “{name}”?" if name else "Can I schedule a routine?"
        cron = args.pop("cron", "").strip()
        timezone = args.pop("timezone", "").strip()
        if cron:
            zone = f" ({timezone})" if timezone else ""
            details.append(f"Schedule: {_routine_schedule(cron)}{zone}")
    if approval.get("reference"):
        title = f"Task {approval['reference']}: {title}"
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
    # Facts Bud looked up (the caution, the site a tab shows) and the reviewer's reason.
    review = approval.get("review") or {}
    for key, label in (("details", ""), ("reason", "Why: ")):
        if isinstance(review.get(key), str) and review[key]:
            details.append(label + review[key])
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
