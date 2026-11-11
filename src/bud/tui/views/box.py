"""Box-drawing utilities for cards, containers, and composers in Bud TUI.

Provides consistent, hairline-bordered container cards inspired by modern
developer tools (OpenCode, OpenAI Codex, Ghostty).
Supports rounded corners (╭─, ╰─), crisp square corners (┌─, └─), and a
minimal left-rail style, with subtle border styling and precise Unicode
visible column alignment.
"""

from __future__ import annotations

from typing import Literal

from bud.tui.terminal import truncate_visible, visible_width
from bud.tui.themes import Theme

BoxStyle = Literal["rounded", "square", "left"]


ROUNDED_GLYPHS: dict[str, str] = {
    "tl": "╭",
    "tr": "╮",
    "bl": "╰",
    "br": "╯",
    "h": "─",
    "v": "│",
}

SQUARE_GLYPHS: dict[str, str] = {
    "tl": "┌",
    "tr": "┐",
    "bl": "└",
    "br": "┘",
    "h": "─",
    "v": "│",
}


def get_box_glyphs(style: BoxStyle) -> dict[str, str]:
    """Return corner and border glyphs for the requested box style."""
    return SQUARE_GLYPHS if style == "square" else ROUNDED_GLYPHS


def render_box(
    title: str = "",
    body_lines: list[str] | None = None,
    width: int = 80,
    theme: Theme | None = None,
    style: BoxStyle = "rounded",
    indent: int = 2,
    inner_padding_x: int = 1,
) -> list[str]:
    """Render a contained card with hairline borders.

    Parameters:
        title: Optional title displayed in the top border (e.g. ' Workspace Activity ').
        body_lines: List of formatted lines inside the card.
        width: Total available terminal columns.
        theme: Theme providing format_border and text colors.
        style: 'rounded' (╭─), 'square' (┌─), or 'left' (│ rail, no frame).
        indent: Left column indentation margin (defaults to 2 spaces).
        inner_padding_x: Left and right horizontal padding inside the box (defaults to 1 space).

    Returns:
        List of formatted terminal string lines.
    """
    indent_str = " " * indent

    def style_border(char: str) -> str:
        return theme.format_border(char) if theme else char

    if style == "left":
        # Rail style: no frame, one rail per row, each row clamped to the width.
        v_bar = style_border("│")
        budget = max(8, width - 2)
        res: list[str] = []
        if title:
            res.append(f"{indent_str}{v_bar} {title}")
        if body_lines:
            res.extend(f"{indent_str}{v_bar} {line}" for line in body_lines)
        return [
            truncate_visible(line, budget) if visible_width(line) > budget else line for line in res
        ]

    glyphs = get_box_glyphs(style)
    tl, tr, bl, br = glyphs["tl"], glyphs["tr"], glyphs["bl"], glyphs["br"]
    h, v = glyphs["h"], glyphs["v"]

    # Calculate outer box width (ensuring 2-col right margin safety)
    box_w = max(24, width - indent - 2)
    lines: list[str] = []

    # 1. Top border with optional title
    # Layout: indent + tl + h + [ title ] + h... + tr
    if title:
        # Pad title with single spaces for breathing room
        clean_title = f" {title.strip()} "
        title_w = visible_width(clean_title)
        available_h = box_w - 2 - 1 - title_w  # minus tl, tr, initial h, title
        if available_h >= 1:
            right_h = h * available_h
            styled_left = style_border(tl + h)
            styled_right = style_border(right_h + tr)
            top_line = f"{indent_str}{styled_left}{clean_title}{styled_right}"
        else:
            # Title too long for box width: truncate title
            truncated_title = truncate_visible(clean_title, box_w - 6)
            rem_h = max(1, box_w - 3 - visible_width(truncated_title))
            styled_left = style_border(tl + h)
            styled_right = style_border((h * rem_h) + tr)
            top_line = f"{indent_str}{styled_left}{truncated_title}{styled_right}"
    else:
        top_line = f"{indent_str}{style_border(tl + (h * (box_w - 2)) + tr)}"

    lines.append(top_line)

    # 2. Content lines
    # Each row: indent + v + (inner_pad) + content + pad_to_right + v
    inner_w = box_w - 2 - (inner_padding_x * 2)
    inner_pad = " " * inner_padding_x

    if body_lines:
        for raw_line in body_lines:
            vw = visible_width(raw_line)
            if vw > inner_w:
                truncated_content = truncate_visible(raw_line, inner_w)
                pad_w = max(0, inner_w - visible_width(truncated_content))
                row = (
                    f"{indent_str}{style_border(v)}{inner_pad}{truncated_content}"
                    f"{' ' * pad_w}{inner_pad}{style_border(v)}"
                )
            else:
                pad_w = inner_w - vw
                row = (
                    f"{indent_str}{style_border(v)}{inner_pad}{raw_line}"
                    f"{' ' * pad_w}{inner_pad}{style_border(v)}"
                )
            lines.append(row)

    # 3. Bottom border
    bot_line = f"{indent_str}{style_border(bl + (h * (box_w - 2)) + br)}"

    lines.append(bot_line)
    return lines


def _wrap_visible(text: str, width: int) -> list[str]:
    """Word-wrap plain text to a visible width, preserving ANSI escapes as zero-width."""
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if visible_width(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        if visible_width(word) > width:
            # A single unbreakable token (URL, hash): clamp it.
            lines.append(truncate_visible(word, width))
            current = ""
        else:
            current = word
    if current:
        lines.append(current)
    return lines


def render_user_turn(
    text: str, width: int, theme: Theme | None = None, indent: int = 2
) -> list[str]:
    """Render a user turn as a left rail.

    The rail wraps instead of truncating, so the user's own message is never
    shortened in scrollback. This is the single renderer for user turns:
    live submit, echo and history replay all route through it.
    """
    rail = theme.format_border("│") if theme else "│"
    indent_str = " " * indent
    content_w = max(8, width - indent - 2)
    lines: list[str] = []
    for logical in text.splitlines() or [""]:
        for piece in _wrap_visible(logical, content_w):
            lines.append(f"{indent_str}{rail} {piece}")
    return lines
