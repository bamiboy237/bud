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
                "prompt.symbol": f"fg:{self.accent} bold",
                "input.prompt": f"fg:{self.accent} bold",
                "card.border": f"fg:{self.border}",
                "composer.badge": f"fg:{self.accent} bold",
                "composer.meta": f"fg:{self.muted}",
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
    "groknight": Theme(
        name="groknight",
        display_name="GrokNight",
        primary="#e1e1e1",
        secondary="#7aa2f7",
        accent="#c084fc",
        success="#9ece6a",
        warning="#e0af68",
        error="#f7768e",
        info="#7dcfff",
        muted="#6b7280",
        dim="#4b5563",
        border="#2d3139",
        bg="#141416",
        fg="#e1e1e1",
    ),
    "aura": Theme(
        name="aura",
        display_name="OpenCode Aura",
        primary="#a277ff",
        secondary="#82e2ff",
        accent="#a277ff",
        success="#61ffca",
        warning="#ffca85",
        error="#ff6767",
        info="#82e2ff",
        muted="#6d6a7e",
        dim="#4d4a5e",
        border="#252433",
        bg="#15141b",
        fg="#edecee",
    ),
    "opencode": Theme(
        name="opencode",
        display_name="OpenCode Dark",
        primary="#fab283",
        secondary="#edb2f1",
        accent="#fab283",
        success="#6bd586",
        warning="#fcd53a",
        error="#fc533a",
        info="#a2bcff",
        muted="#8f8f8f",
        dim="#5c5c5c",
        border="#3a3a3a",
        bg="#1f1f1f",
        fg="#f1ece8",
    ),
    "vesper": Theme(
        name="vesper",
        display_name="OpenCode Vesper",
        primary="#ffc799",
        secondary="#99ffe4",
        accent="#ff8080",
        success="#99ffe4",
        warning="#ffc799",
        error="#ff8080",
        info="#a0b0d0",
        muted="#707070",
        dim="#404040",
        border="#242424",
        bg="#101010",
        fg="#ffffff",
    ),
    "kanagawa": Theme(
        name="kanagawa",
        display_name="OpenCode Kanagawa",
        primary="#7e9cd8",
        secondary="#98bb6c",
        accent="#d27e99",
        success="#98bb6c",
        warning="#d7a657",
        error="#c34043",
        info="#7fb4ca",
        muted="#727169",
        dim="#49473e",
        border="#2d2f3f",
        bg="#1f1f28",
        fg="#dcd7ba",
    ),
    "rosepine": Theme(
        name="rosepine",
        display_name="OpenCode Rosé Pine",
        primary="#ebbcba",
        secondary="#9ccfd8",
        accent="#c4a7e7",
        success="#31748f",
        warning="#f6c177",
        error="#eb6f92",
        info="#9ccfd8",
        muted="#6e6a86",
        dim="#524f67",
        border="#26233a",
        bg="#191724",
        fg="#e0def4",
    ),
    "carbonfox": Theme(
        name="carbonfox",
        display_name="OpenCode Carbonfox",
        primary="#33b1ff",
        secondary="#78a9ff",
        accent="#ff8389",
        success="#42be65",
        warning="#f1c21b",
        error="#ee5396",
        info="#33b1ff",
        muted="#6e6e6e",
        dim="#393939",
        border="#282828",
        bg="#161616",
        fg="#f2f4f8",
    ),
    "everforest": Theme(
        name="everforest",
        display_name="OpenCode Everforest",
        primary="#a7c080",
        secondary="#7fbbb3",
        accent="#d699b6",
        success="#a7c080",
        warning="#dbbc7f",
        error="#e67e80",
        info="#83c092",
        muted="#7a8478",
        dim="#4f585e",
        border="#3a444a",
        bg="#2d353b",
        fg="#d3c6aa",
    ),
}

DEFAULT_THEME_NAME = "aura"


def get_theme(name: str) -> Theme:
    """Retrieve theme by name (case-insensitive) or return default theme."""
    key = name.strip().lower()
    if key in ("aura", "opencode-aura", "opencode_aura"):
        return THEMES["aura"]
    if key in ("opencode", "oc", "oc2", "oc-2", "opencode-dark"):
        return THEMES["opencode"]
    if key in ("vesper", "opencode-vesper"):
        return THEMES["vesper"]
    if key in ("kanagawa", "opencode-kanagawa"):
        return THEMES["kanagawa"]
    if key in ("rosepine", "rose-pine", "rose_pine"):
        return THEMES["rosepine"]
    if key in ("carbonfox", "carbon"):
        return THEMES["carbonfox"]
    if key in ("everforest", "forest"):
        return THEMES["everforest"]
    if key in ("grok", "grok-night", "groknight"):
        return THEMES["groknight"]
    if key in ("zinc", "monochrome"):
        return THEMES["default"]
    return THEMES.get(key, THEMES[DEFAULT_THEME_NAME])


def list_theme_names() -> list[str]:
    """List all available theme keys."""
    return list(THEMES.keys())
