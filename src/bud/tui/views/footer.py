"""Muted one-line status bar for Bud TUI displaying model, metrics, and hotkeys.

Ultra-quiet, calm status bar inspired by Emil Kowalski and pi-tui.
Eliminates neon circles, warning emojis, and harsh vertical pipes.
"""

from __future__ import annotations

from bud.tui.state import InputMode, StateStore
from bud.tui.terminal import truncate_visible, visible_width
from bud.tui.views.base import View


def format_token_count(tokens: int) -> str:
    """Format token count cleanly (e.g. 1.2k)."""
    if tokens < 1000:
        return f"{tokens}"
    return f"{tokens / 1000:.1f}k"


class FooterView(View):
    """Renders a single-line muted status bar."""

    def __init__(self, store: StateStore) -> None:
        super().__init__()
        self.store: StateStore = store

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme

        # Status segment. Paused is persistent state, not a scrollback event.
        st = self.store.status
        if st in ("thinking", "streaming"):
            status_text = theme.format_accent(st)
        elif st.startswith("tool:"):
            status_text = theme.format_accent("working")
        elif st == "waiting_approval":
            if any(a.tool == "ask_user" for a in self.store.pending_approvals):
                status_text = theme.format_accent("answer needed")
            else:
                status_text = theme.format_accent("approval needed")
        elif st == "error":
            status_text = theme.format_error(st)
        elif self.store.queue_paused or st == "paused":
            status_text = theme.format_warning("paused")
        else:
            status_text = theme.format_dim("idle")

        items: list[str] = [status_text]

        queued = len(self.store.queued_messages)
        if queued:
            items.append(theme.format_warning(f"{queued} queued"))

        # Optional segments, in the order they should be dropped when space is tight.
        # The model lives in the composer meta row; the footer carries state only.
        optional: list[str] = []
        tot_tokens = self.store.input_tokens + self.store.output_tokens
        if tot_tokens > 0:
            optional.append(theme.format_dim(f"{format_token_count(tot_tokens)} tokens"))
        lat = self.store.last_latency_ms
        if lat > 0:
            optional.append(theme.format_dim(f"{int(lat)}ms"))

        # Key hints for the active mode; the footer is the single hint home.
        if self.store.mode == InputMode.APPROVAL:
            hint = theme.format_dim("y approve · n deny · Esc cancel")
            condensed = theme.format_dim("y · n · Esc")
        elif self.store.mode == InputMode.QUESTION:
            hint = theme.format_dim("↑/↓ navigate · Enter select · Esc cancel")
            condensed = theme.format_dim("↑/↓ · Enter")
        elif self.store.pending_approvals:
            hint = theme.format_accent("approval pending")
            condensed = theme.format_accent("approval pending")
        elif st in ("thinking", "streaming") or st.startswith("tool:"):
            hint = theme.format_dim("Tab tools · Esc stop · /help · Ctrl-D exit")
            condensed = theme.format_dim("Esc stop · /help")
        else:
            hint = theme.format_dim("Tab tools · /help · Ctrl-D exit")
            condensed = theme.format_dim("/help")

        dot = theme.format_dim("·")
        # 2-col safety margin from the right edge prevents emulator wrap.
        target_w = max(20, width - 2)

        def fits(left: str, right: str) -> bool:
            return visible_width(left) + 2 + visible_width(right) <= target_w

        def assemble(parts: list[str]) -> str:
            return f"  {f' {dot} '.join(parts)}"

        hint_str, condensed_str = hint, condensed
        while True:
            left = assemble(items + optional)
            if fits(left, hint_str):
                pad = max(1, target_w - visible_width(left) - visible_width(hint_str))
                return [f"{left}{' ' * pad}{hint_str}"]
            if optional:
                optional.pop()
                continue
            left = assemble(items)
            if fits(left, condensed_str):
                pad = max(1, target_w - visible_width(left) - visible_width(condensed_str))
                return [f"{left}{' ' * pad}{condensed_str}"]
            return [truncate_visible(left, target_w)]
