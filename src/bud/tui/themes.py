"""Theme definitions and palette engine for bud TUI.

Provides cohesive color palettes, TrueColor ANSI formatting, and prompt-toolkit styles
inspired by modern minimalist CLI tools (Claude Code, pi-tui, OpenCode).
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
                "footer.model": f"fg:{self.primary} bold",
                "footer.tokens": f"fg:{self.secondary}",
                "footer.latency": f"fg:{self.muted}",
                "footer.hint": f"fg:{self.dim}",
                "status.idle": f"fg:{self.muted}",
                "status.active": f"fg:{self.accent} bold",
                "status.waiting": f"fg:{self.warning} bold",
                "choice.selected": f"fg:{self.accent} bold underline",
                "choice.unselected": f"fg:{self.muted}",
                "card.border": f"fg:{self.border}",
                "card.title": f"fg:{self.warning} bold",
                "key.trap": f"fg:{self.accent} bold",
            }
        )


THEMES: dict[str, Theme] = {
    "default": Theme(
        name="default",
        display_name="Monochrome / Muted",
        primary="#e0e0e0",
        secondary="#b0b0b0",
        accent="#ffffff",
        success="#81c784",
        warning="#ffb74d",
        error="#e57373",
        info="#90caf9",
        muted="#757575",
        dim="#424242",
        border="#616161",
        bg="#141414",
        fg="#eeeeee",
    ),
    "nord": Theme(
        name="nord",
        display_name="Nord Arctic",
        primary="#88C0D0",
        secondary="#81A1C1",
        accent="#8FBCBB",
        success="#A3BE8C",
        warning="#EBCB8B",
        error="#BF616A",
        info="#5E81AC",
        muted="#4C566A",
        dim="#3B4252",
        border="#4C566A",
        bg="#2E3440",
        fg="#ECEFF4",
    ),
    "catppuccin": Theme(
        name="catppuccin",
        display_name="Catppuccin Mocha",
        primary="#cba6f7",
        secondary="#89b4fa",
        accent="#b4befe",
        success="#a6e3a1",
        warning="#fab387",
        error="#f38ba8",
        info="#74c7ec",
        muted="#6c7086",
        dim="#45475a",
        border="#585b70",
        bg="#1e1e2e",
        fg="#cdd6f4",
    ),
    "gruvbox": Theme(
        name="gruvbox",
        display_name="Gruvbox Dark",
        primary="#fabd2f",
        secondary="#83a598",
        accent="#8ec07c",
        success="#b8bb26",
        warning="#fe8019",
        error="#fb4934",
        info="#d3869b",
        muted="#928374",
        dim="#504945",
        border="#665c54",
        bg="#282828",
        fg="#ebdbb2",
    ),
    "tokyo-night": Theme(
        name="tokyo-night",
        display_name="Tokyo Night",
        primary="#7aa2f7",
        secondary="#bb9af7",
        accent="#7dcfff",
        success="#73daca",
        warning="#ff9e64",
        error="#f7768e",
        info="#2ac3de",
        muted="#565f89",
        dim="#3b4261",
        border="#414868",
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
