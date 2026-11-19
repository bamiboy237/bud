"""Bud's home workspace and the one-time, copy-only move of older workspaces into it."""

import json
import os
import shutil

import pytest

from bud.config import Settings
from bud.core import workspace_home as module
from bud.core.server import owner_workspace
from bud.core.workspace_home import MigrationError, legacy_workspaces, migrate, workspace_home


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def data(tmp_path):
    data = tmp_path / "data"
    write(data / "workspace" / "notes.md", "simulate-era notes")
    write(data / "composio" / "sessions" / "a" / "workspace" / "reports" / "weekly.md", "week 1")
    write(data / "composio" / "sessions" / "b" / "workspace" / "notes.md", "account b notes")
    return data


def test_a_fresh_install_gets_a_private_home(tmp_path):
    home = workspace_home(Settings(workspace_dir=tmp_path / ".bud" / "workspace"))
    assert home.is_dir()
    assert oct(home.stat().st_mode & 0o777) == "0o700"


def test_files_are_copied_in_and_originals_kept(tmp_path, data):
    home = tmp_path / "home"
    home.mkdir()
    record = data / "workspace-migration.json"

    moved = migrate(legacy_workspaces(data), home, record)

    assert len(moved) == 3
    assert (home / "reports" / "weekly.md").read_text() == "week 1"
    # Two folders had notes.md: the first keeps the name, the second gets a dated suffix.
    assert (home / "notes.md").read_text() == "simulate-era notes"
    [renamed] = home.glob("notes (from *).md")
    assert renamed.read_text() == "account b notes"
    # Nothing is deleted, and nothing is left behind in a staging folder.
    assert (data / "workspace" / "notes.md").read_text() == "simulate-era notes"
    assert not (tmp_path / ".migrating").exists()
    assert len(json.loads(record.read_text())["done"]) == 3


def test_a_second_start_copies_nothing(tmp_path, data):
    home = tmp_path / "home"
    home.mkdir()
    record = data / "workspace-migration.json"
    migrate(legacy_workspaces(data), home, record)
    before = sorted(home.rglob("*"))

    assert migrate(legacy_workspaces(data), home, record) == []
    assert sorted(home.rglob("*")) == before


def test_an_existing_file_in_the_home_is_never_overwritten(tmp_path, data):
    home = tmp_path / "home"
    write(home / "notes.md", "written in the new home")

    migrate(legacy_workspaces(data), home, data / "workspace-migration.json")

    assert (home / "notes.md").read_text() == "written in the new home"
    texts = {path.read_text() for path in home.glob("notes (from *).md")}
    assert texts == {"simulate-era notes", "account b notes"}


def test_an_interrupted_move_resumes_without_duplicates(tmp_path, data, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    record = data / "workspace-migration.json"
    source = data / "composio" / "sessions" / "a" / "workspace"
    write(source / "second.md", "two")
    placed = []
    original = module._place

    def stop_after_one(copy, wanted, original_file):
        if placed:
            raise KeyboardInterrupt
        placed.append(wanted)
        original(copy, wanted, original_file)

    monkeypatch.setattr(module, "_place", stop_after_one)
    with pytest.raises(KeyboardInterrupt):
        migrate([source], home, record)
    assert not record.exists()

    monkeypatch.setattr(module, "_place", original)
    migrate([source], home, record)

    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*.md")) == [
        "reports/weekly.md",
        "second.md",
    ]


def test_a_bad_copy_places_nothing_and_is_retried(tmp_path, data, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    record = data / "workspace-migration.json"
    source = data / "composio" / "sessions" / "a" / "workspace"

    def corrupt(src, dst):
        shutil.copyfile(src, dst)
        with open(dst, "a") as file:
            file.write("!")

    monkeypatch.setattr(module.shutil, "copy2", corrupt)
    with pytest.raises(MigrationError):
        migrate([source], home, record)
    assert list(home.iterdir()) == []
    assert not record.exists()
    assert (source / "reports" / "weekly.md").read_text() == "week 1"

    monkeypatch.undo()
    assert migrate([source], home, record) == [source]


def test_symlinks_in_an_old_workspace_are_not_followed(tmp_path):
    secret = write(tmp_path / "secret.txt", "private")
    source = tmp_path / "old"
    write(source / "kept.md", "kept")
    os.symlink(secret, source / "link.txt")
    os.symlink(tmp_path, source / "linked-dir", target_is_directory=True)
    home = tmp_path / "home"
    home.mkdir()

    migrate([source], home, tmp_path / "record.json")

    assert [path.name for path in home.iterdir()] == ["kept.md"]


def test_the_server_keeps_the_old_workspace_when_the_move_fails(tmp_path, data, monkeypatch):
    settings = Settings(data_dir=data, workspace_dir=tmp_path / ".bud" / "workspace")
    session_dir = data / "composio" / "sessions" / "a"

    def fail(*_):
        raise MigrationError("weekly.md did not copy correctly")

    # This account's folder was copied on an earlier start; then a later copy fails.
    owner_workspace(settings, session_dir)
    monkeypatch.setattr("bud.core.server.migrate", fail)
    old = owner_workspace(settings, session_dir)
    assert old == session_dir.resolve() / "workspace"
    write(old / "during-fallback.md", "written while the move was failing")

    monkeypatch.undo()
    home = owner_workspace(settings, session_dir)
    assert home == (tmp_path / ".bud").resolve() / "workspace"
    assert (home / "reports" / "weekly.md").read_text() == "week 1"
    assert (home / "during-fallback.md").exists()
    assert not list(home.glob("weekly (from *).md"))
