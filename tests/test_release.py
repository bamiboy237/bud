"""Offline unit tests for pre-update backups, LaunchAgent service, updater, and rollback."""

from __future__ import annotations

import hashlib
import io
import json
import plistlib
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path

import pytest

from bud import cli
from bud.core.log import EventLog
from bud.release import BackupError, ServiceError, UpdateError, UpdateRollbackError
from bud.release.backup import (
    backup_data_dir,
    list_backups,
    migrate_from_checkout,
    restore_backup,
    verify_backup,
)
from bud.release.service import LAUNCH_AGENT_LABEL, ServiceManager, render_launchd_plist
from bud.release.updater import (
    ReleaseInfo,
    apply_update,
    download_and_verify_release,
    fetch_github_release,
    install_release_dir,
    is_newer_version,
    rollback_release,
    safe_extract_tar,
    verify_sha256_sums,
)


def _write_wal_db(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT)")
    db.execute("DELETE FROM notes")
    db.execute("INSERT INTO notes VALUES (1, ?)", (value,))
    db.commit()
    db.close()


def _read_note(path: Path) -> str:
    with closing(sqlite3.connect(path)) as db:
        row = db.execute("SELECT body FROM notes WHERE id = 1").fetchone()
    assert row is not None
    return str(row[0])


def _seed_bud_home(home: Path, note: str = "before-update") -> Path:
    data = home / "data"
    (data / "auth").mkdir(parents=True, mode=0o700)
    (data / "auth" / "owner.token").write_text("A" * 43, encoding="ascii")
    (data / "auth" / "owner.token").chmod(0o600)
    (home / "config.env").write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")
    _write_wal_db(data / "bud.db", note)
    return data


def _make_fake_release(
    root: Path, *, version: str, app_marker: str | None = None
) -> tuple[str, Path]:
    bundle = root / f"bundle-{version}"
    (bundle / "wheel").mkdir(parents=True, exist_ok=True)
    (bundle / "bin").mkdir(parents=True, exist_ok=True)
    (bundle / "wheel" / f"bud-{version}-py3-none-any.whl").write_bytes(b"fake-wheel")
    (bundle / "manifest.json").write_text(
        json.dumps({"version": version, "arch": "arm64"}), encoding="utf-8"
    )
    if app_marker is not None:
        mac_os = bundle / "Bud.app" / "Contents" / "MacOS"
        mac_os.mkdir(parents=True, exist_ok=True)
        (mac_os / "bud").write_text(app_marker, encoding="utf-8")
    ver, rel_dir = install_release_dir(
        bundle,
        root / "bud_home",
        install_deps=False,
        runner=lambda _cmd: (0, "", ""),
    )
    (rel_dir / "bin" / "bud").write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
    (rel_dir / "bin" / "bud").chmod(0o755)
    return ver, rel_dir


def test_backup_verify_and_restore_preserves_wal_database_and_clears_sidecars(tmp_path):
    bud_home = tmp_path / "bud"
    data = _seed_bud_home(bud_home, "v1-important-history")
    # Leave uncheckpointed WAL data open while backing up.
    live = sqlite3.connect(data / "bud.db")
    live.execute("INSERT INTO notes VALUES (2, 'uncheckpointed-wal-row')")
    live.commit()
    (data / "tmp").mkdir()
    (data / "tmp" / "scratch.txt").write_text("skip-me", encoding="utf-8")
    (data / "live-server.lock").write_text("", encoding="utf-8")

    try:
        backup = backup_data_dir(data, bud_home / "backups", label="pre-0.2.0", app_version="0.1.0")
    finally:
        live.close()

    manifest = verify_backup(backup)
    assert "bud.db" in manifest["files"]
    assert "auth/owner.token" in manifest["files"]
    assert "_config.env" in manifest["files"]
    assert "live-server.lock" not in manifest["files"]
    assert not any(k.startswith("tmp/") for k in manifest["files"])
    with closing(sqlite3.connect(backup / "bud.db")) as bdb:
        rows = bdb.execute("SELECT id, body FROM notes ORDER BY id").fetchall()
    assert rows == [(1, "v1-important-history"), (2, "uncheckpointed-wal-row")]

    # Simulate destructive mutation + stale WAL file, then restore.
    _write_wal_db(data / "bud.db", "corrupted-after-failed-upgrade")
    wal_sidecar = data / "bud.db-wal"
    wal_sidecar.write_bytes(b"stale-wal-garbage")
    restore_backup(backup, data)
    assert not wal_sidecar.exists()
    assert _read_note(data / "bud.db") == "v1-important-history"


