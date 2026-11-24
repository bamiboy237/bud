"""macOS LaunchAgent lifecycle management and server health verification."""

from __future__ import annotations

import fcntl
import os
import plistlib
import re
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

from bud.config import HOST, PORT
from bud.release import ServiceError

LAUNCH_AGENT_LABEL = "app.bud.server"
CommandRunner = Callable[[list[str]], tuple[int, str, str]]
RuntimeFetcher = Callable[[str, str], dict[str, Any]]


def default_plist_path() -> Path:
    """Standard per-user LaunchAgent path for Bud's daily-driver server."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _default_runner(command: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _default_runtime_fetcher(url: str, token: str) -> dict[str, Any]:
    with httpx.Client(timeout=3.0) as client:
        response = client.get(
            f"{url.rstrip('/')}/runtime",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Unexpected /runtime payload.")
        return payload


def server_lock_free(data_dir: Path) -> bool:
    """Return True when ``live-server.lock`` in ``data_dir`` is not held."""
    lock_path = data_dir / "live-server.lock"
    if not lock_path.exists():
        return True
    try:
        with lock_path.open("r+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    except OSError:
        return True


def wait_for_lock_release(data_dir: Path, *, timeout: float = 15.0) -> None:
    """Block until the running Bud server releases ``live-server.lock``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_lock_free(data_dir):
            return
        time.sleep(0.2)
    raise ServiceError(
        f"Bud server did not release {data_dir / 'live-server.lock'} within {timeout:.1f}s."
    )


