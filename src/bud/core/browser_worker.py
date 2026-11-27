"""Drive the browser step via Bud running on the paired computer."""

import base64
import fcntl
import hashlib
import os
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from bud.core.handoff import WORKER_PATH

_LOCK_DIR = Path("/tmp")


def browser_step(client: httpx.Client, browser: httpx.Client, work: dict) -> bool:
    # Another process on this desktop may see "sending" while the submit is still running.
    port = browser.base_url.port or (443 if browser.base_url.scheme == "https" else 80)
    path = _LOCK_DIR / f"bud-browser-worker-{os.getuid()}-{port}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise ValueError("The browser worker lock must be a file owned by you.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return _browser_step(client, browser, work)
    finally:
        os.close(fd)


def _browser_step(client: httpx.Client, browser: httpx.Client, work: dict) -> bool:
    step, phase = work["step_id"], work["phase"]

    def publish(target: str) -> bool:
        response = client.put(
            WORKER_PATH, json={"step_id": step, "phase": target, "platform": sys.platform}
        )
        if response.status_code == 409:
            return False
        try:
            result = response.raise_for_status().json()
        except (ValueError, RecursionError):
            return False
        return isinstance(result, dict) and result.get("changed") is True

    def local(action: str, **kwargs) -> str:
        response = browser.post(
            "/browser/desktop-delivery",
            json={"step": step, "form": work["form"], "action": action, **kwargs},
        )
        # A local Bud without Browse can be started or paired later.
        if response.status_code in (404, 409, 423, 502, 503):
            return "unavailable"
        try:
            state = response.raise_for_status().json()["state"]
        except (ValueError, TypeError, KeyError, RecursionError):
            return "unavailable"
        return state if isinstance(state, str) else "unavailable"

    def record_result(result: str) -> bool:
        if result not in ("delivered", "uncertain"):
            return False
        changed = publish(result)
        return changed and result == "delivered"

    if phase in ("queued", "sign_in"):
        prepared = local("prepare")
        if prepared in ("ready", "sign_in"):
            publish(prepared)
        return False
    if phase == "sending":
        return record_result(local("check"))

    if phase != "approved" or not publish("sending"):
        return False
    # This authorization is consumed before touching the form. A disconnect after
    # this point leaves a read-only recovery step, even if the browser did nothing.
    file = client.get(f"{WORKER_PATH}/{step}/file")
    if file.status_code == 409:
        publish("uncertain")
        return False
    file.raise_for_status()
    if hashlib.sha256(file.content).hexdigest() != work["sha256"]:
        publish("uncertain")
        return False
    result = local(
        "submit",
        file=work["file"],
        sha256=work["sha256"],
        data=base64.b64encode(file.content).decode("ascii"),
    )
    return record_result(result)


def local_browser(url: str, token: str) -> httpx.Client:
    endpoint = urlsplit(url)
    if (
        endpoint.scheme not in {"http", "https"}
        or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("The browser service must be a local Bud URL.")
    return httpx.Client(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
        timeout=110,
        trust_env=False,
    )