def test_verify_backup_rejects_corruption_and_traversal(tmp_path):
    bud_home = tmp_path / "bud"
    data = _seed_bud_home(bud_home, "clean")
    backup = backup_data_dir(data, bud_home / "backups", label="check", app_version="0.1.0")
    (backup / "auth" / "owner.token").write_text("TAMPERED", encoding="ascii")
    with pytest.raises(BackupError, match="Checksum mismatch"):
        verify_backup(backup)

    bad = tmp_path / "bad-backup"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps({"files": {"../escape": "0" * 64}}))
    with pytest.raises(BackupError, match="Unsafe backup entry path"):
        verify_backup(bad)


def test_backup_retention_pruning(tmp_path):
    bud_home = tmp_path / "bud"
    data = _seed_bud_home(bud_home)
    for idx in range(4):
        backup_data_dir(
            data, bud_home / "backups", label=f"snap-{idx}", app_version="0.1.0", keep=2
        )
    listed = list_backups(bud_home / "backups")
    assert len(listed) == 2


def test_migrate_from_checkout_seeds_fresh_home_without_touching_source(tmp_path):
    repo = tmp_path / "repo"
    (repo / "data" / "auth").mkdir(parents=True)
    (repo / "data" / "tmp_test").mkdir(parents=True)
    (repo / ".env").write_text("OPENAI_API_KEY=sk-live\nDATA_DIR=data\nBUD_DAILY_USD=5\n")
    (repo / "data" / "auth" / "owner.token").write_text("T" * 43)
    _write_wal_db(repo / "data" / "bud.db", "repo-chat")
    (repo / "data" / "tmp_test" / "ignore.db").write_text("temp")

    bud_home = tmp_path / ".bud"
    report = migrate_from_checkout(repo, bud_home)
    assert report["copied_env"] is True
    assert "bud.db" in report["copied_files"]
    assert not any("tmp_test" in item for item in report["copied_files"])
    config_text = (bud_home / "config.env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-live" in config_text
    assert "# migrated from checkout: DATA_DIR=data" in config_text
    assert _read_note(bud_home / "data" / "bud.db") == "repo-chat"

    # Re-running against populated bud_home leaves existing data untouched.
    _write_wal_db(bud_home / "data" / "bud.db", "already-migrated")
    second = migrate_from_checkout(repo, bud_home)
    assert second["skipped_existing_data"] is True
    assert _read_note(bud_home / "data" / "bud.db") == "already-migrated"


def test_render_launchd_plist_and_service_manager(tmp_path):
    bud_home = tmp_path / "bud-home"
    plist_bytes = render_launchd_plist(
        bud_home / "releases" / "current" / "bin" / "bud", bud_home, port=9420
    )
    parsed = plistlib.loads(plist_bytes)
    assert parsed["Label"] == LAUNCH_AGENT_LABEL
    assert parsed["ProgramArguments"][-2:] == ["--home", str(bud_home)]
    assert parsed["WorkingDirectory"] == str(bud_home)
    assert parsed["EnvironmentVariables"]["DATA_DIR"] == str(bud_home / "data")
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}

    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]) -> tuple[int, str, str]:
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return 0, "state = running\n\tpid = 4321\n", ""
        return 0, "", ""

    svc = ServiceManager(
        bud_home,
        plist_path=tmp_path / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist",
        runner=fake_runner,
        uid=501,
    )
    svc.install(bud_home / "releases" / "current" / "bin" / "bud", port=9420)
    assert svc.plist_path.is_file()
    status = svc.status()
    assert status["installed"] is True
    assert status["loaded"] is True
    assert status["pid"] == 4321

    health = svc.wait_healthy(
        "http://127.0.0.1:9420",
        "tok",
        expected_version="0.2.0",
        fetcher=lambda _u, _t: {"version": "0.2.0"},
    )
    assert health["version"] == "0.2.0"

    with pytest.raises(ServiceError, match="expected '0.3.0'"):
        svc.wait_healthy(
            "http://127.0.0.1:9420",
            "tok",
            expected_version="0.3.0",
            fetcher=lambda _u, _t: {"version": "0.2.0"},
        )


