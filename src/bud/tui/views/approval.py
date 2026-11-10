"""1-key interactive approval cards for Bud TUI.

Allows immediate single-key authorization:
  [y] Approve
  [n] Deny
  [Esc] Dismiss
No UUID copying or typing required.
"""

from __future__ import annotations

import json

from bud.tui.state import ApprovalState, StateStore
from bud.tui.terminal import truncate_visible, visible_width
from bud.tui.views.base import View


def friendly_tool_name(tool: str) -> str:
    titles: dict[str, str] = {
        "draft_reply": "Save email reply draft",
        "send_email": "Send an email",
        "GMAIL_SEND_EMAIL": "Send an email",
        "create_event": "Create calendar meeting",
        "GOOGLECALENDAR_CREATE_EVENT": "Create calendar meeting",
        "move_event": "Move calendar meeting",
        "GOOGLECALENDAR_PATCH_EVENT": "Move calendar meeting",
        "workspace_create": "Create a workspace file",
        "workspace_update": "Update a workspace file",
    }
    return titles.get(tool, tool)


class ApprovalView(View):
    """Renders a single-key approval card."""

    def __init__(self, approval: ApprovalState, store: StateStore) -> None:
        super().__init__()
        self.approval: ApprovalState = approval
        self.store: StateStore = store

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme
        card_w = max(40, min(width - 2, 74))
        inner_w = card_w - 4

        # Glyphs
        top_l, top_r = "╭─", "╮"
        bot_l, bot_r = "╰─", "╯"
        mid_l, mid_r = "├─", "┤"
        pipe = "│"
        horiz = "─"

        # Colors
        c_border = theme.format_border

        def c_title(s: str) -> str:
            return theme.format_warning(s, bold=True)

        def c_key_y(s: str) -> str:
            return theme.format_success(s, bold=True)

        def c_key_n(s: str) -> str:
            return theme.format_error(s, bold=True)

        def c_key_esc(s: str) -> str:
            return theme.format_muted(s)

        c_val = theme.format_primary
        c_dim = theme.format_dim

        # Title
        title_text = f" ⚠ Permission Required: {friendly_tool_name(self.approval.tool)} "
        title_w = visible_width(title_text)
        fill_top = max(0, card_w - 4 - title_w)
        top_line = f"{c_border(top_l)}{c_title(title_text)}{c_border(horiz * fill_top + top_r)}"

        content_lines: list[str] = []

        # Tool identifier and ref
        ref_text = f" (ref: {self.approval.reference})" if self.approval.reference else ""
        tool_line = f"Tool: {self.approval.tool}{ref_text}"
        tool_trunc = truncate_visible(tool_line, inner_w)
        pad = max(0, inner_w - visible_width(tool_trunc))
        content_lines.append(f"{c_border(pipe)}  {c_val(tool_trunc)}{' ' * pad}  {c_border(pipe)}")

        # Arguments
        for key, val in self.approval.args.items():
            label = key.replace("_", " ").capitalize() + ":"
            if key in ("body", "message_body", "content") and isinstance(val, str):
                pad_label = max(0, inner_w - visible_width(label))
                content_lines.append(
                    f"{c_border(pipe)}  {c_dim(label)}{' ' * pad_label}  {c_border(pipe)}"
                )
                for bline in val.splitlines()[:5]:
                    btrunc = truncate_visible(f"  │ {bline}", inner_w)
                    bpad = max(0, inner_w - visible_width(btrunc))
                    sec_btrunc = theme.format_secondary(btrunc)
                    content_lines.append(
                        f"{c_border(pipe)}  {sec_btrunc}{' ' * bpad}  {c_border(pipe)}"
                    )
                if len(val.splitlines()) > 5:
                    more = f"  … ({len(val.splitlines()) - 5} more lines)"
                    mpad = max(0, inner_w - visible_width(more))
                    content_lines.append(
                        f"{c_border(pipe)}  {c_dim(more)}{' ' * mpad}  {c_border(pipe)}"
                    )
            else:
                val_str = (
                    json.dumps(val, ensure_ascii=False)
                    if isinstance(val, (dict, list))
                    else str(val)
                )
                line_str = f"{label} {val_str}"
                line_trunc = truncate_visible(line_str, inner_w)
                pad = max(0, inner_w - visible_width(line_trunc))
                sec_trunc = theme.format_secondary(line_trunc)
                content_lines.append(f"{c_border(pipe)}  {sec_trunc}{' ' * pad}  {c_border(pipe)}")

        # Divider
        mid_fill = max(0, card_w - 4)
        mid_line = f"{c_border(mid_l + horiz * mid_fill + mid_r)}"

        # Action shortcuts
        actions = (
            f" {c_key_y('[y] Approve')}   {c_key_n('[n] Deny')}   {c_key_esc('[Esc] Cancel')} "
        )
        actions_w = visible_width(" [y] Approve   [n] Deny   [Esc] Cancel ")
        actions_pad = max(0, inner_w - actions_w)
        action_line = f"{c_border(pipe)}  {actions}{' ' * actions_pad}  {c_border(pipe)}"

        # Bottom line
        bot_fill = max(0, card_w - 4)
        bot_line = f"{c_border(bot_l + horiz * bot_fill + bot_r)}"

        return [top_line, *content_lines, mid_line, action_line, bot_line]
