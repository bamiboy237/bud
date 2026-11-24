"""GitHub/local release resolution, SHA-256 verification, atomic swaps, and rollback."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tomllib
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from bud import __version__
from bud.config import HOST, PORT, Settings
from bud.core.auth import owner_token
from bud.core.events import Event
from bud.core.projection import runs
from bud.release import UpdateError, UpdateRollbackError
from bud.release.backup import backup_data_dir, restore_backup
from bud.release.service import CommandRunner, RuntimeFetcher, ServiceManager

DEFAULT_GITHUB_REPO = "bamiboy237/bud"
DEFAULT_SIGN_IDENTITY = "bud companion (local)"
DEFAULT_APP_DIR = Path("/Applications")
_EXECUTING_STATUSES = frozenset({"queued", "running", "stopping"})
_SHA256_LINE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-.](.+))?$")

HttpGetter = Callable[[str], bytes]


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    tarball_url: str
    checksums_url: str | None
    html_url: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class UpdateResult:
    previous_version: str | None
    new_version: str
    release_dir: Path
    backup_dir: Path
    app_updated: bool
    service_restarted: bool


def _default_runner(command: list[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _default_http_get(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"bud/{__version__}",
        },
    )
    with urlopen(request, timeout=30) as response:
        return response.read()


def _machine_arch() -> str:
    raw = platform.machine().lower()
    if raw in {"arm64", "aarch64"}:
        return "arm64"
    if raw in {"x86_64", "amd64"}:
        return "x86_64"
    return raw


def _find_uv() -> str:
    candidates = [
        shutil.which("uv"),
        str(Path.home() / ".local" / "bin" / "uv"),
        str(Path.home() / ".cargo" / "bin" / "uv"),
    ]
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise UpdateError("`uv` is required to install Bud releases. Install uv first.")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65_536):
            digest.update(chunk)
    return digest.hexdigest()


def parse_version(raw: str) -> tuple[tuple[int, ...], int, str]:
    """Parse ``vX.Y.Z`` or ``X.Y.Z-rc1`` into a sortable comparable tuple."""
    match = _VERSION_RE.match(raw.strip())
    if not match:
        raise UpdateError(f"Unsupported version format: {raw!r}.")
    numbers = tuple(int(part) for part in match.group(1).split("."))
    prerelease = match.group(2) or ""
    return numbers, 0 if prerelease else 1, prerelease


def normalize_version(raw: str) -> str:
    return raw.strip().lstrip("v")


def is_newer_version(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def current_release_version(bud_home: Path) -> str | None:
    manifest = bud_home.expanduser() / "releases" / "current" / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    version = data.get("version")
    return str(version) if isinstance(version, str) and version else None


def fetch_github_release(
    repo: str = DEFAULT_GITHUB_REPO,
    *,
    tag: str | None = None,
    arch: str | None = None,
    http_get: HttpGetter | None = None,
) -> ReleaseInfo | None:
    """Fetch metadata for the latest (or specific tag) GitHub release."""
    getter = http_get or _default_http_get
    target_arch = arch or _machine_arch()
    endpoint = (
        f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
        if tag
        else f"https://api.github.com/repos/{repo}/releases/latest"
    )
    try:
        raw = getter(endpoint)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise UpdateError(f"Failed to query GitHub Releases ({exc.code}): {exc.reason}") from exc
    except Exception as exc:
        raise UpdateError(f"Failed to query GitHub Releases: {exc}") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Invalid GitHub Release response: {exc}") from exc

    tag_name = str(payload.get("tag_name") or "").strip()
    if not tag_name:
        return None
    assets = payload.get("assets") or []
    tarball_url: str | None = None
    checksums_url: str | None = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if not url:
            continue
        if name == "SHA256SUMS.txt":
            checksums_url = url
        elif name.endswith(".tar.gz") and f"darwin-{target_arch}" in name:
            tarball_url = url
    if tarball_url is None:
        raise UpdateError(
            f"Release {tag_name} does not contain a macOS {target_arch} archive asset."
        )
    return ReleaseInfo(
        version=normalize_version(tag_name),
        tag=tag_name,
        tarball_url=tarball_url,
        checksums_url=checksums_url,
        html_url=payload.get("html_url"),
        published_at=payload.get("published_at"),
    )


def verify_sha256_sums(archive_path: Path, sums_text: str) -> str:
    """Verify ``archive_path`` against ``SHA256SUMS.txt`` contents."""
    expected: str | None = None
    target_name = archive_path.name
    for line in sums_text.splitlines():
        match = _SHA256_LINE.match(line)
        if not match:
            continue
        digest, fname = match.groups()
        if Path(fname).name == target_name:
            expected = digest.lower()
            break
    if expected is None:
        raise UpdateError(f"No checksum entry found for {target_name} in SHA256SUMS.txt.")
    actual = _sha256_file(archive_path).lower()
    if not secrets.compare_digest(actual, expected):
        raise UpdateError(
            f"Checksum mismatch for {target_name}: expected {expected}, got {actual}."
        )
    return actual


def safe_extract_tar(archive_path: Path, dest_dir: Path) -> Path:
    """Safely extract a release tarball, refusing traversal, links, and special files."""
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest_resolved = dest_dir.resolve()
    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            member_path = Path(member.name)
            if (
                not member.name
                or member_path.is_absolute()
                or ".." in member_path.parts
                or member.issym()
                or member.islnk()
                or not (member.isdir() or member.isreg())
            ):
                raise UpdateError(f"Unsafe member in release archive: {member.name!r}.")
            resolved = (dest_resolved / member_path).resolve()
            if resolved != dest_resolved and not resolved.is_relative_to(dest_resolved):
                raise UpdateError(f"Archive member escapes target folder: {member.name!r}.")
        for member in members:
            target = dest_dir / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise UpdateError(f"Could not read archive entry: {member.name!r}.")
            with extracted, target.open("wb") as out:
                shutil.copyfileobj(extracted, out)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)

    if (dest_dir / "manifest.json").is_file():
        return dest_dir
    top_dirs = [
        entry
        for entry in dest_dir.iterdir()
        if entry.is_dir() and (entry / "manifest.json").is_file()
    ]
    if len(top_dirs) == 1:
        return top_dirs[0]
    raise UpdateError("Extracted release archive is missing manifest.json.")


def download_and_verify_release(
    release: ReleaseInfo,
    dest_dir: Path,
    *,
    http_get: HttpGetter | None = None,
) -> Path:
    """Download release archive + ``SHA256SUMS.txt``, verify digest, and extract safely."""
    if not release.checksums_url:
        raise UpdateError(f"Release {release.tag} has no SHA256SUMS.txt checksum asset.")
    getter = http_get or _default_http_get
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive_name = Path(release.tarball_url.split("?")[0]).name or f"bud-{release.version}.tar.gz"
    archive_path = dest_dir / archive_name
    sums_text = getter(release.checksums_url).decode("utf-8")
    archive_path.write_bytes(getter(release.tarball_url))
    verify_sha256_sums(archive_path, sums_text)
    return safe_extract_tar(archive_path, dest_dir / "bundle")


def _repo_git_sha(repo_dir: Path) -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def build_local_release(
    repo_dir: Path,
    staging_dir: Path,
    *,
    build_app: bool = True,
    build_tui: bool = False,
    app_path: Path | None = None,
) -> Path:
    """Build a release bundle directly from a local git checkout."""
    repo = repo_dir.expanduser().resolve()
    pyproject_path = repo / "pyproject.toml"
    if not pyproject_path.is_file():
        raise UpdateError(f"Not a Bud checkout: missing {pyproject_path}.")
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    version = str(pyproject["project"]["version"])

    staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    wheel_dir = staging_dir / "wheel"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    uv = _find_uv()

    for cmd in (
        [uv, "build", "--wheel", "--out-dir", str(wheel_dir)],
        [
            uv,
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "-q",
            "-o",
            str(wheel_dir / "constraints.txt"),
        ],
    ):
        code, _, stderr = _default_runner(cmd, cwd=repo)
        if code != 0:
            raise UpdateError(f"Command failed ({' '.join(cmd)}): {stderr.strip()}")

    if app_path is not None:
        if not app_path.is_dir():
            raise UpdateError(f"App bundle not found at {app_path}.")
        shutil.copytree(app_path, staging_dir / "Bud.app", symlinks=True)
    elif build_app and sys.platform == "darwin":
        build_script = repo / "mac" / "build.sh"
        code, _, stderr = _default_runner([str(build_script)], cwd=repo)
        if code != 0:
            raise UpdateError(f"./mac/build.sh failed: {stderr.strip()}")
        built_app = repo / "mac" / "build" / "bud.app"
        if not built_app.is_dir():
            raise UpdateError(f"Expected built app at {built_app}.")
        shutil.copytree(built_app, staging_dir / "Bud.app", symlinks=True)

    tui_bin = repo / "tui" / "target" / "release" / "bud-tui"
    if build_tui and (repo / "tui" / "Cargo.toml").is_file():
        cargo = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / "cargo")
        if os.access(cargo, os.X_OK):
            code, _, stderr = _default_runner(
                [cargo, "build", "--release", "--locked"], cwd=repo / "tui"
            )
            if code != 0:
                raise UpdateError(f"Rust TUI build failed: {stderr.strip()}")
    if tui_bin.is_file():
        bin_dir = staging_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tui_bin, bin_dir / "bud-tui")
        (bin_dir / "bud-tui").chmod(0o755)

    manifest = {
        "version": version,
        "git_sha": _repo_git_sha(repo),
        "arch": _machine_arch(),
        "built_at": datetime.now(tz=UTC).isoformat(),
    }
    (staging_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return staging_dir


def _sign_app_if_needed(
    app_path: Path,
    *,
    sign_identity: str = DEFAULT_SIGN_IDENTITY,
    runner: CommandRunner | None = None,
) -> None:
    if sys.platform != "darwin" or not app_path.is_dir():
        return
    run = runner or (lambda cmd: _default_runner(cmd))
    if shutil.which("xattr"):
        run(["xattr", "-cr", str(app_path)])

    _, _, sign_info = run(["codesign", "-dvv", str(app_path)])
    if "Signature=adhoc" not in sign_info and "code object is not signed" not in sign_info:
        return

    _, ident_out, _ = run(["security", "find-identity", "-p", "codesigning"])
    chosen = sign_identity if f'"{sign_identity}"' in ident_out else "-"
    helper = app_path / "Contents" / "Helpers" / "browse"
    if helper.is_file():
        code, _, stderr = run(["codesign", "--force", "--sign", chosen, str(helper)])
        if code != 0:
            raise UpdateError(f"Failed to sign bundled browse helper: {stderr.strip()}")
    code, _, stderr = run(["codesign", "--force", "--sign", chosen, str(app_path)])
    if code != 0:
        raise UpdateError(f"Failed to sign Bud.app: {stderr.strip()}")


def install_release_dir(
    bundle_dir: Path,
    bud_home: Path,
    *,
    sign_identity: str = DEFAULT_SIGN_IDENTITY,
    install_deps: bool = True,
    runner: CommandRunner | None = None,
) -> tuple[str, Path]:
    """Install a staged/extracted bundle into an isolated folder under ``bud_home/releases``."""
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        raise UpdateError(f"Missing manifest.json in {bundle_dir}.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = str(manifest.get("version") or "").strip()
    if not version:
        raise UpdateError("Release manifest is missing version.")
    parse_version(version)

    wheel_dir = bundle_dir / "wheel"
    wheels = sorted(wheel_dir.glob("bud-*.whl")) if wheel_dir.is_dir() else []
    if install_deps and not wheels:
        raise UpdateError(f"No bud wheel found under {wheel_dir}.")

    releases_root = bud_home.expanduser() / "releases"
    releases_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    final_dir = releases_root / f"v{version}-{stamp}-{uuid4().hex[:6]}"
    staging_dir = releases_root / f".install-{uuid4().hex}"
    staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    run = runner or (lambda cmd: _default_runner(cmd))
    try:
        shutil.copy2(manifest_path, staging_dir / "manifest.json")
        bin_dir = staging_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
        if install_deps:
            uv = _find_uv()
            venv_dir = staging_dir / "venv"
            code, _, stderr = run([uv, "venv", "--python", sys.executable, str(venv_dir)])
            if code != 0:
                raise UpdateError(f"Failed to create release venv: {stderr.strip()}")
            constraints = wheel_dir / "constraints.txt"
            pip_cmd = [uv, "pip", "install", "--python", str(venv_dir / "bin" / "python")]
            if constraints.is_file():
                pip_cmd.extend(["--constraint", str(constraints)])
            pip_cmd.append(str(wheels[0]))
            code, _, stderr = run(pip_cmd)
            if code != 0:
                raise UpdateError(f"Failed to install bud wheel into venv: {stderr.strip()}")
            os.symlink(Path("..") / "venv" / "bin" / "bud", bin_dir / "bud")

        bundle_tui = bundle_dir / "bin" / "bud-tui"
        if bundle_tui.is_file():
            shutil.copy2(bundle_tui, bin_dir / "bud-tui")
            (bin_dir / "bud-tui").chmod(0o755)

        bundle_app = bundle_dir / "Bud.app"
        if bundle_app.is_dir():
            installed_app = staging_dir / "Bud.app"
            shutil.copytree(bundle_app, installed_app, symlinks=True)
            _sign_app_if_needed(installed_app, sign_identity=sign_identity, runner=runner)

        os.replace(staging_dir, final_dir)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    return version, final_dir


def _atomic_symlink(link_path: Path, target: Path | str) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    temp_link = link_path.with_name(f".{link_path.name}-{uuid4().hex}")
    try:
        os.symlink(target, temp_link)
        os.replace(temp_link, link_path)
    finally:
        if temp_link.is_symlink() or temp_link.exists():
            temp_link.unlink(missing_ok=True)


def _move_path(src: Path, dst: Path) -> None:
    try:
        os.replace(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        elif dst.exists() or dst.is_symlink():
            dst.unlink()
        shutil.move(str(src), str(dst))


def _is_app_running(target_app: Path) -> bool:
    if sys.platform != "darwin":
        return False
    executable = target_app / "Contents" / "MacOS" / "bud"
    proc = subprocess.run(
        ["pgrep", "-f", str(executable)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def _quit_app(target_app: Path) -> None:
    if sys.platform != "darwin":
        return
    executable = target_app / "Contents" / "MacOS" / "bud"
    subprocess.run(["pkill", "-TERM", "-f", str(executable)], check=False)


def _reopen_app(target_app: Path) -> None:
    if sys.platform == "darwin" and target_app.is_dir():
        subprocess.run(["open", str(target_app)], check=False)


def _active_runs_present(data_dir: Path) -> bool:
    if not data_dir.is_dir():
        return False
    candidates = [data_dir / "bud.db", *(data_dir / "composio" / "sessions").glob("*/bud.db")]
    for db_path in candidates:
        if not db_path.is_file():
            continue
        try:
            with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)) as db:
                rows = db.execute(
                    "SELECT id, session_id, seq, type, data, created_at "
                    "FROM events ORDER BY session_id, seq"
                ).fetchall()
        except sqlite3.DatabaseError:
            continue
        by_session: dict[str, list[Event]] = {}
        for event_id, session_id, seq, kind, raw_data, created_at in rows:
            event = Event(
                id=event_id,
                session_id=session_id,
                seq=seq,
                type=kind,
                data=json.loads(raw_data),
                created_at=datetime.fromisoformat(created_at),
            )
            by_session.setdefault(session_id, []).append(event)
        for session_events in by_session.values():
            states = runs(session_events)
            if states and states[-1].status in _EXECUTING_STATUSES:
                return True
    return False


def _swap_app_bundle(new_app: Path, target_app: Path, stash_app: Path) -> bool:
    if not new_app.is_dir():
        return False
    target_app.parent.mkdir(parents=True, exist_ok=True)
    staged = target_app.parent / f".Bud.app.staging-{uuid4().hex[:6]}"
    try:
        shutil.copytree(new_app, staged, symlinks=True)
        if target_app.exists():
            _move_path(target_app, stash_app)
        _move_path(staged, target_app)
        return True
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


def _restore_app_bundle(target_app: Path, stash_app: Path) -> None:
    if not stash_app.exists():
        return
    if target_app.exists():
        shutil.rmtree(target_app, ignore_errors=True)
    _move_path(stash_app, target_app)


def _prune_old_releases(
    bud_home: Path,
    *,
    preserve: set[Path],
    keep: int = 3,
) -> None:
    releases_root = bud_home / "releases"
    if not releases_root.is_dir():
        return
    candidates = sorted(
        (
            entry
            for entry in releases_root.iterdir()
            if entry.is_dir()
            and not entry.is_symlink()
            and not entry.name.startswith(".")
            and (entry / "manifest.json").is_file()
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    keep_resolved = {p.resolve() for p in preserve}
    kept = 0
    for candidate in candidates:
        if candidate.resolve() in keep_resolved:
            continue
        if kept < keep:
            kept += 1
            continue
        shutil.rmtree(candidate, ignore_errors=True)


def _healthcheck_credentials(bud_home: Path, *, port: int) -> tuple[str, str]:
    env_file = bud_home / "config.env"
    settings = (
        Settings(_env_file=env_file, data_dir=bud_home / "data")
        if env_file.is_file()
        else Settings(_env_file=None, data_dir=bud_home / "data")
    )
    token = owner_token(settings).get_secret_value()
    return f"http://{HOST}:{port}", token


def apply_update(
    new_version: str,
    new_release_dir: Path,
    bud_home: Path,
    *,
    app_install_dir: Path = DEFAULT_APP_DIR,
    install_app: bool = True,
    manage_service: bool = True,
    port: int = PORT,
    force: bool = False,
    service: ServiceManager | None = None,
    fetcher: RuntimeFetcher | None = None,
    health_timeout: float = 20.0,
) -> UpdateResult:
    """Atomically activate ``new_release_dir`` with pre-update backup and auto-rollback."""
    home = bud_home.expanduser()
    data_dir = home / "data"
    if not force and _active_runs_present(data_dir):
        raise UpdateError(
            "Bud has an active run in progress. Wait for it to finish or re-run with --force."
        )

    svc = service or ServiceManager(home)
    previous_version = current_release_version(home)
    current_link = home / "releases" / "current"
    previous_release = current_link.resolve() if current_link.exists() else None
    target_app = app_install_dir.expanduser() / "Bud.app"
    app_was_running = _is_app_running(target_app)
    stash_app = home / "releases" / f".prev-Bud.app-{uuid4().hex[:6]}"
    app_swapped = False

    if manage_service:
        svc.stop(timeout=15.0)
    if app_was_running:
        _quit_app(target_app)

    backup_dir = backup_data_dir(
        data_dir,
        home / "backups",
        label=f"pre-{new_version}",
        app_version=previous_version or __version__,
    )

    try:
        _atomic_symlink(current_link, new_release_dir)
        bud_bin = current_link / "bin" / "bud"
        if bud_bin.exists() or bud_bin.is_symlink():
            _atomic_symlink(
                home / "bin" / "bud",
                Path("..") / "releases" / "current" / "bin" / "bud",
            )
        tui_bin = current_link / "bin" / "bud-tui"
        if tui_bin.exists():
            _atomic_symlink(
                home / "bin" / "bud-tui", Path("..") / "releases" / "current" / "bin" / "bud-tui"
            )

        if install_app:
            app_swapped = _swap_app_bundle(new_release_dir / "Bud.app", target_app, stash_app)

        if manage_service:
            svc.install(bud_bin, port=port, start=True)
            url, token = _healthcheck_credentials(home, port=port)
            svc.wait_healthy(
                url,
                token,
                expected_version=new_version,
                timeout=health_timeout,
                fetcher=fetcher,
            )
    except Exception as exc:
        rollback_ok = False
        with suppress(Exception):
            if manage_service:
                svc.stop(timeout=15.0)
        with suppress(Exception):
            if previous_release is not None:
                _atomic_symlink(current_link, previous_release)
            elif current_link.is_symlink():
                current_link.unlink(missing_ok=True)
        if app_swapped:
            with suppress(Exception):
                _restore_app_bundle(target_app, stash_app)
        try:
            restore_backup(backup_dir, data_dir)
            if manage_service and previous_release is not None:
                svc.install(current_link / "bin" / "bud", port=port, start=True)
                url, token = _healthcheck_credentials(home, port=port)
                svc.wait_healthy(
                    url,
                    token,
                    expected_version=previous_version,
                    timeout=health_timeout,
                    fetcher=fetcher,
                )
            rollback_ok = True
        except Exception:
            rollback_ok = False
        if app_was_running:
            _reopen_app(target_app)
        raise UpdateRollbackError(
            f"Update to v{new_version} failed ({exc}); rollback "
            f"{'succeeded' if rollback_ok else 'failed'}.",
            rollback_ok=rollback_ok,
        ) from exc

    if stash_app.exists():
        shutil.rmtree(stash_app, ignore_errors=True)
    preserve = {new_release_dir}
    if previous_release is not None:
        preserve.add(previous_release)
    _prune_old_releases(home, preserve=preserve)

    if app_was_running:
        _reopen_app(target_app)

    return UpdateResult(
        previous_version=previous_version,
        new_version=new_version,
        release_dir=new_release_dir,
        backup_dir=backup_dir,
        app_updated=app_swapped,
        service_restarted=manage_service,
    )


def rollback_release(
    bud_home: Path,
    *,
    backup_dir: Path | None = None,
    target_release: Path | None = None,
    manage_service: bool = True,
    port: int = PORT,
    service: ServiceManager | None = None,
    fetcher: RuntimeFetcher | None = None,
    health_timeout: float = 20.0,
) -> Path:
    """Restore ``data_dir`` from the latest (or named) backup and optionally switch release."""
    home = bud_home.expanduser()
    svc = service or ServiceManager(home)
    backups_root = home / "backups"
    if backup_dir is None:
        candidates = sorted(
            (
                entry
                for entry in backups_root.iterdir()
                if entry.is_dir()
                and not entry.name.startswith(".")
                and (entry / "manifest.json").is_file()
            ),
            key=lambda p: p.name,
            reverse=True,
        )
        if not candidates:
            raise UpdateError(f"No verified backups found in {backups_root}.")
        chosen_backup = candidates[0]
    else:
        chosen_backup = backup_dir.expanduser()

    if manage_service:
        svc.stop(timeout=15.0)
    restore_backup(chosen_backup, home / "data")
    if target_release is not None:
        _atomic_symlink(home / "releases" / "current", target_release)
    if manage_service:
        bud_bin = home / "releases" / "current" / "bin" / "bud"
        if bud_bin.exists():
            svc.install(bud_bin, port=port, start=True)
            url, token = _healthcheck_credentials(home, port=port)
            svc.wait_healthy(url, token, timeout=health_timeout, fetcher=fetcher)
    return chosen_backup
