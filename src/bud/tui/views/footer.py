"""Muted one-line status bar for Bud TUI displaying model, tokens, latency, and hotkeys."""

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
        pipe = theme.format_dim("│")

        # Status badge
        st = self.store.status
        if st == "idle":
            status_badge = theme.format_dim("● idle")
        elif st in ("thinking", "streaming") or st.startswith("tool:"):
            status_badge = theme.format_accent(f"⠋ {st}")
        elif st == "waiting_approval":
            status_badge = theme.format_warning("⚠ approval pending", bold=True)
        elif st == "paused":
            status_badge = theme.format_warning("⏸ paused")
        else:
            status_badge = theme.format_dim(f"● {st}")

        # Model
        model_str = theme.format_primary(self.store.model_id, bold=True)

        # Tokens
        tot_tokens = self.store.input_tokens + self.store.output_tokens
        token_str = (
            theme.format_secondary(f"{format_token_count(tot_tokens)} tokens")
            if tot_tokens > 0
            else ""
        )

        # Latency
        lat = self.store.last_latency_ms
        lat_str = theme.format_muted(f"{int(lat)}ms") if lat > 0 else ""

        # Shortcuts based on mode
        if self.store.mode == InputMode.APPROVAL:
            hint = theme.format_warning("[y] approve • [n] deny • [Esc] cancel")
        elif self.store.mode == InputMode.QUESTION:
            hint = theme.format_accent("↑/↓ navigate • Enter select • Esc cancel")
        else:
            hint = theme.format_dim("Tab tools • Esc stop • /help")

        # Combine segments
        left_parts = [status_badge, model_str]
        if token_str:
            left_parts.append(token_str)
        if lat_str:
            left_parts.append(lat_str)

        left = f" {f' {pipe} '.join(left_parts)}"

        # If terminal width is tight, drop hints
        total_w = visible_width(left) + visible_width(hint) + 4
        if total_w <= width:
            pad = max(1, width - visible_width(left) - visible_width(hint) - 1)
            line = f"{left}{' ' * pad}{hint} "
        else:
            # Fallback without hint
            line = truncate_visible(left, width)

        return [line]
