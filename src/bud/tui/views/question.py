"""Interactive choice selector view for Bud TUI.

Provides a clean typographic chevron selector inspired by OpenCode and Claude Code.
Eliminates web radio circles (○, ●) and question mark icons.
"""

from __future__ import annotations

from bud.tui.state import StateStore
from bud.tui.terminal import truncate_visible
from bud.tui.views.base import View


class QuestionView(View):
    """Renders an interactive choice selector menu with chevron pointers."""

    def __init__(self, store: StateStore) -> None:
        super().__init__()
        self.store: StateStore = store
        self.store.subscribe(self.mark_dirty)

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme
        lines: list[str] = [""]

        # Question prompt
        title = self.store.question_title or "Select an option:"
        prompt_line = f"  {theme.format_primary(title, bold=True)}"
        lines.append(truncate_visible(prompt_line, width))

        # Choices
        choices = self.store.question_choices
        selected_idx = self.store.question_selected_index

        for idx, choice in enumerate(choices):
            is_selected = idx == selected_idx
            if is_selected:
                pointer = theme.format_accent("›", bold=True)
                choice_str = theme.format_accent(choice, bold=True)
                line = f"    {pointer} {choice_str}"
            else:
                choice_str = theme.format_muted(choice)
                line = f"      {choice_str}"
            lines.append(truncate_visible(line, width))

        # Navigation hint
        hint = theme.format_dim("  ↑/↓ navigate · Enter select · Esc cancel")
        lines.append("")
        lines.append(truncate_visible(hint, width))
        lines.append("")

        return lines
