"""Bud's workspace home, `~/.bud/workspace`, and the one-time move of older workspaces into it.

Older versions kept a workspace inside the data folder, one per account binding. The move
copies those files, checks every copy against its original, and only then places it. It
never deletes or overwrites anything: originals stay where they were, and a name already
taken in the home gets a dated suffix. A record of finished folders keeps a later start from
copying them again, and an interrupted move resumes without duplicating files.
"""

import json
import logging
import os
import shutil
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from bud.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_HOME = Path.home() / ".bud" / "workspace"
RECORD = "workspace-migration.json"


class MigrationError(Exception):
    """A copy did not match its original; nothing from that folder was placed."""


def workspace_home(settings: Settings) -> Path:
    """The folder Bud reads and writes on the owner's behalf, created on first use."""
    target = settings.workspace_dir or DEFAULT_HOME
    target = target.expanduser()
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


def legacy_workspaces(data_dir: Path) -> list[Path]:
    """Workspaces older versions kept in the live data folder, oldest layout first."""
    sessions = sorted((data_dir / "composio" / "sessions").glob("*/workspace"))
    found = [data_dir / "workspace", *sessions]
    return [path for path in found if path.is_dir() and not path.is_symlink()]


def migrate(sources: list[Path], home: Path, record: Path) -> list[Path]:
    """Copies each unfinished source into `home`. Returns the sources it finished now.

    Raises MigrationError when a copy does not match; sources finished before it stay
    recorded, and the failed one is tried again on the next start.
    """
    done = set(json.loads(record.read_text())["done"]) if record.exists() else set()
    finished = []
    for source in sources:
        key = str(source.resolve())
        if key == str(home.resolve()) or key in done:
            continue
        _move_in(source, home)
        done.add(key)
        finished.append(source)
        _write_record(record, done)
    return finished


def forget(source: Path, record: Path) -> None:
    """Marks `source` unfinished, so new files written there are copied on a later start."""
    if record.exists():
        done = set(json.loads(record.read_text())["done"]) - {str(source.resolve())}
        _write_record(record, done)


def _move_in(source: Path, home: Path) -> None:
    staging = home.parent / ".migrating"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700)
    try:
        files = [
            path
            for path in sorted(source.rglob("*"))
            if path.is_file() and not path.is_symlink() and not _under_symlink(path, source)
        ]
        for original in files:
            copy = staging / original.relative_to(source)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, copy)
            if _digest(copy) != _digest(original):
                raise MigrationError(f"{original} did not copy correctly")
        for original in files:
            relative = original.relative_to(source)
            _place(staging / relative, home / relative, original)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _place(copy: Path, wanted: Path, original: Path) -> None:
    """Moves a verified copy to `wanted`, or beside it when that name holds other content."""
    wanted.parent.mkdir(parents=True, exist_ok=True)
    digest = _digest(copy)
    day = datetime.fromtimestamp(original.stat().st_mtime, UTC).date()
    names = [
        wanted.name,
        *(
            f"{wanted.stem} (from {day}{f', {n}' if n > 1 else ''}){wanted.suffix}"
            for n in range(1, 1000)
        ),
    ]
    for name in names:
        target = wanted.with_name(name)
        if not target.exists():
            os.rename(copy, target)
            return
        if target.is_file() and _digest(target) == digest:
            return  # placed by an earlier, interrupted run
    raise MigrationError(f"no free name for {wanted}")


def _write_record(record: Path, done: set[str]) -> None:
    partial = record.with_suffix(".tmp")
    partial.write_text(json.dumps({"done": sorted(done)}, indent=1))
    os.replace(partial, record)


def _under_symlink(path: Path, root: Path) -> bool:
    return any((root / parent).is_symlink() for parent in path.relative_to(root).parents)


def _digest(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()
