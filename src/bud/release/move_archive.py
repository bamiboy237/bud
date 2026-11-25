"""Bounds and path rules for the private runtime transfer archive."""

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from dotenv import dotenv_values

from bud.release import ReleaseError

MAX_MOVE_BYTES = 256 * 1024 * 1024
MAX_MOVE_FILES = 20_000


class RuntimeMoveError(ReleaseError):
    """The runtime move cannot proceed."""


def regular_files(directory: Path) -> list[Path]:
    if directory.is_symlink():
        raise RuntimeMoveError("Bud cannot move a symbolic link.")
    if not directory.is_dir():
        return []
    files = []
    for path in sorted(directory.iterdir()):
        if path.is_symlink():
            raise RuntimeMoveError("Bud cannot move a symbolic link.")
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(regular_files(path))
    return files


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_paths(home: Path, workspace: Path | None = None) -> tuple[Path, Path]:
    config = next((home / name for name in ("config.env", ".env") if (home / name).is_file()), None)
    values = dotenv_values(config, interpolate=False) if config else {}
    data = home / Path(values.get("DATA_DIR") or "data").expanduser()
    if (home / "composio" / "connections.db").is_file():
        data = home
    if not data.resolve().is_relative_to(home.resolve()):
        raise RuntimeMoveError("Select the Bud data folder directly for this installation.")
    if workspace is not None:
        return data, workspace
    candidate = home / Path(values.get("WORKSPACE_DIR") or "workspace").expanduser()
    if not candidate.resolve().is_relative_to(home.resolve()):
        raise RuntimeMoveError("Select the workspace folder for this installation.")
    return data, candidate


def unpack_archive(archive: Path, destination: Path, permits: Callable[[Path], bool]) -> None:
    try:
        with ZipFile(archive) as package:
            entries = package.infolist()
            if (
                len(entries) > MAX_MOVE_FILES
                or sum(entry.file_size for entry in entries) > MAX_MOVE_BYTES
            ):
                raise RuntimeMoveError("This Bud profile is too large to move.")
            for entry in entries:
                name = Path(entry.filename)
                if (
                    entry.is_dir()
                    or name.is_absolute()
                    or ".." in name.parts
                    or "\\" in entry.filename
                    or name.as_posix() != entry.filename
                    or name == Path(".")
                    or not permits(name)
                ):
                    raise RuntimeMoveError("The Bud archive contains an invalid file.")
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with package.open(entry) as reader, target.open("xb") as output:
                    target.chmod(0o600)
                    shutil.copyfileobj(reader, output)
                    output.flush()
                    os.fsync(output.fileno())
    except (OSError, BadZipFile, RuntimeError, ValueError):
        raise RuntimeMoveError("Couldn’t read the Bud runtime archive.") from None
