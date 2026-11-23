"""Outbound client for running checks on a paired desktop."""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import ExitStack, closing
from getpass import getpass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from bud.config import LOCAL_URL, get_settings
from bud.core.auth import owner_token
from bud.core.browser_delivery import BrowserForm
from bud.core.browser_worker import browser_step, local_browser
from bud.core.handoff import WORKER_PATH


def arguments(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("desktop", help="pair a desktop or run a cross-desktop file check")
    parser.add_argument("--url", default=LOCAL_URL)
    actions = parser.add_subparsers(dest="desktop_action", required=True)
    actions.add_parser("pair", help="issue a revocable desktop credential").add_argument("label")
    actions.add_parser("list", help="list paired desktops")
    actions.add_parser("primary", help="print the main chat ID")
    files = actions.add_parser("attachments", help="list attachments already sent in a chat")
    files.add_argument("session")
    actions.add_parser("revoke", help="revoke a desktop").add_argument("device")
    dispatch = actions.add_parser("dispatch", help="check one attachment on selected desktops")
    dispatch.add_argument("session")
    dispatch.add_argument("file_id")
    dispatch.add_argument("file_name")
    dispatch.add_argument("devices", nargs="+")
    dispatch.add_argument("--job-id", default=None, help="reuse this ID for an identical retry")
    dispatch.add_argument("--form", type=Path, help="JSON description of a selected browser form")
    decision = actions.add_parser("browser", help="approve, cancel, or verify a browser delivery")
    decision.add_argument("session")
    decision.add_argument("job_id")
    decision.add_argument("decision", choices=["approve", "cancel", "verify"])
    retarget = actions.add_parser("retarget", help="change the browser desktop before approval")
    retarget.add_argument("session")
    retarget.add_argument("job_id")
    retarget.add_argument("device")
    status = actions.add_parser("status", help="show progress in one conversation")
    status.add_argument("session")
    status.add_argument("job_id")
    run = actions.add_parser("run", help="start an outbound worker on this desktop")
    run.add_argument("--once", action="store_true", help="check for work once and exit")
    run.add_argument("--state", type=Path, help="private directory for this worker's receipts")
    run.add_argument(
        "--browser-local", help="loopback URL for this computer's own Browse-enabled Bud server"
    )


def connection(url: str, token: str) -> httpx.Client:
    endpoint = urlsplit(url)
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("Use a Bud server URL without credentials, a query, or a fragment.")
    if endpoint.scheme != "https" and (
        endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Desktop connections require HTTPS outside localhost.")
    return httpx.Client(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


def work_once(client: httpx.Client, state: Path, browser: httpx.Client | None = None) -> str | None:
    response = client.get(WORKER_PATH, params={"browser": browser is not None}).raise_for_status()
    work = response.json()
    if (step := work["step_id"]) is None:
        return None
    if browser is not None and work.get("phase"):
        return step if browser_step(client, browser, work) else None
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.stat().st_mode & 0o077 or state.stat().st_uid != os.getuid():
        raise ValueError("The desktop receipt directory must be private and owned by you.")
    with closing(sqlite3.connect(state / "receipts.db", timeout=45)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, sha256 TEXT)")
        with db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT sha256 FROM receipts WHERE id = ?", (step,)).fetchone()
            if previous:
                digest = previous[0]
            else:
                file = client.get(f"{WORKER_PATH}/{step}/file").raise_for_status()
                digest = hashlib.sha256(file.content).hexdigest()
                db.execute("INSERT INTO receipts VALUES (?, ?)", (step, digest))
    client.put(
        WORKER_PATH, json={"step_id": step, "sha256": digest, "platform": sys.platform}
    ).raise_for_status()
    return step


def main(args: argparse.Namespace) -> None:
    action = args.desktop_action
    settings = get_settings()
    if action == "run":
        token = os.environ.get("BUD_DESKTOP_TOKEN") or getpass("Desktop pairing token: ")
        with ExitStack() as stack:
            client = stack.enter_context(connection(args.url, token))
            browser = None
            if args.browser_local:
                browser = stack.enter_context(
                    local_browser(args.browser_local, owner_token(settings).get_secret_value())
                )
            state = args.state or (settings.data_dir / "desktop-worker")
            delay = 1
            while True:
                try:
                    step = work_once(client, state, browser)
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    transient = (
                        isinstance(exc, httpx.TransportError) or exc.response.status_code >= 500
                    )
                    if args.once or not transient:
                        raise
                    print("Bud server unavailable. Reconnecting.", file=sys.stderr, flush=True)
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                delay = 1
                if step:
                    print(f"Completed desktop step {step}.", flush=True)
                if args.once:
                    return
                if not step:
                    time.sleep(2)
    with connection(args.url, owner_token(settings).get_secret_value()) as client:
        match action:
            case "pair":
                response = client.post("/desktops", json={"label": args.label})
            case "list":
                response = client.get("/desktops")
            case "primary":
                result = client.post("/sessions/primary").raise_for_status().json()
                print(result["session_id"])
                return
            case "attachments":
                snapshot = client.get(f"/sessions/{args.session}").raise_for_status().json()
                print(
                    json.dumps(
                        [
                            file
                            for turn in snapshot["transcript"]
                            for file in turn.get("attachments", [])
                        ],
                        indent=2,
                    )
                )
                return
            case "revoke":
                client.delete(f"/desktops/{args.device}").raise_for_status()
                return
            case "dispatch":
                job_id = args.job_id or uuid4().hex
                print(f"Job ID {job_id}", file=sys.stderr, flush=True)
                form = (
                    BrowserForm.model_validate_json(args.form.read_bytes()) if args.form else None
                )
                response = client.post(
                    f"/sessions/{args.session}/desktop-jobs",
                    json={
                        "job_id": job_id,
                        "file": {"id": args.file_id, "name": args.file_name},
                        "desktops": args.devices,
                        **({"browser": form.model_dump()} if form else {}),
                    },
                )
            case "browser":
                response = client.post(
                    f"/sessions/{args.session}/desktop-jobs/{args.job_id}/browser/{args.decision}"
                )
            case "retarget":
                response = client.post(
                    f"/sessions/{args.session}/desktop-jobs/{args.job_id}/browser/retarget",
                    json={"desktop": args.device},
                )
            case "status":
                response = client.get(f"/sessions/{args.session}/desktop-jobs/{args.job_id}")
            case _:
                raise ValueError("Unknown desktop command.")
        print(json.dumps(response.raise_for_status().json(), indent=2))
