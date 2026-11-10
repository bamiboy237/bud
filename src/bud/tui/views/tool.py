"""Collapsible tool disclosure widgets for Bud TUI.

Supports single-line collapsed summaries and expanded argument/result disclosure,
toggled via the Tab key.
"""

from __future__ import annotations

import json
import time

from bud.tui.state import StateStore, ToolState
from bud.tui.terminal import truncate_visible, visible_width
from bud.tui.views.base import View

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def get_spinner_frame() -> str:
    """Return a deterministic spinner frame based on wall-clock time."""
    idx = int(time.time() * 10) % len(SPINNER_FRAMES)
    return SPINNER_FRAMES[idx]


def summarize_args(args: dict) -> str:
    """Format arguments into a concise inline summary string."""
    if not args:
        return ""
    items = []
    for k, v in args.items():
        if isinstance(v, str):
            val = f'"{v[:30]}…"' if len(v) > 30 else f'"{v}"'
        else:
            val = str(v)
            if len(val) > 20:
                val = val[:20] + "…"
        items.append(f"{k}={val}")
    return ", ".join(items)


class ToolView(View):
    """Renders a collapsible disclosure widget for a single tool execution."""

    def __init__(self, tool: ToolState, store: StateStore | None = None) -> None:
        super().__init__()
        self.tool: ToolState = tool
        self.store: StateStore | None = store

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme if self.store else None
        t = self.tool

        # Triangle glyph
        arrow = "▾" if t.expanded else "▸"
        arrow_str = theme.format_accent(arrow) if theme else arrow

        # Tool name
        name_str = theme.format_primary(t.tool_name, bold=True) if theme else t.tool_name

        # Badge
        if t.status == "running":
            spin = get_spinner_frame()
            badge_text = f"{spin} running"
            badge = theme.format_warning(f"[{badge_text}]") if theme else f"[{badge_text}]"
        elif t.status == "completed":
            dur = f"{int(t.duration_ms)}ms" if t.duration_ms is not None else ""
            badge_text = f"✓ {dur}".strip()
            badge = theme.format_success(f"[{badge_text}]") if theme else f"[{badge_text}]"
        else:
            badge = theme.format_error("[✗ failed]") if theme else "[✗ failed]"

        # Args preview
        args_summary = summarize_args(t.args)
        if args_summary:
            args_preview = theme.format_muted(f"({args_summary})") if theme else f"({args_summary})"
        else:
            args_preview = ""

        # Compute header line with bounds
        header_base = f" {arrow_str} {name_str} {args_preview}"
        badge_w = visible_width(badge)
        available = max(10, width - badge_w - 2)

        truncated_header = truncate_visible(header_base, available)
        header_pad = max(1, width - visible_width(truncated_header) - badge_w)
        header_line = f"{truncated_header}{' ' * header_pad}{badge}"

        lines = [header_line]

        if t.expanded:
            pipe = theme.format_border("│") if theme else "│"
            dim_pipe = theme.format_dim("│") if theme else "│"

            # Formatted args
            lines.append(f" {pipe} {theme.format_dim('Args:') if theme else 'Args:'}")
            for k, v in t.args.items():
                label = theme.format_secondary(f"  {k}:") if theme else f"  {k}:"
                if isinstance(v, (dict, list)):
                    val_str = json.dumps(v, ensure_ascii=False)
                else:
                    val_str = str(v)
                val_trunc = truncate_visible(val_str, max(10, width - visible_width(label) - 6))
                lines.append(f" {dim_pipe} {label} {val_trunc}")

            # Formatted result
            if t.result is not None:
                lines.append(f" {pipe} {theme.format_dim('Result:') if theme else 'Result:'}")
                res_lines = t.result.splitlines()[:10]  # Show up to 10 lines of result
                for rline in res_lines:
                    rline_trunc = truncate_visible(rline, max(10, width - 6))
                    lines.append(
                        f" {dim_pipe}   {theme.format_muted(rline_trunc) if theme else rline_trunc}"
                    )
                if len(t.result.splitlines()) > 10:
                    more = f"  … ({len(t.result.splitlines()) - 10} more lines)"
                    lines.append(f" {dim_pipe} {theme.format_dim(more) if theme else more}")

        return lines


class ToolsContainerView(View):
    """Renders all currently active or recently finished tool disclosure widgets."""

    def __init__(self, store: StateStore) -> None:
        super().__init__()
        self.store: StateStore = store
        self.store.subscribe(self.mark_dirty)

    def _render(self, width: int) -> list[str]:
        if not self.store.tools:
            return []
        lines: list[str] = []
        for tool_state in self.store.tools.values():
            widget = ToolView(tool_state, self.store)
            lines.extend(widget.render(width))
        return lines
