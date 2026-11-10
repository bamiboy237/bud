"""Low-level terminal operations, ANSI controls, DECSET 2026, and differential rendering.

Synchronized Output (DECSET 2026: `\\x1b[?2026h` before frame flushes, `\\x1b[?2026l` after)
ensures flicker-free and tear-free terminal updates.
The differential renderer commits finalized output directly to native terminal scrollback
without using DECSET 1049 alternate screen buffer, preserving native scroll history.
"""

from __future__ import annotations

import re
import shutil
import sys
from typing import Any, TextIO

import wcwidth
from prompt_toolkit.cursor_shapes import CursorShape
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import Output
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.styles import Attrs

# DECSET 2026 Synchronized Output sequences
SYNC_START = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"

# Cursor and screen controls
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
CLEAR_LINE = "\x1b[2K"
CLEAR_TO_END = "\x1b[0K"
CLEAR_BELOW = "\x1b[J"
CLEAR_SCREEN = "\x1b[2J\x1b[H"

ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    if "\x1b" not in text:
        return text
    return ANSI_ESCAPE_RE.sub("", text)


def visible_width(text: str) -> int:
    """Calculate the visible terminal column width of text, accounting for wide characters."""
    if not text:
        return 0
    clean = text if "\x1b" not in text else ANSI_ESCAPE_RE.sub("", text)
    if clean.isascii():
        return len(clean)
    w = wcwidth.wcswidth(clean)
    return w if w >= 0 else len(clean)


def truncate_visible(text: str, max_width: int, tail: str = "…") -> str:
    """Truncate text to max_width visible terminal columns, preserving ANSI if possible."""
    if max_width <= 0:
        return ""
    if visible_width(text) <= max_width:
        return text

    tail_w = visible_width(tail)
    target = max(1, max_width - tail_w)

    # Accumulate visible chars
    res: list[str] = []
    current_w = 0
    in_escape = False

    for char in text:
        if char == "\x1b":
            in_escape = True
            res.append(char)
            continue
        if in_escape:
            res.append(char)
            if char in "mABCDHJKsuhlr":
                in_escape = False
            continue

        cw = wcwidth.wcwidth(char)
        cw = cw if cw >= 0 else 1
        if current_w + cw > target:
            break
        res.append(char)
        current_w += cw

    res.append(tail)
    res.append("\x1b[0m")
    return "".join(res)


def get_terminal_size() -> tuple[int, int]:
    """Return current terminal dimensions as (columns, lines)."""
    size = shutil.get_terminal_size((80, 24))
    return size.columns, size.lines


def cursor_up(n: int) -> str:
    """Return ANSI cursor up escape sequence if n > 0."""
    return f"\x1b[{n}A" if n > 0 else ""


def cursor_down(n: int) -> str:
    """Return ANSI cursor down escape sequence if n > 0."""
    return f"\x1b[{n}B" if n > 0 else ""


def cursor_col(n: int = 1) -> str:
    """Move cursor to 1-based column n."""
    return f"\x1b[{n}G"


