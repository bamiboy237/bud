"""Pack the portable part of a stopped Bud profile."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

from dotenv import dotenv_values

from bud import __version__
from bud.composio.binding import BindingStore
from bud.config import Settings
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.projection import parked_approvals, queued_messages, runs, undecided_approvals
from bud.core.routines import RoutineStore
from bud.core.workspace_home import workspace_home
from bud.release.backup import _atomic_write_bytes, _copy_file_consistent
from bud.release.move_archive import (
    MAX_MOVE_BYTES,
    MAX_MOVE_FILES,
    RuntimeMoveError,
    digest,
    regular_files,
    unpack_archive,
)

_STATE_FILES = frozenset(
    {
        "approval-mode.json",
        "background.db",
        "bud.db",
        "default-model",
        "memory.db",
        "operating-profile.json",
        "preferences.db",
        "recall.db",
        "spend.db",
    }
)
_STATE_FOLDERS = frozenset({"known-addresses", "skills"})
_PORTABLE_FIELDS = (
    "approval_review",
    "approval_review_model",
    "composio_api_key",
    "daily_spend_usd",
    "deepseek_api_key",
    "default_model",
    "exa_api_key",
    "mistral_api_key",
    "model_api_keys",
    "model_fallback",
    "muse_api_key",
    "openai_api_key",
    "operating_profile",
)
_PORTABLE_ENV_NAMES = {
    "BUD_DAILY_USD" if name == "daily_spend_usd" else name.upper() for name in _PORTABLE_FIELDS
}


def _permitted_data(path: Path) -> bool:
    parts = path.parts
    if any(part.startswith(".") or part.endswith((".lock", "-wal", "-shm")) for part in parts):
        return False
    if parts[:2] == ("composio", "sessions"):
        parts = parts[3:]
    elif parts in {
        ("composio", "connections.db"),
        ("chatgpt", "credentials.json"),
        ("chatgpt", "installation.json"),
    }:
        return True
    if len(parts) == 1:
        return parts[0] in _STATE_FILES
    return bool(parts and parts[0] in _STATE_FOLDERS)


def _permitted(path: Path) -> bool:
    if path.as_posix() in {"config.env", "manifest.json"}:
        return True
    return len(path.parts) > 1 and (
        path.parts[0] == "workspace"
        or (path.parts[0] == "data" and _permitted_data(Path(*path.parts[1:])))
    )


def _portable_paths(data: Path) -> list[Path]:
    # Do not walk excluded folders (e.g. browser state or tool output) at all.
    paths = []
    roots = [data]
    composio = data / "composio"
    scopes = composio / "sessions"
    if composio.is_symlink() or scopes.is_symlink() or (data / "chatgpt").is_symlink():
        raise RuntimeMoveError("Bud data contains a symbolic link.")
    if scopes.is_dir():
        for scope in sorted(scopes.iterdir()):
            if scope.is_symlink():
                raise RuntimeMoveError("Bud data contains a symbolic link.")
            if scope.is_dir():
                roots.append(scope)
    for root in roots:
        paths.extend(root / name for name in sorted(_STATE_FILES))
        for folder in sorted(_STATE_FOLDERS):
            paths.extend(regular_files(root / folder))
    paths.extend(
        (
            composio / "connections.db",
            data / "chatgpt/credentials.json",
            data / "chatgpt/installation.json",
        )
    )
    if any(path.is_symlink() for path in paths):
        raise RuntimeMoveError("Bud data contains a symbolic link.")
    return [path for path in paths if path.is_file() and _permitted_data(path.relative_to(data))]


def _settings_file(settings: Settings, path: Path) -> None:
    values = settings.model_dump(include=set(_PORTABLE_FIELDS), exclude_none=True)
    values["model_api_keys"] = {k: v.get_secret_value() for k, v in settings.model_api_keys.items()}
    lines = []
    for name, value in values.items():
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if name == "daily_spend_usd":
            name = "bud_daily_usd"
        if isinstance(value, bool | dict):
            value = json.dumps(value)
        lines.append(f"{name.upper()}={json.dumps(str(value), ensure_ascii=False)}\n")
    _atomic_write_bytes(path, "".join(lines).encode())


def _check_idle(data: Path) -> None:
    sessions = data / "composio" / "sessions"
    for database in sorted(sessions.glob("*/bud.db")):
        log = EventLog(database)
        if log.pending_runs() or log.unfinished_runs():
            raise RuntimeMoveError("Finish or stop pending Bud work before moving.")
        if any(queued_messages(log.read(session)) for session in log.queue_sessions()):
            raise RuntimeMoveError("Finish or clear queued messages before moving Bud.")
        for session in log.sessions_with_requests():
            events = log.read(session)
            if undecided_approvals(events) or parked_approvals(events):
                raise RuntimeMoveError("Resolve open approvals before moving Bud.")
        for session in log.sessions_with_waiting_runs():
            if any(run.status == "awaiting_approval" for run in runs(log.read(session))[-1:]):
                raise RuntimeMoveError(
                    "Finish the conversation waiting for an answer before moving."
                )
        for session in log.setup_wakeup_sessions():
            if log.pending_setup_wakeups(session):
                raise RuntimeMoveError("Finish pending sign-ins before moving Bud.")
        routine_store = RoutineStore(Hub(log))
        if routine_store.active_occurrences() or routine_store.unconfirmed_deliveries():
            raise RuntimeMoveError("Finish active routine runs and deliveries before moving Bud.")
        background = database.with_name("background.db")
        if background.is_file():
            with closing(sqlite3.connect(f"{background.as_uri()}?mode=ro", uri=True)) as db:
                if db.execute(
                    "SELECT 1 FROM items WHERE kind = 'command' "
                    "AND status IN ('waiting', 'running') LIMIT 1"
                ).fetchone():
                    raise RuntimeMoveError("Stop background commands before moving Bud.")
    binding_path = data / "composio" / "connections.db"
    if binding_path.is_file():
        _, pending = BindingStore(binding_path).snapshot()
        if pending:
            raise RuntimeMoveError("Finish pending app sign-ins before moving Bud.")
        return
    raise RuntimeMoveError("Open Bud on this Mac before moving its runtime.")


def _make_routine_reports_portable(database: Path, workspace: Path) -> None:
    with closing(sqlite3.connect(database)) as db, db:
        rows = db.execute(
            "SELECT occurrence_id, report_path FROM routine_occurrences "
            "WHERE report_path IS NOT NULL"
        ).fetchall()
        for occurrence, value in rows:
            path = Path(value)
            if path.is_absolute() and path.is_relative_to(workspace):
                db.execute(
                    "UPDATE routine_occurrences SET report_path = ? WHERE occurrence_id = ?",
                    (path.relative_to(workspace).as_posix(), occurrence),
                )


def _detach_workspace(messages: list[dict], source_ref: dict[str, str]) -> bool:
    changed = False
    for message in messages:
        if message.get("kind") == "response" and message.get("workspace_ref") == source_ref:
            del message["workspace_ref"]
            changed = True
    return changed


def _make_chat_workspaces_portable(database: Path, workspace: Path) -> None:
    # The destination resolves its configured workspace when history has no ref.
    # Leave refs to other environments alone: their state did not move.
    source_ref = {"provider": "local", "id": str(workspace)}
    with closing(sqlite3.connect(database)) as db, db:
        for run_id, data in db.execute("SELECT run_id, data FROM conversations"):
            snapshot = json.loads(data)
            if _detach_workspace(snapshot["messages"], source_ref):
                db.execute(
                    "UPDATE conversations SET data = ? WHERE run_id = ?",
                    (json.dumps(snapshot), run_id),
                )

        # After a failed turn Bud can rebuild history from the run events.
        for event_id, data in db.execute(
            "SELECT id, data FROM events WHERE type IN ('run.completed', 'run.waiting')"
        ):
            event = json.loads(data)
            changed = _detach_workspace(event.get("messages", []), source_ref)
            if raw := event.get("conversation"):
                # Older logs kept the saved conversation inside the event.
                snapshot = json.loads(raw) if isinstance(raw, str) else raw
                if _detach_workspace(snapshot["messages"], source_ref):
                    event["conversation"] = (
                        json.dumps(snapshot) if isinstance(raw, str) else snapshot
                    )
                    changed = True
            if changed:
                db.execute("UPDATE events SET data = ? WHERE id = ?", (json.dumps(event), event_id))


def pack_runtime(settings: Settings, output: Path) -> None:
    """Write a private archive after the caller has stopped and fenced the source."""
    data = settings.data_dir.resolve()
    if settings.models_file is not None or settings.photon_api_key is not None:
        raise RuntimeMoveError("Remove the custom model catalog or iMessage setup before moving.")
    _check_idle(data)
    workspace = workspace_home(settings)
    files = {
        **{Path("data") / p.relative_to(data): p for p in _portable_paths(data)},
        **{Path("workspace") / p.relative_to(workspace): p for p in regular_files(workspace)},
    }
    if (
        len(files) + 2 > MAX_MOVE_FILES
        or sum(p.stat().st_size for p in files.values()) > MAX_MOVE_BYTES
    ):
        raise RuntimeMoveError("This Bud profile is too large to move.")
    with TemporaryDirectory(dir=output.parent, prefix=".bud-move-") as directory:
        stage = Path(directory)
        config = stage / "config.env"
        _settings_file(settings, config)
        digests = {"config.env": digest(config)}
        for relative, path in files.items():
            snapshot = stage / relative
            checksum = _copy_file_consistent(path, snapshot)
            if relative.parts[:3] == ("data", "composio", "sessions") and relative.name == "bud.db":
                _make_routine_reports_portable(snapshot, workspace)
                _make_chat_workspaces_portable(snapshot, workspace)
                checksum = digest(snapshot)
            digests[relative.as_posix()] = checksum
        manifest = stage / "manifest.json"
        manifest.write_text(json.dumps({"version": __version__, "files": digests}))
        names = ["manifest.json", *digests]
        if sum((stage / name).stat().st_size for name in names) > MAX_MOVE_BYTES:
            raise RuntimeMoveError("This Bud profile is too large to move.")
        archive = stage / "move.zip"
        with ZipFile(archive, "x", compression=ZIP_DEFLATED) as package:
            for name in names:
                package.write(stage / name, name)
        if archive.stat().st_size > MAX_MOVE_BYTES:
            raise RuntimeMoveError("This Bud profile is too large to move.")
        archive.chmod(0o600)
        os.link(archive, output)


def verify_runtime(archive: Path, stage: Path) -> None:
    unpack_archive(archive, stage, _permitted)
    manifest = json.loads((stage / "manifest.json").read_bytes())
    files = {
        path.relative_to(stage).as_posix(): digest(path)
        for path in regular_files(stage)
        if path != stage / "manifest.json"
    }
    if manifest != {"version": __version__, "files": files}:
        raise RuntimeMoveError("The archive failed verification or has a different Bud version.")
    for name in files:
        if not (name.startswith("data/") and name.endswith(".db")):
            continue
        with closing(sqlite3.connect(f"{(stage / name).as_uri()}?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RuntimeMoveError("A Bud database failed its integrity check.")
    config = stage / "config.env"
    if not config.is_file():
        raise RuntimeMoveError("The Bud runtime archive is missing its configuration.")
    if not dotenv_values(config, interpolate=False).keys() <= _PORTABLE_ENV_NAMES:
        raise RuntimeMoveError("The Bud archive contains non-portable settings.")
    Settings(_env_file=config)
    _check_idle(stage / "data")