def test_github_release_verification_and_safe_tar_extraction(tmp_path):
    assert is_newer_version("v0.2.0", "0.1.0")
    assert is_newer_version("0.2.0", "0.2.0-rc1")
    assert not is_newer_version("0.1.0", "v0.1.0")

    payload = {
        "tag_name": "v0.2.0",
        "html_url": "https://github.com/bamiboy237/bud/releases/tag/v0.2.0",
        "assets": [
            {
                "name": "bud-0.2.0-darwin-arm64.tar.gz",
                "browser_download_url": "https://example.invalid/bud-0.2.0-darwin-arm64.tar.gz",
            },
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": "https://example.invalid/SHA256SUMS.txt",
            },
        ],
    }
    release = fetch_github_release(
        arch="arm64",
        http_get=lambda _u: json.dumps(payload).encode("utf-8"),
    )
    assert isinstance(release, ReleaseInfo)
    assert release.version == "0.2.0"

    # Build a clean tarball and SHA256SUMS.txt.
    archive = tmp_path / "bud-0.2.0-darwin-arm64.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        manifest_bytes = json.dumps({"version": "0.2.0"}).encode("utf-8")
        info = tarfile.TarInfo("bud-0.2.0-darwin-arm64/manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sums_text = f"{digest}  bud-0.2.0-darwin-arm64.tar.gz\n"
    assert verify_sha256_sums(archive, sums_text) == digest

    downloaded = download_and_verify_release(
        release,
        tmp_path / "dl",
        http_get=lambda u: sums_text.encode("utf-8") if "SHA256" in u else archive.read_bytes(),
    )
    assert (downloaded / "manifest.json").is_file()

    # Corrupted tarball fails SHA-256 verification.
    with pytest.raises(UpdateError, match="Checksum mismatch"):
        download_and_verify_release(
            release,
            tmp_path / "dl-bad",
            http_get=lambda u: sums_text.encode("utf-8") if "SHA256" in u else b"tampered",
        )

    # Traversal or symlink members inside tar are refused.
    unsafe_tar = tmp_path / "unsafe.tar.gz"
    with tarfile.open(unsafe_tar, "w:gz") as tar:
        bad_info = tarfile.TarInfo("../evil")
        bad_info.size = 3
        tar.addfile(bad_info, io.BytesIO(b"bad"))
    with pytest.raises(UpdateError, match="Unsafe member"):
        safe_extract_tar(unsafe_tar, tmp_path / "unsafe-out")

    symlink_tar = tmp_path / "symlink.tar.gz"
    with tarfile.open(symlink_tar, "w:gz") as tar:
        sym = tarfile.TarInfo("link")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "/etc/passwd"
        tar.addfile(sym)
    with pytest.raises(UpdateError, match="Unsafe member"):
        safe_extract_tar(symlink_tar, tmp_path / "symlink-out")


def test_apply_update_success_and_automatic_rollback(tmp_path):
    bud_home = tmp_path / "bud_home"
    data = _seed_bud_home(bud_home, "original-chat-history")
    app_dir = tmp_path / "Applications"

    v1, rel1 = _make_fake_release(tmp_path, version="0.1.0", app_marker="app-v1")
    v2, rel2 = _make_fake_release(tmp_path, version="0.2.0", app_marker="app-v2")

    calls: list[list[str]] = []
    svc = ServiceManager(
        bud_home,
        plist_path=tmp_path / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist",
        runner=lambda cmd: (calls.append(cmd), (0, "", ""))[1],
        uid=501,
    )

    first = apply_update(
        v1,
        rel1,
        bud_home,
        app_install_dir=app_dir,
        service=svc,
        fetcher=lambda _u, _t: {"version": "0.1.0"},
    )
    assert first.new_version == "0.1.0"
    assert (bud_home / "releases" / "current").resolve() == rel1.resolve()
    assert (app_dir / "Bud.app" / "Contents" / "MacOS" / "bud").read_text() == "app-v1"

    # Simulate an active run blocking update unless --force is passed.
    log = EventLog(data / "bud.db")
    log.append("s1", "run.started", {"run_id": "r-active"})
    with pytest.raises(UpdateError, match="active run in progress"):
        apply_update(v2, rel2, bud_home, app_install_dir=app_dir, service=svc)
    log.append("s1", "run.completed", {"run_id": "r-active"})

    # Failed v0.2.0 healthcheck must trigger full automatic rollback to v0.1.0 and backup.
    def flaky_healthcheck(_u: str, _t: str) -> dict[str, str]:
        current_target = (bud_home / "releases" / "current").resolve()
        if current_target == rel2.resolve():
            # Corrupt data dir before failing to prove backup restore runs too.
            _write_wal_db(data / "bud.db", "broken-by-v2")
            raise ConnectionError("v2 crashed on startup")
        return {"version": "0.1.0"}

    with pytest.raises(UpdateRollbackError) as exc_info:
        apply_update(
            v2,
            rel2,
            bud_home,
            app_install_dir=app_dir,
            service=svc,
            fetcher=flaky_healthcheck,
            health_timeout=0.5,
        )
    assert exc_info.value.rollback_ok is True
    assert (bud_home / "releases" / "current").resolve() == rel1.resolve()
    assert (app_dir / "Bud.app" / "Contents" / "MacOS" / "bud").read_text() == "app-v1"
    assert _read_note(data / "bud.db") == "original-chat-history"

    # Manual rollback command also works cleanly.
    _write_wal_db(data / "bud.db", "manual-mutation")
    restored_from = rollback_release(
        bud_home,
        service=svc,
        fetcher=lambda _u, _t: {"version": "0.1.0"},
    )
    assert restored_from.is_dir()
    assert _read_note(data / "bud.db") == "original-chat-history"


def test_cli_release_commands_parse_and_dispatch(tmp_path, capsys):
    bud_home = tmp_path / "cli-bud"
    data = _seed_bud_home(bud_home, "cli-history")
    cli.main(["backup", "create", "--home", str(bud_home), "--label", "smoke"])
    out = capsys.readouterr().out.strip()
    assert Path(out).is_dir()

    cli.main(["backup", "list", "--home", str(bud_home)])
    listed = capsys.readouterr().out
    assert "label=smoke" in listed
    assert _read_note(data / "bud.db") == "cli-history"


def test_sign_app_if_needed_resigns_adhoc_with_local_cert_when_available(tmp_path, monkeypatch):
    from bud.release import updater

    monkeypatch.setattr(updater.sys, "platform", "darwin")
    app = tmp_path / "Bud.app"
    (app / "Contents" / "Helpers").mkdir(parents=True)
    (app / "Contents" / "Helpers" / "browse").write_text("#!/bin/sh\n")

    recorded: list[list[str]] = []

    def fake_run(cmd: list[str]) -> tuple[int, str, str]:
        recorded.append(cmd)
        if cmd[:2] == ["codesign", "-dvv"]:
            return 0, "", "Signature=adhoc\n"
        if cmd[:3] == ["security", "find-identity", "-p"]:
            return 0, '  1) ABC "bud companion (local)"\n', ""
        return 0, "", ""

    updater._sign_app_if_needed(app, runner=fake_run)
    sign_calls = [c for c in recorded if c[:2] == ["codesign", "--force"]]
    assert len(sign_calls) == 2
    assert sign_calls[0][:4] == ["codesign", "--force", "--sign", "bud companion (local)"]
    assert sign_calls[1][:4] == ["codesign", "--force", "--sign", "bud companion (local)"]

    # Developer ID (non-adhoc) signature is left untouched.
    recorded.clear()

    def signed_run(cmd: list[str]) -> tuple[int, str, str]:
        recorded.append(cmd)
        if cmd[:2] == ["codesign", "-dvv"]:
            return 0, "", "Authority=Developer ID Application: Example\n"
        return 0, "", ""

    updater._sign_app_if_needed(app, runner=signed_run)
    assert not any(c[:2] == ["codesign", "--force"] for c in recorded)
