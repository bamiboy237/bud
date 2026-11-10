"""Interactive choice selector view for Bud TUI.

Provides a clean typographic chevron selector inside a contained hairline card,
inspired by OpenCode and Claude Code.
Eliminates web radio circles (○, ●) and tacky icons in favor of quiet chevrons.
"""

from __future__ import annotations

from bud.tui.state import StateStore
from bud.tui.views.base import View
from bud.tui.views.box import BoxStyle, render_box


class QuestionView(View):
    """Renders an interactive choice selector menu inside a contained hairline card."""

    def __init__(self, store: StateStore) -> None:
        super().__init__()
        self.store: StateStore = store
        self.store.subscribe(self.mark_dirty)

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme
        border_style: BoxStyle = getattr(self.store, "border_style", "rounded")

        title = self.store.question_title or "Select an option"
        choices = self.store.question_choices
        selected_idx = self.store.question_selected_index

        body_lines: list[str] = []
        for idx, choice in enumerate(choices):
            is_selected = idx == selected_idx
            if is_selected:
                pointer = theme.format_accent("›", bold=True)
                choice_str = theme.format_accent(choice, bold=True)
                body_lines.append(f"{pointer} {choice_str}")
            else:
                choice_str = theme.format_muted(choice)
                body_lines.append(f"  {choice_str}")

        hint = "↑/↓ navigate · Enter select · Esc cancel"
        return render_box(
            title=title,
            body_lines=body_lines,
            width=width,
            theme=theme,
            style=border_style,
            footer_text=hint,
        )
