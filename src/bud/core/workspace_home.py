"""Bud's workspace home: `~/.bud/workspace`, or `WORKSPACE_DIR` when set."""

from pathlib import Path

from bud.config import Settings


def workspace_home(settings: Settings) -> Path:
    """The folder Bud reads and writes on the owner's behalf, created on first use."""
    # An empty WORKSPACE_DIR means unset, not the current directory.
    configured = settings.workspace_dir
    if configured is None or str(configured) in ("", "."):
        configured = Path.home() / ".bud" / "workspace"
    target = configured.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The parent is resolved because /tmp and /var are symlinks on macOS. The home itself is
    # not, so a symlink swapped in at that level is still refused by the workspace tools.
    home = target.parent.resolve() / target.name
    home.mkdir(exist_ok=True, mode=0o700)
    return home


def display_path(path: Path) -> str:
    """The path as the owner would type it, with ~ for their home folder."""
    try:
        return f"~/{path.relative_to(Path.home().resolve())}"
    except ValueError:
        return str(path)
