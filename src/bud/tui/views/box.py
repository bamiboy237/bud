"""Box-drawing utilities for cards, containers, and composers in Bud TUI.

Provides consistent, hairline-bordered container cards inspired by modern
developer tools (OpenCode, OpenAI Codex, Ghostty).
Supports both rounded corners (╭─, ╰─) and crisp square corners (┌─, └─)
with subtle border styling and precise Unicode visible column alignment.
"""

from __future__ import annotations

from typing import Literal

from bud.tui.terminal import truncate_visible, visible_width
from bud.tui.themes import Theme

BoxStyle = Literal["rounded", "square"]


def get_box_glyphs(style: BoxStyle) -> dict[str, str]:
    """Return corner and border glyphs for the requested box style."""
    if style == "square":
        return {
            "tl": "┌",
            "tr": "┐",
            "bl": "└",
            "br": "┘",
            "h": "─",
            "v": "│",
        }
    return {
        "tl": "╭",
        "tr": "╮",
        "bl": "╰",
        "br": "╯",
        "h": "─",
        "v": "│",
    }


def render_box(
    title: str = "",
    body_lines: list[str] | None = None,
    width: int = 80,
    theme: Theme | None = None,
    style: BoxStyle = "rounded",
    footer_text: str = "",
    indent: int = 2,
    inner_padding_x: int = 1,
) -> list[str]:
    """Render a contained card with hairline borders.

    Parameters:
        title: Optional title displayed in the top border (e.g. ' Workspace Activity ').
        body_lines: List of formatted lines inside the card.
        width: Total available terminal columns.
        theme: Theme providing format_border and text colors.
        style: 'rounded' (╭─) or 'square' (┌─).
        footer_text: Optional text/shortcuts embedded in the bottom border.
        indent: Left column indentation margin (defaults to 2 spaces).
        inner_padding_x: Left and right horizontal padding inside the box (defaults to 1 space).

    Returns:
        List of formatted terminal string lines.
    """
    glyphs = get_box_glyphs(style)
    tl, tr, bl, br = glyphs["tl"], glyphs["tr"], glyphs["bl"], glyphs["br"]
    h, v = glyphs["h"], glyphs["v"]

    # Calculate outer box width (ensuring 2-col right margin safety)
    indent_str = " " * indent
    box_w = max(24, width - indent - 2)

    def style_border(char: str) -> str:
        return theme.format_border(char) if theme else char

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

    # 3. Bottom border with optional footer
    if footer_text:
        clean_footer = f" {footer_text.strip()} "
        footer_w = visible_width(clean_footer)
        available_h = box_w - 2 - 1 - footer_w
        if available_h >= 1:
            right_h = h * available_h
            bot_line = (
                f"{indent_str}{style_border(bl + h)}{clean_footer}{style_border(right_h + br)}"
            )
        else:
            truncated_footer = truncate_visible(clean_footer, box_w - 6)
            rem_h = max(1, box_w - 3 - visible_width(truncated_footer))
            bot_line = (
                f"{indent_str}{style_border(bl + h)}{truncated_footer}"
                f"{style_border((h * rem_h) + br)}"
            )
    else:
        bot_line = f"{indent_str}{style_border(bl + (h * (box_w - 2)) + br)}"

    lines.append(bot_line)
    return lines
