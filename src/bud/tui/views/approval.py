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
    if "create" in t:
        path = args.get("path") or ""
        return f"Create file: {path}" if path else "Create file"
    if "update" in t:
        path = args.get("path") or ""
        return f"Update file: {path}" if path else "Update file"

    cleaned = (
        tool.replace("GOOGLECALENDAR_", "")
        .replace("GMAIL_", "")
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
        for k, v in appr.args.items():
            if isinstance(v, (dict, list)):
                continue
            key_label = k.replace("_", " ").capitalize()
            if k in ("body", "message", "message_body", "content"):
                label_line = f"    {dim_pipe} {theme.format_dim(key_label + ':')}"
                lines.append(truncate_visible(label_line, width))
                body_lines = str(v).splitlines()
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
                    f"{theme.format_primary(str(v))}"
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
