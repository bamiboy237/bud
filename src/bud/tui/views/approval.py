"""1-key interactive approval card for Bud TUI.

Provides an executive clean action card with natural typography, inspired by
Grok Bot and Town AI.
Eliminates all ASCII rounded borders (╭─, ╰─), warning emojis (⚠), raw tool
identifiers (GMAIL_SEND_EMAIL), and JSON curly braces ({}).
"""

from __future__ import annotations

from typing import Any

from bud.tui.state import ApprovalState, StateStore
from bud.tui.terminal import truncate_visible
from bud.tui.views.base import View


def friendly_action_title(tool: str, args: dict[str, Any]) -> str:
    """Produce an executive human-readable action title."""
    t = tool.lower()
    if "email" in t or "draft" in t:
        recipient = args.get("to") or args.get("recipient") or ""
        return f"Send email to {recipient}" if recipient else "Send email"
    if "event" in t or "calendar" in t:
        title = args.get("title") or args.get("summary") or ""
        return f"Schedule meeting: \"{title}\"" if title else "Schedule meeting"
    if "issue" in t:
        title = args.get("title") or ""
        return f"Create issue: \"{title}\"" if title else "Create GitHub issue"
    if "pull_request" in t or "pr" in t:
        title = args.get("title") or ""
        return f"Create pull request: \"{title}\"" if title else "Create pull request"
    if "slack" in t or "channel" in args:
        target = args.get("channel") or args.get("to") or ""
        return f"Send Slack message to {target}" if target else "Send Slack message"
    if "delete" in t or "remove" in t:
        target = args.get("path") or args.get("title") or ""
        return f"Delete {target}" if target else "Delete item"
    if "create" in t:
        path = args.get("path") or ""
        return f"Create file: {path}" if path else "Create file"
    if "update" in t:
        path = args.get("path") or ""
        return f"Update file: {path}" if path else "Update file"

    cleaned = (
        tool.replace("COMPOSIO_", "")
        .replace("GOOGLECALENDAR_", "")
        .replace("GMAIL_", "")
        .replace("WORKSPACE_", "")
        .replace("_", " ")
        .capitalize()
    )
    return cleaned


class ApprovalView(View):
    """Renders a clean executive approval card without ASCII boxes or curly braces."""

    def __init__(self, approval: ApprovalState, store: StateStore) -> None:
        super().__init__()
        self.approval: ApprovalState = approval
        self.store: StateStore = store

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme
        appr = self.approval

        # Title
        title = friendly_action_title(appr.tool, appr.args)
        header_text = f"Action Required: {title}"
        header_line = f"  {theme.format_primary(header_text, bold=True)}"

        lines = ["", truncate_visible(header_line, width)]
        dim_pipe = theme.format_dim("│")

        # Arguments in human readable layout
        flat_args: list[tuple[str, str]] = []
        for k, v in appr.args.items():
            if isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    if not isinstance(sub_v, (dict, list)):
                        flat_args.append((f"{k} {sub_k}", str(sub_v)))
            elif not isinstance(v, list):
                flat_args.append((k, str(v)))

        for key_name, val_raw in flat_args:
            key_label = key_name.replace("_", " ").capitalize()
            val_clean = val_raw.replace("{", "").replace("}", "")
            if key_name.lower() in ("body", "message", "message_body", "content"):
                label_line = f"    {dim_pipe} {theme.format_dim(key_label + ':')}"
                lines.append(truncate_visible(label_line, width))
                body_lines = val_clean.splitlines()
                for bline in body_lines[:6]:
                    content_line = f"    {dim_pipe}   {theme.format_secondary(bline)}"
                    lines.append(truncate_visible(content_line, width))
                if len(body_lines) > 6:
                    more_text = f"… ({len(body_lines) - 6} more lines)"
                    more_line = f"    {dim_pipe}   {theme.format_dim(more_text)}"
                    lines.append(truncate_visible(more_line, width))
            else:
                arg_text = (
                    f"    {dim_pipe} {theme.format_dim(key_label + ':')} "
                    f"{theme.format_primary(val_clean)}"
                )
                lines.append(truncate_visible(arg_text, width))

        # Bottom action shortcuts
        key_y = theme.format_success("[y] Approve", bold=True)
        key_n = theme.format_error("[n] Deny", bold=True)
        key_esc = theme.format_muted("[esc] Cancel")
        action_line = f"    {key_y}   {key_n}   {key_esc}"
        lines.append("")
        lines.append(truncate_visible(action_line, width))
        lines.append("")

        return lines
