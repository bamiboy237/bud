"""CLI subcommands for installing, updating, backing up, and managing daily-driver Bud."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from bud import __version__
from bud.config import PORT, default_bud_home
from bud.release import ReleaseError
from bud.release.backup import backup_data_dir, list_backups, migrate_from_checkout
from bud.release.service import ServiceManager
from bud.release.updater import (
    DEFAULT_APP_DIR,
    DEFAULT_GITHUB_REPO,
    apply_update,
    build_local_release,
    current_release_version,
    download_and_verify_release,
    fetch_github_release,
    install_release_dir,
    is_newer_version,
    rollback_release,
)


def _resolve_bud_home(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env_home = os.environ.get("BUD_HOME", "").strip()
    return Path(env_home).expanduser() if env_home else default_bud_home()


def _source_checkout(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser().resolve()
    cwd = Path.cwd()
    return cwd if (cwd / "pyproject.toml").is_file() and (cwd / "src" / "bud").is_dir() else None


def add_parsers(commands: argparse._SubParsersAction) -> None:
    """Register `install`, `update`, `rollback`, `service`, and `backup` commands."""
    install_p = commands.add_parser("install", help="install standalone daily-driver Bud")
    install_p.add_argument(
        "--from-repo",
        nargs="?",
        const=".",
        help="build and install from a local Bud checkout (defaults to cwd if present)",
    )
    install_p.add_argument(
        "--migrate-from",
        help="copy .env and persistent data from a repo checkout into --home if fresh",
    )
    install_p.add_argument("--home", help="Bud home directory (default: ~/.bud)")
    install_p.add_argument("--port", type=int, default=PORT, help="server port (default: 8420)")
    install_p.add_argument("--app-dir", default=str(DEFAULT_APP_DIR), help="Mac app install folder")
    install_p.add_argument("--repo", default=DEFAULT_GITHUB_REPO, help="GitHub owner/repo")
    install_p.add_argument("--version", help="install a specific GitHub release tag")
    install_p.add_argument("--no-app", action="store_true", help="skip installing Bud.app")
    install_p.add_argument("--no-service", action="store_true", help="skip LaunchAgent setup")
    install_p.add_argument("--tui", action="store_true", help="also build and bundle bud-tui")

    update_p = commands.add_parser("update", help="safely update daily-driver Bud with backup")
    update_p.add_argument("--check", action="store_true", help="check GitHub Releases only")
    update_p.add_argument("--from-repo", help="update by building from a local checkout")
    update_p.add_argument("--version", help="install a specific GitHub release tag (e.g. v0.2.0)")
    update_p.add_argument("--repo", default=DEFAULT_GITHUB_REPO, help="GitHub owner/repo")
    update_p.add_argument("--home", help="Bud home directory (default: ~/.bud)")
    update_p.add_argument("--port", type=int, default=PORT, help="server port (default: 8420)")
    update_p.add_argument("--app-dir", default=str(DEFAULT_APP_DIR), help="Mac app install folder")
    update_p.add_argument("--no-app", action="store_true", help="skip updating Bud.app")
    update_p.add_argument("--no-service", action="store_true", help="skip LaunchAgent restart")
    update_p.add_argument("--force", action="store_true", help="update even if runs look active")
    update_p.add_argument("--yes", action="store_true", help="non-interactive confirmation")

    rollback_p = commands.add_parser(
        "rollback", help="restore data from a verified backup and restart"
    )
    rollback_p.add_argument("--home", help="Bud home directory (default: ~/.bud)")
    rollback_p.add_argument("--backup", help="backup folder name or path (defaults to latest)")
    rollback_p.add_argument("--port", type=int, default=PORT, help="server port (default: 8420)")
    rollback_p.add_argument("--no-service", action="store_true", help="do not restart LaunchAgent")

    service_p = commands.add_parser("service", help="manage the macOS LaunchAgent service")
    service_p.add_argument(
        "action",
        choices=["install", "start", "stop", "restart", "status", "uninstall", "logs"],
    )
    service_p.add_argument("--home", help="Bud home directory (default: ~/.bud)")
    service_p.add_argument("--port", type=int, default=PORT, help="server port (default: 8420)")
    service_p.add_argument("--lines", type=int, default=50, help="log lines to show for `logs`")

    backup_p = commands.add_parser("backup", help="create, list, or restore data backups")
    backup_p.add_argument("action", choices=["create", "list", "restore"])
    backup_p.add_argument("target", nargs="?", help="backup name or path for restore")
    backup_p.add_argument("--label", default="manual", help="label for `backup create`")
    backup_p.add_argument("--home", help="Bud home directory (default: ~/.bud)")

    move_p = commands.add_parser(
        "move-runtime", help="move the stopped Bud runtime to a prepared server (token on stdin)"
    )
    move_p.add_argument("source", type=Path)
    move_p.add_argument("destination")
    move_p.add_argument("workspace", type=Path, nargs="?")
    move_p.add_argument("--cancel", action="store_true")


def _install(args: argparse.Namespace) -> None:
    bud_home = _resolve_bud_home(args.home)
    repo_source = _source_checkout(args.from_repo)
    migrate_source = (
        Path(args.migrate_from).expanduser().resolve() if args.migrate_from else repo_source
    )
    if migrate_source is not None:
        migrated = migrate_from_checkout(migrate_source, bud_home)
        if migrated["copied_env"] or migrated["copied_files"]:
            print(
                f"Migrated config/data from {migrate_source} into {bud_home} "
                f"({len(migrated['copied_files'])} files)."
            )
    else:
        for sub in ("data", "workspace", "logs", "backups", "releases", "bin"):
            (bud_home / sub).mkdir(parents=True, exist_ok=True, mode=0o700)

    with tempfile.TemporaryDirectory(prefix="bud-install-") as tmp:
        stage = Path(tmp)
        if repo_source is not None and (args.from_repo is not None or args.version is None):
            bundle_dir = build_local_release(
                repo_source,
                stage / "bundle",
                build_app=not args.no_app and sys.platform == "darwin",
                build_tui=args.tui,
            )
        else:
            release = fetch_github_release(args.repo, tag=args.version)
            if release is None:
                raise ReleaseError(
                    "No published GitHub release found. Pass `--from-repo` to install locally."
                )
            bundle_dir = download_and_verify_release(release, stage)

        version, release_dir = install_release_dir(bundle_dir, bud_home)

    result = apply_update(
        version,
        release_dir,
        bud_home,
        app_install_dir=Path(args.app_dir),
        install_app=not args.no_app,
        manage_service=not args.no_service,
        port=args.port,
        force=True,
    )
    print(f"Installed Bud v{result.new_version} into {bud_home}.")
    print(f"CLI binary: {bud_home / 'bin' / 'bud'}")
    if result.app_updated:
        print(f"Mac app: {Path(args.app_dir).expanduser() / 'Bud.app'}")
    if result.service_restarted:
        print(f"Service running at http://127.0.0.1:{args.port}.")


def _update(args: argparse.Namespace) -> None:
    bud_home = _resolve_bud_home(args.home)
    current = current_release_version(bud_home) or __version__
    if args.check:
        release = fetch_github_release(args.repo, tag=args.version)
        if release is None:
            print("No published GitHub releases available yet.")
            return
        if is_newer_version(release.version, current):
            print(f"Update available: v{release.version} (installed: v{current}).")
        else:
            print(f"Bud v{current} is up to date (latest: v{release.version}).")
        return

    with tempfile.TemporaryDirectory(prefix="bud-update-") as tmp:
        stage = Path(tmp)
        if args.from_repo:
            repo = Path(args.from_repo).expanduser().resolve()
            bundle_dir = build_local_release(
                repo,
                stage / "bundle",
                build_app=not args.no_app and sys.platform == "darwin",
            )
        else:
            release = fetch_github_release(args.repo, tag=args.version)
            if release is None:
                raise ReleaseError("No published GitHub release found.")
            if (
                not args.force
                and not args.version
                and not is_newer_version(release.version, current)
            ):
                print(f"Bud v{current} is already up to date.")
                return
            bundle_dir = download_and_verify_release(release, stage)

        version, release_dir = install_release_dir(bundle_dir, bud_home)

    result = apply_update(
        version,
        release_dir,
        bud_home,
        app_install_dir=Path(args.app_dir),
        install_app=not args.no_app,
        manage_service=not args.no_service,
        port=args.port,
        force=args.force,
    )
    print(
        f"Updated Bud from v{result.previous_version or 'fresh'} to v{result.new_version}. "
        f"Backup: {result.backup_dir}"
    )


def _rollback(args: argparse.Namespace) -> None:
    bud_home = _resolve_bud_home(args.home)
    backup_path: Path | None = None
    if args.backup:
        candidate = Path(args.backup).expanduser()
        backup_path = candidate if candidate.is_absolute() else bud_home / "backups" / candidate
    restored = rollback_release(
        bud_home,
        backup_dir=backup_path,
        manage_service=not args.no_service,
        port=args.port,
    )
    print(f"Restored Bud data from {restored}.")


def _service(args: argparse.Namespace) -> None:
    bud_home = _resolve_bud_home(args.home)
    svc = ServiceManager(bud_home)
    action = args.action
    if action == "install":
        bud_bin = bud_home / "releases" / "current" / "bin" / "bud"
        if not bud_bin.exists():
            bud_bin = bud_home / "bin" / "bud"
        if not bud_bin.exists():
            raise ReleaseError(f"No installed Bud binary in {bud_home}. Run `bud install` first.")
        plist = svc.install(bud_bin, port=args.port, start=True)
        print(f"Installed and started LaunchAgent at {plist}.")
    elif action == "start":
        svc.start()
        print("Bud service started.")
    elif action == "stop":
        svc.stop()
        print("Bud service stopped.")
    elif action == "restart":
        svc.restart()
        print("Bud service restarted.")
    elif action == "uninstall":
        svc.uninstall()
        print("Bud service stopped and removed.")
    elif action == "status":
        info = svc.status()
        print(
            f"label={info['label']} installed={info['installed']} "
            f"loaded={info['loaded']} pid={info['pid']} lock_held={info['lock_held']}"
        )
    elif action == "logs":
        for name in ("server.stdout.log", "server.stderr.log"):
            log_file = bud_home / "logs" / name
            print(f"==> {log_file} <==")
            if log_file.is_file():
                lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                print("\n".join(lines[-args.lines :]))
            else:
                print("(empty)")


def _backup(args: argparse.Namespace) -> None:
    bud_home = _resolve_bud_home(args.home)
    data_dir = bud_home / "data"
    backups_root = bud_home / "backups"
    if args.action == "create":
        version = current_release_version(bud_home) or __version__
        created = backup_data_dir(data_dir, backups_root, label=args.label, app_version=version)
        print(created)
    elif args.action == "list":
        entries = list_backups(backups_root)
        if not entries:
            print("No backups found.")
            return
        for entry in entries:
            print(
                f"{entry['name']}  version={entry['version']}  "
                f"label={entry['label']}  files={entry['file_count']}"
            )
    elif args.action == "restore":
        if not args.target:
            raise ReleaseError("Specify a backup name from `bud backup list` or a path.")
        candidate = Path(args.target).expanduser()
        target = candidate if candidate.is_absolute() else backups_root / candidate
        restored = rollback_release(bud_home, backup_dir=target, manage_service=False)
        print(f"Restored {restored} into {data_dir}. Restart Bud service if running.")


def handle_command(args: argparse.Namespace) -> bool:
    """Execute release CLI commands if selected; return False otherwise."""
    if args.command == "move-runtime":
        from bud.release.runtime_move_client import move_runtime

        token = sys.stdin.readline(257).strip()
        if not token or len(token) > 256:
            raise ReleaseError("Enter the destination access token on stdin.")
        result = move_runtime(
            args.source, args.destination, token, workspace=args.workspace, cancel=args.cancel
        )
        print(json.dumps(result))
        return True
    if args.command == "install":
        _install(args)
        return True
    if args.command == "update":
        _update(args)
        return True
    if args.command == "rollback":
        _rollback(args)
        return True
    if args.command == "service":
        _service(args)
        return True
    if args.command == "backup":
        _backup(args)
        return True
    return False