def render_launchd_plist(
    bud_bin: Path,
    bud_home: Path,
    *,
    host: str = HOST,
    port: int = PORT,
) -> bytes:
    """Build the XML plist for ``app.bud.server``."""
    home_resolved = str(bud_home.expanduser())
    logs_dir = bud_home.expanduser() / "logs"
    payload: dict[str, Any] = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(bud_bin),
            "serve",
            "--host",
            host,
            "--port",
            str(port),
            "--home",
            home_resolved,
        ],
        "WorkingDirectory": home_resolved,
        "EnvironmentVariables": {
            "BUD_HOME": home_resolved,
            "DATA_DIR": str(bud_home.expanduser() / "data"),
            "WORKSPACE_DIR": str(bud_home.expanduser() / "workspace"),
            "PATH": (
                f"{bud_home.expanduser() / 'bin'}:/opt/homebrew/bin:/usr/local/bin:"
                "/usr/bin:/bin:/usr/sbin:/sbin"
            ),
            "PYDANTIC_AI_NO_BANNER": "1",
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "StandardOutPath": str(logs_dir / "server.stdout.log"),
        "StandardErrorPath": str(logs_dir / "server.stderr.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


class ServiceManager:
    """Install, stop, start, inspect, and health-check ``app.bud.server``."""

    def __init__(
        self,
        bud_home: Path,
        *,
        plist_path: Path | None = None,
        runner: CommandRunner | None = None,
        uid: int | None = None,
    ) -> None:
        self.bud_home = bud_home.expanduser()
        self.plist_path = (plist_path or default_plist_path()).expanduser()
        self._runner = runner or _default_runner
        self.uid = uid if uid is not None else (os.getuid() if hasattr(os, "getuid") else 0)

    @property
    def domain_target(self) -> str:
        return f"gui/{self.uid}"

    @property
    def service_target(self) -> str:
        return f"{self.domain_target}/{LAUNCH_AGENT_LABEL}"

    @property
    def data_dir(self) -> Path:
        return self.bud_home / "data"

    def install(
        self,
        bud_bin: Path,
        *,
        host: str = HOST,
        port: int = PORT,
        start: bool = True,
    ) -> Path:
        """Write/replace the LaunchAgent plist cleanly and optionally start the service."""
        (self.bud_home / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stop(timeout=15.0)
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_bytes = render_launchd_plist(bud_bin, self.bud_home, host=host, port=port)
        temp_path = self.plist_path.with_name(f".{self.plist_path.name}.tmp")
        try:
            temp_path.write_bytes(plist_bytes)
            temp_path.chmod(0o644)
            os.replace(temp_path, self.plist_path)
        finally:
            temp_path.unlink(missing_ok=True)
        if start:
            self.start()
        return self.plist_path

    def start(self) -> None:
        """Bootstrap and start the installed LaunchAgent."""
        if not self.plist_path.is_file():
            raise ServiceError(
                f"LaunchAgent plist missing at {self.plist_path}. Run `bud install` first."
            )
        code, _, stderr = self._runner(
            ["launchctl", "bootstrap", self.domain_target, str(self.plist_path)]
        )
        if code != 0:
            # If already bootstrapped (e.g. partial state), fall back to kickstart.
            kick_code, _, kick_err = self._runner(
                ["launchctl", "kickstart", "-k", self.service_target]
            )
            if kick_code != 0:
                detail = kick_err.strip() or stderr.strip() or "unknown launchctl error"
                raise ServiceError(f"Failed to start {LAUNCH_AGENT_LABEL}: {detail}")

    def stop(self, *, timeout: float = 15.0) -> None:
        """Stop the LaunchAgent if loaded and wait until the SQLite server lock clears."""
        code, _, stderr = self._runner(["launchctl", "bootout", self.service_target])
        if code != 0:
            lowered = stderr.lower()
            not_loaded = any(
                marker in lowered
                for marker in (
                    "could not find service",
                    "no such process",
                    "input/output error",
                )
            )
            if not not_loaded and self.plist_path.is_file():
                # Try plist-path syntax for older launchctl versions before giving up.
                fallback_code, _, fallback_err = self._runner(
                    ["launchctl", "bootout", self.domain_target, str(self.plist_path)]
                )
                if fallback_code != 0 and server_lock_free(self.data_dir) is False:
                    detail = fallback_err.strip() or stderr.strip()
                    raise ServiceError(f"Failed to stop {LAUNCH_AGENT_LABEL}: {detail}")
        wait_for_lock_release(self.data_dir, timeout=timeout)

    def restart(self, *, timeout: float = 15.0) -> None:
        self.stop(timeout=timeout)
        self.start()

    def uninstall(self, *, timeout: float = 15.0) -> None:
        self.stop(timeout=timeout)
        with suppress(OSError):
            self.plist_path.unlink(missing_ok=True)

    def status(self) -> dict[str, Any]:
        """Inspect plist installation, launchctl state, PID, and server lock."""
        plist_exists = self.plist_path.is_file()
        code, stdout, _ = self._runner(["launchctl", "print", self.service_target])
        loaded = code == 0
        pid: int | None = None
        if loaded:
            match = re.search(r"\bpid\s*=\s*(\d+)", stdout)
            if match:
                pid = int(match.group(1))
        return {
            "label": LAUNCH_AGENT_LABEL,
            "plist_path": str(self.plist_path),
            "installed": plist_exists,
            "loaded": loaded,
            "pid": pid,
            "lock_held": not server_lock_free(self.data_dir),
        }

    def wait_healthy(
        self,
        url: str,
        token: str,
        *,
        expected_version: str | None = None,
        timeout: float = 20.0,
        fetcher: RuntimeFetcher | None = None,
    ) -> dict[str, Any]:
        """Wait until ``/runtime`` answers cleanly with optional expected version."""
        poll = fetcher or _default_runtime_fetcher
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                payload = poll(url, token)
            except Exception as exc:
                last_error = exc
                time.sleep(0.4)
                continue
            if expected_version is not None and payload.get("version") != expected_version:
                raise ServiceError(
                    f"Server answered with version {payload.get('version')!r}, "
                    f"expected {expected_version!r}."
                )
            return payload
        detail = f": {last_error}" if last_error else ""
        raise ServiceError(
            f"Bud did not pass healthcheck at {url.rstrip('/')}/runtime within "
            f"{timeout:.1f}s{detail}."
        )
