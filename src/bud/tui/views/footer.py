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
        self.store.subscribe(self.mark_dirty)

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme
        dot = theme.format_dim("·")

        # Status segment
        st = self.store.status
        if st == "idle":
            status_text = theme.format_dim("idle")
        elif st in ("thinking", "streaming"):
            status_text = theme.format_accent(st)
        elif st.startswith("tool:"):
            status_text = theme.format_accent("working")
        elif st == "waiting_approval":
            status_text = theme.format_accent("approval needed")
        elif st == "paused":
            status_text = theme.format_dim("paused")
        else:
            status_text = theme.format_dim(st)

        # Model
        model_str = theme.format_primary(self.store.model_id)

        # Tokens
        tot_tokens = self.store.input_tokens + self.store.output_tokens
        token_str = (
            theme.format_dim(f"{format_token_count(tot_tokens)} tokens") if tot_tokens > 0 else ""
        )

        # Latency
        lat = self.store.last_latency_ms
        lat_str = theme.format_dim(f"{int(lat)}ms") if lat > 0 else ""

        # Shortcuts based on mode
        if self.store.mode == InputMode.APPROVAL:
            hint = theme.format_dim("y approve · n deny · Esc cancel")
        elif self.store.mode == InputMode.QUESTION:
            hint = theme.format_dim("↑/↓ navigate · Enter select · Esc cancel")
        elif self.store.pending_approvals:
            hint = theme.format_accent("approval pending")
        else:
            hint = theme.format_dim("Tab tools · Esc stop · /help · Ctrl-D exit")

        # Assemble left items with middle dots
        items = [status_text, model_str]
        if token_str:
            items.append(token_str)
        if lat_str:
            items.append(lat_str)

        left = f"  {f' {dot} '.join(items)}"

        # Spacing to right-aligned hint (keep 2-col safety margin from right edge)
        # to prevent terminal emulator automatic wrap onto the next line
        target_w = max(20, width - 2)
        total_needed = visible_width(left) + visible_width(hint) + 2

        if total_needed <= target_w:
            pad = max(1, target_w - visible_width(left) - visible_width(hint))
            line = f"{left}{' ' * pad}{hint}"
        else:
            # Try condensed hint if space is tight at standard 80 columns
            condensed = (
                theme.format_dim("Tab tools · /help")
                if self.store.mode == InputMode.PROMPT
                else theme.format_dim("↑/↓ · Enter")
            )
            if visible_width(left) + visible_width(condensed) + 2 <= target_w:
                pad = max(1, target_w - visible_width(left) - visible_width(condensed))
                line = f"{left}{' ' * pad}{condensed}"
            else:
                line = truncate_visible(left, target_w)

        return [line]
