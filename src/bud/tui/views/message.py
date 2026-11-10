"""Streaming markdown rendering with token buffering (30-50ms debounce) & syntax highlighting."""

from __future__ import annotations

import time
from io import StringIO

from rich.console import Console
from rich.markdown import Markdown

from bud.tui.state import StateStore
from bud.tui.views.base import View


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


class MessageView(View):
    """Renders a single message with markdown formatting and syntax highlighting."""

    def __init__(self, role: str, text: str = "", store: StateStore | None = None) -> None:
        super().__init__()
        self.role: str = role
        self.store: StateStore | None = store
        self._text: str = text
        self._token_buffer: list[str] = []
        self._last_flush_time: float = 0.0
        self._is_streaming: bool = False
        self._debounce_interval: float = 0.04  # 40ms debounce window (25fps cap)

    @property
    def text(self) -> str:
        if self._token_buffer:
            return self._text + "".join(self._token_buffer)
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        self._token_buffer.clear()
        self.mark_dirty()

    def start_streaming(self) -> None:
        self._is_streaming = True
        self._token_buffer.clear()
        self._last_flush_time = time.time()
        self.mark_dirty()

    def push_token(self, token: str) -> bool:
        """Push a token into the debounce buffer. Returns True if a flush occurred."""
        self._token_buffer.append(token)
        now = time.time()
        if now - self._last_flush_time >= self._debounce_interval:
            self._text += "".join(self._token_buffer)
            self._token_buffer.clear()
            self._last_flush_time = now
            self.mark_dirty()
            return True
        return False

    def finish_streaming(self, final_text: str | None = None) -> None:
        self._is_streaming = False
        if final_text is not None:
            self._text = final_text
        elif self._token_buffer:
            self._text += "".join(self._token_buffer)
        self._token_buffer.clear()
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
            prefix = theme.format_primary("you ›", bold=True) if theme else "\x1b[1myou ›\x1b[0m"
            user_lines = current_text.splitlines() or [""]
            res = [f"  {prefix} {user_lines[0]}"]
            indent = "        "
            for uline in user_lines[1:]:
                res.append(f"{indent}{uline}")
            return res

        # Assistant role: parse markdown using rich
        prefix = theme.format_accent("bud ›", bold=True) if theme else "\x1b[1mbud ›\x1b[0m"
        content_width = max(20, width - 8)

        try:
            buf = StringIO()
            console = Console(
                file=buf,
                force_terminal=True,
                color_system="truecolor",
                width=content_width,
                highlight=True,
            )
            theme_name = theme.name if theme else "default"
            pygments_theme = get_code_theme_for_theme(theme_name)
            md = Markdown(current_text, code_theme=pygments_theme)
            console.print(md)
            rendered_lines = [line.rstrip() for line in buf.getvalue().splitlines()]
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
