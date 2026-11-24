"""Consistent pre-update backups, SHA-256 verification, restores, and repo state migration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from bud.release import BackupError

_SQLITE_HEADER = b"SQLite format 3\x00"
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9._-]+")
_SKIP_PARTS = frozenset({"tmp"})
_SKIP_SUFFIXES = (".lock", "-wal", "-shm")
_MIGRATE_ENTRIES = (
    "auth",
    "chatgpt",
    "composio",
    "skills",
    "browser-private",
    "known-addresses",
    "grants.json",
    "approval-mode.json",
    "operating-profile.json",
    "browser-consent.json",
    "bud.db",
    "runtime.db",
    "spend.db",
    "memory.db",
    "recall.db",
    "preferences.db",
    "background.db",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65_536):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sqlite_file(path: Path) -> bool:
    if path.suffix == ".db":
        return True
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def _atomic_write_bytes(target: Path, data: bytes, *, mode: int = 0o600) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}-", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def _copy_file_consistent(src: Path, dst: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_sqlite_file(src):
        fd, temp_name = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}-", suffix=".tmp")
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            temp_path.chmod(0o600)
            with (
                closing(sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5)) as source_db,
                closing(sqlite3.connect(temp_path)) as dest_db,
            ):
                source_db.backup(dest_db)
            os.replace(temp_path, dst)
        except sqlite3.DatabaseError:
            temp_path.unlink(missing_ok=True)
            _atomic_write_bytes(dst, src.read_bytes(), mode=0o600)
        finally:
            temp_path.unlink(missing_ok=True)
    else:
        _atomic_write_bytes(dst, src.read_bytes(), mode=0o600)
    return _sha256_file(dst)


def _should_skip_data_rel(rel: Path) -> bool:
    parts = rel.parts
    if not parts or any(part in _SKIP_PARTS or part.startswith("tmp_") for part in parts):
        return True
    name = parts[-1]
    return bool(name.startswith(".") or name.endswith(_SKIP_SUFFIXES))


def _sanitize_label(label: str) -> str:
    cleaned = _SAFE_LABEL.sub("-", label).strip("-.")
    return cleaned or "snapshot"


def backup_data_dir(
    data_dir: Path,
    backups_root: Path,
    *,
    label: str,
    app_version: str,
    keep: int = 10,
) -> Path:
    """Create a verified, consistent snapshot of ``data_dir`` under ``backups_root``."""
    backups_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_label = _sanitize_label(label)
    backup_dir = backups_root / f"{stamp}-{safe_label}-{uuid4().hex[:6]}"
    staging_dir = backups_root / f".staging-{uuid4().hex}"
    staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    files: dict[str, str] = {}
    try:
        if data_dir.is_dir():
            for root, dirs, names in os.walk(data_dir, followlinks=False):
                root_path = Path(root)
                rel_dir = root_path.relative_to(data_dir)
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if not (root_path / d).is_symlink() and not _should_skip_data_rel(rel_dir / d)
                )
                for name in sorted(names):
                    src = root_path / name
                    if src.is_symlink() or not src.is_file():
                        continue
                    rel = rel_dir / name
                    if _should_skip_data_rel(rel):
                        continue
                    dst = staging_dir / rel
                    files[rel.as_posix()] = _copy_file_consistent(src, dst)

        config_env = data_dir.parent / "config.env"
        if config_env.is_file() and not config_env.is_symlink():
            files["_config.env"] = _copy_file_consistent(config_env, staging_dir / "_config.env")

        manifest = {
            "version": app_version,
            "label": label,
            "created_at": datetime.now(tz=UTC).isoformat(),
            "data_dir": str(data_dir),
            "files": files,
        }
        _atomic_write_bytes(
            staging_dir / "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            mode=0o600,
        )
        os.replace(staging_dir, backup_dir)
    except Exception as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise BackupError(f"Failed to create backup: {exc}") from exc

    if keep >= 1:
        _prune_backups(backups_root, keep=keep)
    return backup_dir


def _prune_backups(backups_root: Path, *, keep: int) -> None:
    existing = sorted(
        (
            entry
            for entry in backups_root.iterdir()
            if entry.is_dir()
            and not entry.name.startswith(".")
            and (entry / "manifest.json").is_file()
        ),
        key=lambda p: p.name,
    )
    excess = len(existing) - keep
    for old in existing[: max(0, excess)]:
        shutil.rmtree(old, ignore_errors=True)


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    """Verify backup manifest and SHA-256 digests of every backed-up file."""
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BackupError(f"Missing backup manifest at {manifest_path}.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"Corrupt backup manifest at {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise BackupError(f"Invalid backup manifest format in {manifest_path}.")

    backup_resolved = backup_dir.resolve()
    for rel_str, expected_hash in manifest["files"].items():
        if (
            not isinstance(rel_str, str)
            or not isinstance(expected_hash, str)
            or Path(rel_str).is_absolute()
            or ".." in Path(rel_str).parts
        ):
            raise BackupError(f"Unsafe backup entry path: {rel_str!r}.")
        candidate = backup_dir / rel_str
        if candidate.is_symlink() or not candidate.is_file():
            raise BackupError(f"Missing backed-up file: {rel_str}.")
        if not candidate.resolve().is_relative_to(backup_resolved):
            raise BackupError(f"Backup entry escapes backup root: {rel_str}.")
        actual = _sha256_file(candidate)
        if actual != expected_hash:
            raise BackupError(f"Checksum mismatch for {rel_str} in {backup_dir}.")
    return manifest


def restore_backup(backup_dir: Path, data_dir: Path) -> None:
    """Restore a verified backup into ``data_dir`` atomically, clearing stale WAL sidecars."""
    manifest = verify_backup(backup_dir)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_resolved = data_dir.resolve()

    for rel_str in sorted(manifest["files"]):
        src = backup_dir / rel_str
        if rel_str == "_config.env":
            _atomic_write_bytes(data_dir.parent / "config.env", src.read_bytes(), mode=0o600)
            continue
        target = data_dir / rel_str
        if not target.resolve().is_relative_to(data_resolved):
            raise BackupError(f"Restore target escapes data_dir: {rel_str}.")
        _atomic_write_bytes(target, src.read_bytes(), mode=0o600)
        if _is_sqlite_file(target):
            for suffix in ("-wal", "-shm"):
                with suppress(OSError):
                    target.with_name(target.name + suffix).unlink(missing_ok=True)


def list_backups(backups_root: Path) -> list[dict[str, Any]]:
    """Return backup summaries ordered newest first."""
    if not backups_root.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for folder in sorted(backups_root.iterdir(), key=lambda p: p.name, reverse=True):
        manifest_path = folder / "manifest.json"
        if not folder.is_dir() or folder.name.startswith(".") or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        entries.append(
            {
                "name": folder.name,
                "path": folder,
                "version": manifest.get("version"),
                "label": manifest.get("label"),
                "created_at": manifest.get("created_at"),
                "file_count": len(manifest.get("files", {})),
            }
        )
    return entries


def _rewrite_config_env(raw: str) -> str:
    """Drop checkout-relative DATA_DIR=data so ~/.bud defaults cleanly apply."""
    out: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            cleaned = value.strip().strip("\"'")
            if key.strip().upper() == "DATA_DIR" and cleaned in {"data", "./data"}:
                out.append(f"# migrated from checkout: {line}")
                continue
        out.append(line)
    return "\n".join(out) + ("\n" if raw.endswith("\n") or out else "")


def migrate_from_checkout(repo_dir: Path, bud_home: Path) -> dict[str, Any]:
    """Seed a fresh ``bud_home`` from an existing repo checkout without altering it."""
    bud_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    for sub in ("data", "workspace", "logs", "backups", "releases", "bin"):
        (bud_home / sub).mkdir(parents=True, exist_ok=True, mode=0o700)

    repo = repo_dir.expanduser().resolve()
    config_dst = bud_home / "config.env"
    copied_env = False
    repo_env = repo / ".env"
    if repo_env.is_file() and not repo_env.is_symlink() and not config_dst.exists():
        _atomic_write_bytes(
            config_dst,
            _rewrite_config_env(repo_env.read_text(encoding="utf-8")).encode("utf-8"),
            mode=0o600,
        )
        copied_env = True

    target_data = bud_home / "data"
    has_existing_state = any(
        (target_data / marker).exists()
        for marker in ("bud.db", "composio/connections.db", "auth/owner.token")
    )
    copied_files: list[str] = []
    repo_data = repo / "data"
    if not has_existing_state and repo_data.is_dir():
        for name in _MIGRATE_ENTRIES:
            src = repo_data / name
            if src.is_symlink() or not src.exists():
                continue
            if src.is_file():
                _copy_file_consistent(src, target_data / name)
                copied_files.append(name)
            elif src.is_dir():
                for root, dirs, files in os.walk(src, followlinks=False):
                    root_path = Path(root)
                    rel_dir = root_path.relative_to(repo_data)
                    dirs[:] = sorted(
                        d
                        for d in dirs
                        if not (root_path / d).is_symlink()
                        and not _should_skip_data_rel(rel_dir / d)
                    )
                    for fname in sorted(files):
                        fsrc = root_path / fname
                        if fsrc.is_symlink() or not fsrc.is_file():
                            continue
                        rel = rel_dir / fname
                        if _should_skip_data_rel(rel):
                            continue
                        _copy_file_consistent(fsrc, target_data / rel)
                        copied_files.append(rel.as_posix())

    return {
        "bud_home": bud_home,
        "copied_env": copied_env,
        "copied_files": copied_files,
        "skipped_existing_data": has_existing_state,
    }
