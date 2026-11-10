"""Interactive choice selector view for Bud TUI.

Allows keyboard navigation via arrow keys (or j/k), Enter to confirm, and Esc to cancel.
"""

from __future__ import annotations

from bud.tui.state import StateStore
from bud.tui.terminal import truncate_visible
from bud.tui.views.base import View


class QuestionView(View):
    """Renders an interactive choice selector menu."""

    def __init__(self, store: StateStore) -> None:
        super().__init__()
        self.store: StateStore = store
        self.store.subscribe(self.mark_dirty)

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme
        lines: list[str] = []

        # Question prompt
        title = self.store.question_title or "Select an option:"
        prompt_line = (
            f" {theme.format_accent('?', bold=True)} {theme.format_primary(title, bold=True)}"
        )
        lines.append(truncate_visible(prompt_line, width))

        # Choices
        choices = self.store.question_choices
        selected_idx = self.store.question_selected_index

        for idx, choice in enumerate(choices):
            is_selected = idx == selected_idx
            if is_selected:
                bullet = theme.format_accent("●", bold=True)
                choice_str = theme.format_accent(choice, bold=True)
                line = f"   {bullet} {choice_str}"
            else:
                bullet = theme.format_muted("○")
                choice_str = theme.format_muted(choice)
                line = f"   {bullet} {choice_str}"
            lines.append(truncate_visible(line, width))

        # Navigation hint
        hint = theme.format_dim(" [↑/↓ navigate • Enter select • Esc cancel]")
        lines.append(truncate_visible(hint, width))

        return lines
