"""Theme definitions and palette engine for bud TUI.

Provides cohesive, restrained color palettes, TrueColor ANSI formatting,
and prompt-toolkit styles inspired by minimalist modern tools (Grok Bot, pi-tui, OpenCode).
Follows a strict monochrome-first foundation with zero neon noise.
"""

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.styles import Style


def hex_to_rgb(hex_code: str) -> tuple[int, int, int]:
    """Convert hex color string like '#88C0D0' or '88C0D0' to RGB tuple."""
    code = hex_code.lstrip("#")
    if len(code) == 3:
        code = "".join(c * 2 for c in code)
    if len(code) != 6:
        return (255, 255, 255)
    return (int(code[0:2], 16), int(code[2:4], 16), int(code[4:6], 16))


def fg_rgb(hex_code: str) -> str:
    """Generate 24-bit TrueColor foreground ANSI escape sequence."""
    r, g, b = hex_to_rgb(hex_code)
    return f"\x1b[38;2;{r};{g};{b}m"


def bg_rgb(hex_code: str) -> str:
    """Generate 24-bit TrueColor background ANSI escape sequence."""
    r, g, b = hex_to_rgb(hex_code)
    return f"\x1b[48;2;{r};{g};{b}m"


RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
UNDERLINE = "\x1b[4m"


@dataclass(frozen=True)
class Theme:
    name: str
    display_name: str
    primary: str
    secondary: str
    accent: str
    success: str
    warning: str
    error: str
    info: str
    muted: str
    dim: str
    border: str
    bg: str
    fg: str

    def colorize(self, text: str, hex_code: str, *, bold: bool = False, dim: bool = False) -> str:
        """Apply color and formatting to text with reset."""
        prefix = fg_rgb(hex_code)
        if bold:
            prefix += BOLD
        if dim:
            prefix += DIM
        return f"{prefix}{text}{RESET}"

    def format_primary(self, text: str, *, bold: bool = False) -> str:
        return self.colorize(text, self.primary, bold=bold)

    def format_secondary(self, text: str, *, bold: bool = False) -> str:
        return self.colorize(text, self.secondary, bold=bold)

    def format_accent(self, text: str, *, bold: bool = False) -> str:
        return self.colorize(text, self.accent, bold=bold)

    def format_success(self, text: str, *, bold: bool = False) -> str:
        return self.colorize(text, self.success, bold=bold)

    def format_warning(self, text: str, *, bold: bool = False) -> str:
        return self.colorize(text, self.warning, bold=bold)

    def format_error(self, text: str, *, bold: bool = False) -> str:
        return self.colorize(text, self.error, bold=bold)

    def format_info(self, text: str, *, bold: bool = False) -> str:
        return self.colorize(text, self.info, bold=bold)

    def format_muted(self, text: str) -> str:
        return self.colorize(text, self.muted)

    def format_dim(self, text: str) -> str:
        return self.colorize(text, self.dim, dim=True)

    def format_border(self, text: str) -> str:
        return self.colorize(text, self.border)

    def to_prompt_toolkit_style(self) -> Style:
        """Generate prompt-toolkit Style object mapped to this theme."""
        return Style.from_dict(
            {
                "prompt": f"fg:{self.primary} bold",
                "prompt.symbol": f"fg:{self.accent} bold",
                "input": f"fg:{self.fg}",
                "footer": f"fg:{self.muted}",
                "footer.model": f"fg:{self.primary}",
                "footer.tokens": f"fg:{self.muted}",
                "footer.latency": f"fg:{self.muted}",
                "footer.hint": f"fg:{self.dim}",
                "status.idle": f"fg:{self.muted}",
                "status.active": f"fg:{self.accent}",
                "status.waiting": f"fg:{self.secondary}",
                "choice.selected": f"fg:{self.accent} bold",
                "choice.unselected": f"fg:{self.muted}",
                "card.border": f"fg:{self.border}",
                "card.title": f"fg:{self.primary} bold",
                "key.trap": f"fg:{self.accent} bold",
            }
        )


THEMES: dict[str, Theme] = {
    "default": Theme(
        name="default",
        display_name="Monochrome / Zinc",
        primary="#f4f4f5",
        secondary="#d4d4d8",
        accent="#ffffff",
        success="#a1a1aa",
        warning="#e4e4e7",
        error="#f87171",
        info="#a1a1aa",
        muted="#71717a",
        dim="#52525b",
        border="#3f3f46",
        bg="#09090b",
        fg="#f4f4f5",
    ),
    "nord": Theme(
        name="nord",
        display_name="Nord Polar Night",
        primary="#eceff4",
        secondary="#88c0d0",
        accent="#81a1c1",
        success="#a3be8c",
        warning="#ebcb8b",
        error="#bf616a",
        info="#5e81ac",
        muted="#4c566a",
        dim="#3b4252",
        border="#434c5e",
        bg="#2e3440",
        fg="#eceff4",
    ),
    "catppuccin": Theme(
        name="catppuccin",
        display_name="Catppuccin Mocha",
        primary="#cdd6f4",
        secondary="#b4befe",
        accent="#cba6f7",
        success="#a6e3a1",
        warning="#fab387",
        error="#f38ba8",
        info="#89dceb",
        muted="#6c7086",
        dim="#45475a",
        border="#313244",
        bg="#1e1e2e",
        fg="#cdd6f4",
    ),
    "gruvbox": Theme(
        name="gruvbox",
        display_name="Gruvbox Dark",
        primary="#ebdbb2",
        secondary="#fabd2f",
        accent="#8ec07c",
        success="#b8bb26",
        warning="#fe8019",
        error="#fb4934",
        info="#83a598",
        muted="#928374",
        dim="#504945",
        border="#3c3836",
        bg="#282828",
        fg="#ebdbb2",
    ),
    "tokyo-night": Theme(
        name="tokyo-night",
        display_name="Tokyo Night Storm",
        primary="#c0caf5",
        secondary="#7aa2f7",
        accent="#7dcfff",
        success="#73daca",
        warning="#e0af68",
        error="#f7768e",
        info="#2ac3de",
        muted="#565f89",
        dim="#3b4261",
        border="#292e42",
        bg="#1a1b26",
        fg="#c0caf5",
    ),
}

DEFAULT_THEME_NAME = "default"


def get_theme(name: str) -> Theme:
    """Retrieve theme by name (case-insensitive) or return default theme."""
    key = name.strip().lower()
    return THEMES.get(key, THEMES[DEFAULT_THEME_NAME])


def list_theme_names() -> list[str]:
    """List all available theme keys."""
    return list(THEMES.keys())
