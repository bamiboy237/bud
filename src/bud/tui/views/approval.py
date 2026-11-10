"""1-key interactive approval card for Bud TUI.

Provides an executive clean action card with natural typography and hairline containment,
inspired by OpenCode, Grok Bot, and Town AI.
Eliminates raw developer plumbing (GMAIL_SEND_EMAIL, {} curly braces, warning emojis)
in favor of structured human-readable fields framed in subtle hairline borders.
"""

from __future__ import annotations

from typing import Any

from bud.tui.state import ApprovalState, StateStore
from bud.tui.views.base import View
from bud.tui.views.box import BoxStyle, render_box


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
    """Renders a clean executive approval card inside hairline borders."""

    def __init__(self, approval: ApprovalState, store: StateStore) -> None:
        super().__init__()
        self.approval: ApprovalState = approval
        self.store: StateStore = store

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme
        appr = self.approval
        border_style: BoxStyle = getattr(self.store, "border_style", "rounded")

        # Action title
        title = friendly_action_title(appr.tool, appr.args)
        card_title = f"Action Required: {title}"

        body_lines: list[str] = []

        # Flatten arguments into readable label-value pairs
        flat_args: list[tuple[str, str]] = []
        for k, v in appr.args.items():
            if isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    if not isinstance(sub_v, (dict, list)):
                        flat_args.append((f"{k} {sub_k}", str(sub_v)))
            elif isinstance(v, list):
                clean_items = [str(x) for x in v if not isinstance(x, (dict, list))]
                if clean_items:
                    flat_args.append((k, ", ".join(clean_items)))
            else:
                flat_args.append((k, str(v)))

        for key_name, val_raw in flat_args:
            key_label = key_name.replace("_", " ").capitalize()
            val_clean = val_raw.replace("{", "").replace("}", "")
            if key_name.lower() in ("body", "message", "message_body", "content"):
                body_lines.append(f"{theme.format_dim(key_label + ':')}")
                content_lines = val_clean.splitlines()
                for bline in content_lines[:6]:
                    body_lines.append(f"  {theme.format_secondary(bline)}")
                if len(content_lines) > 6:
                    body_lines.append(
                        f"  {theme.format_dim(f'… ({len(content_lines) - 6} more lines)')}"
                    )
            else:
                body_lines.append(
                    f"{theme.format_dim(key_label + ':')} {theme.format_primary(val_clean)}"
                )

        # Shortcuts line inside the card
        key_y = theme.format_success("[y] Approve", bold=True)
        key_n = theme.format_error("[n] Deny", bold=True)
        key_esc = theme.format_muted("[esc] Cancel")
        action_line = f"{key_y}   {key_n}   {key_esc}"

        if body_lines:
            body_lines.append("")
        body_lines.append(action_line)

        return render_box(
            title=card_title,
            body_lines=body_lines,
            width=width,
            theme=theme,
            style=border_style,
        )
