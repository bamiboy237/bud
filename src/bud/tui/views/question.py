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

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme
        border_style: BoxStyle = self.store.border_style

        title = self.store.question_title or "Select an option"
        choices = self.store.question_choices
        selected_idx = self.store.question_selected_index

        body_lines: list[str] = []
        for idx, choice in enumerate(choices):
            marker = theme.format_dim(f"{idx + 1}.")
            if idx == selected_idx:
                pointer = theme.format_accent("›", bold=True)
                choice_str = theme.format_accent(choice, bold=True)
            else:
                pointer = theme.format_dim(" ")
                choice_str = theme.format_muted(choice)
            body_lines.append(f"{pointer} {marker} {choice_str}")

        # Key hints live in the footer; the card carries content only.
        return render_box(
            title=title,
            body_lines=body_lines,
            width=width,
            theme=theme,
            style=border_style,
        )