class DifferentialRenderer:
    """Renders dynamic view lines at the bottom of the screen with synchronized output.

    Commits finalized lines into native terminal scrollback so the user retains
    full scrollback history without alternate screen buffer trapping.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream: TextIO = stream if stream is not None else sys.stdout
        self.prev_lines: list[str] = []
        self._synced: bool = True

    def render_frame(self, lines: list[str]) -> None:
        """Render a frame differentially over previous lines using DECSET 2026."""
        if not lines and not self.prev_lines:
            return

        cols, _ = get_terminal_size()
        # Truncate lines to terminal width to prevent unexpected wrapping
        bounded_lines = [
            truncate_visible(line, cols) if visible_width(line) > cols else line for line in lines
        ]

        buf: list[str] = []
        if self._synced:
            buf.append(SYNC_START)

        # Move cursor to top of previous frame
        prev_h = len(self.prev_lines)
        if prev_h > 0:
            buf.append(cursor_up(prev_h))
            buf.append(cursor_col(1))

        # Overwrite lines
        new_h = len(bounded_lines)
        for line in bounded_lines:
            buf.append(CLEAR_LINE)
            buf.append(cursor_col(1))
            buf.append(line)
            buf.append("\n")

        # Clear remaining extra lines from previous frame
        if prev_h > new_h:
            extra = prev_h - new_h
            for _ in range(extra):
                buf.append(CLEAR_LINE)
                buf.append("\n")
            buf.append(cursor_up(extra))

        if self._synced:
            buf.append(SYNC_END)

        self.stream.write("".join(buf))
        self.stream.flush()
        self.prev_lines = bounded_lines

    def commit_lines(self, lines: list[str]) -> None:
        """Clear dynamic active area and permanently commit lines to scrollback."""
        buf: list[str] = []
        if self._synced:
            buf.append(SYNC_START)

        # Clear dynamic frame
        prev_h = len(self.prev_lines)
        if prev_h > 0:
            buf.append(cursor_up(prev_h))
            buf.append(cursor_col(1))
            buf.append(CLEAR_BELOW)

        # Output committed lines
        for line in lines:
            buf.append(line)
            buf.append("\n")

        if self._synced:
            buf.append(SYNC_END)

        self.stream.write("".join(buf))
        self.stream.flush()
        self.prev_lines = []

    def clear(self) -> None:
        """Clear the active dynamic area."""
        prev_h = len(self.prev_lines)
        if prev_h > 0:
            buf: list[str] = []
            if self._synced:
                buf.append(SYNC_START)
            buf.append(cursor_up(prev_h))
            buf.append(cursor_col(1))
            buf.append(CLEAR_BELOW)
            if self._synced:
                buf.append(SYNC_END)
            self.stream.write("".join(buf))
            self.stream.flush()
            self.prev_lines = []


class SynchronizedOutput(Output):
    """Wraps prompt-toolkit Output to enclose all flushes in DECSET 2026 synchronized output."""

    def __init__(self, output: Output) -> None:
        self._output: Output = output

    def __getattr__(self, name: str) -> Any:
        return getattr(self._output, name)

    def fileno(self) -> int:
        return self._output.fileno()

    def encoding(self) -> str:
        return self._output.encoding()

    def write(self, data: str) -> None:
        self._output.write(data)

    def write_raw(self, data: str) -> None:
        self._output.write_raw(data)

    def set_title(self, title: str) -> None:
        self._output.set_title(title)

    def clear_title(self) -> None:
        self._output.clear_title()

    def flush(self) -> None:
        buf = getattr(self._output, "_buffer", None)
        if isinstance(buf, list):
            if not buf:
                return
            buf.insert(0, SYNC_START)
            buf.append(SYNC_END)
            self._output.flush()
            return
        try:
            self._output.write_raw(SYNC_START)
            self._output.flush()
            self._output.write_raw(SYNC_END)
            self._output.flush()
        except (OSError, ValueError):
            self._output.flush()

    def erase_screen(self) -> None:
        self._output.erase_screen()

    def enter_alternate_screen(self) -> None:
        # Explicitly NO-OP: do NOT enter alternate screen buffer!
        pass

    def quit_alternate_screen(self) -> None:
        # Explicitly NO-OP: do NOT quit alternate screen buffer!
        pass

    def enable_mouse_support(self) -> None:
        self._output.enable_mouse_support()

    def disable_mouse_support(self) -> None:
        self._output.disable_mouse_support()

    def erase_end_of_line(self) -> None:
        self._output.erase_end_of_line()

    def erase_down(self) -> None:
        self._output.erase_down()

    def reset_attributes(self) -> None:
        self._output.reset_attributes()

    def set_attributes(self, attrs: Attrs, color_depth: ColorDepth) -> None:
        self._output.set_attributes(attrs, color_depth)

    def set_cursor_shape(self, cursor_shape: CursorShape) -> None:
        self._output.set_cursor_shape(cursor_shape)

    def reset_cursor_shape(self) -> None:
        self._output.reset_cursor_shape()

    def disable_autowrap(self) -> None:
        self._output.disable_autowrap()

    def enable_autowrap(self) -> None:
        self._output.enable_autowrap()

    def enable_bracketed_paste(self) -> None:
        self._output.enable_bracketed_paste()

    def disable_bracketed_paste(self) -> None:
        self._output.disable_bracketed_paste()

    def reset_cursor_key_mode(self) -> None:
        self._output.reset_cursor_key_mode()

    def cursor_goto(self, row: int = 0, column: int = 0) -> None:
        self._output.cursor_goto(row, column)

    def cursor_up(self, amount: int) -> None:
        self._output.cursor_up(amount)

    def cursor_down(self, amount: int) -> None:
        self._output.cursor_down(amount)

    def cursor_forward(self, amount: int) -> None:
        self._output.cursor_forward(amount)

    def cursor_backward(self, amount: int) -> None:
        self._output.cursor_backward(amount)

    def hide_cursor(self) -> None:
        self._output.hide_cursor()

    def show_cursor(self) -> None:
        self._output.show_cursor()

    def get_size(self) -> Size:
        return self._output.get_size()

    def bell(self) -> None:
        self._output.bell()

    def get_default_color_depth(self) -> ColorDepth:
        return self._output.get_default_color_depth()

    def scroll_buffer_to_prompt(self) -> None:
        self._output.scroll_buffer_to_prompt()

    def get_rows_below_cursor_position(self) -> int:
        return self._output.get_rows_below_cursor_position()

    def ask_for_cpr(self) -> None:
        self._output.ask_for_cpr()

    @property
    def responds_to_cpr(self) -> bool:
        return self._output.responds_to_cpr
