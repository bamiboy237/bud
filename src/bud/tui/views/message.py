"""Streaming markdown rendering with syntax highlighting."""

from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.markdown import Markdown

from bud.tui.state import StateStore
from bud.tui.views.base import View
from bud.tui.views.box import render_user_turn


def get_code_theme_for_theme(theme_name: str) -> str:
    """Map UI theme to a subdued, tasteful pygments syntax theme."""
    mapping = {
        "nord": "nord",
        "gruvbox": "gruvbox-dark",
        "catppuccin": "one-dark",
        "tokyo-night": "material",
        "groknight": "one-dark",
        "aura": "one-dark",
        "opencode": "one-dark",
        "vesper": "monokai",
        "kanagawa": "material",
        "rosepine": "one-dark",
        "carbonfox": "one-dark",
        "everforest": "gruvbox-dark",
        "default": "one-dark",
    }
    return mapping.get(theme_name, "one-dark")


_SHARED_CONSOLE: Console | None = None


def _render_markdown(text: str, width: int, theme_name: str) -> list[str]:
    """Parse and render markdown lines reusing a shared Rich Console."""
    global _SHARED_CONSOLE
    buf = StringIO()
    if _SHARED_CONSOLE is None:
        _SHARED_CONSOLE = Console(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            width=width,
            highlight=True,
        )
    else:
        _SHARED_CONSOLE.file = buf
        _SHARED_CONSOLE.width = width

    try:
        pygments_theme = get_code_theme_for_theme(theme_name)
        md = Markdown(text, code_theme=pygments_theme)
        _SHARED_CONSOLE.print(md)
        return [line.rstrip() for line in buf.getvalue().splitlines()]
    finally:
        _SHARED_CONSOLE.file = StringIO()


class MessageView(View):
    """Renders a single message with markdown formatting and syntax highlighting."""

    def __init__(self, role: str, text: str = "", store: StateStore | None = None) -> None:
        super().__init__()
        self.role: str = role
        self.store: StateStore | None = store
        self._text: str = text
        self._is_streaming: bool = False

    @property
    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        self.mark_dirty()

    def start_streaming(self) -> None:
        self._is_streaming = True
        self.mark_dirty()

    def finish_streaming(self, final_text: str | None = None) -> None:
        self._is_streaming = False
        if final_text is not None:
            self._text = final_text
        self.mark_dirty()

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme if self.store else None
        current_text = self.text

        if not current_text and self._is_streaming:
            prefix = theme.format_accent("bud ›", bold=True) if theme else "\x1b[1mbud ›\x1b[0m"
            spinner = theme.format_dim("thinking…") if theme else "thinking…"
            return [f"{prefix} {spinner}"]

        # Format user vs assistant
        if self.role == "you":
            # Same left-rail renderer as live submissions, so the transcript
            # replays in the one user-turn language.
            return render_user_turn(current_text, width, theme if theme else None)

        # Assistant role: parse markdown using rich
        prefix = theme.format_accent("bud ›", bold=True) if theme else "\x1b[1mbud ›\x1b[0m"
        content_width = max(20, width - 8)
        theme_name = theme.name if theme else "default"

        try:
            rendered_lines = _render_markdown(current_text, content_width, theme_name)
        except Exception:
            # Fallback to plain wrapped text if markdown parsing fails
            rendered_lines = current_text.splitlines()

        if not rendered_lines:
            rendered_lines = [""]

        # Attach prefix to the first line, indented by 2 spaces
        first_line = rendered_lines[0]
        formatted_first = f"  {prefix} {first_line}" if first_line.strip() else f"  {prefix}"

        res_lines: list[str] = [formatted_first]
        for line in rendered_lines[1:]:
            res_lines.append(f"  {line}" if line.strip() else "")

        # If streaming, append a subtle pulsing indicator to the last line
        if self._is_streaming:
            cursor = theme.format_accent(" ▋") if theme else " ▋"
            res_lines[-1] = res_lines[-1] + cursor

        return res_lines
