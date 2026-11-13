"""Drive a simulated bud server the way an owner would, one command at a time.

A simulated person (a human or a model) uses these commands; every approval is an explicit
decision, and the report is read from bud's own event log and cost ledger, not from the
driver's memory. State (port, data directory, the primary session) lives in `sim.json`
inside the data directory, so each command is independent and a restart loses nothing.

    python -m bud.evals.sim start --data DIR [--port 8701]
    python -m bud.evals.sim day 2026-11-03T08:30:00 --data DIR
    python -m bud.evals.sim deliver --data DIR --sender a@x.example --subject S --body B
    python -m bud.evals.sim say "What's on today?" --data DIR [--session new|primary|ID]
    python -m bud.evals.sim approve APPROVAL_ID --data DIR      (or: deny, answer ID TEXT)
    python -m bud.evals.sim mode auto|strict --data DIR
    python -m bud.evals.sim report --data DIR
    python -m bud.evals.sim stop --data DIR

Never point this at the owner's live server or real accounts: `start` always runs
`create_app(simulate=True)` on its own port and data directory.
"""

import argparse
import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

from bud.core.projection import reply_text
from bud.world.store import World

LIVE_PORT = 8420


def _state(data: Path) -> dict:
    path = data / "sim.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _save(data: Path, state: dict) -> None:
    (data / "sim.json").write_text(json.dumps(state, indent=1))


def _client(data: Path) -> httpx.Client:
    state = _state(data)
    token = (data / "auth" / "owner.token").read_text().strip()
    return httpx.Client(
        base_url=f"http://127.0.0.1:{state['port']}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


def start(data: Path, port: int) -> None:
    if port == LIVE_PORT:
        sys.exit("Port 8420 is the owner's live server; pick another port.")
    data.mkdir(parents=True, exist_ok=True)
    # Owner-only, like Codex and Vibe's log files.
    log = os.fdopen(
        os.open(data / "server.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a"
    )
    env = {**os.environ, "DATA_DIR": str(data), "PYDANTIC_AI_NO_BANNER": "1"}
    code = (
        "import uvicorn; from bud.core.server import create_app; "
        f"uvicorn.run(create_app(simulate=True), host='127.0.0.1', port={port}, "
        "log_level='warning', timeout_graceful_shutdown=3)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code], env=env, stdout=log, stderr=log, start_new_session=True
    )
    _save(data, {**_state(data), "port": port, "pid": process.pid})
    for _ in range(120):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/runtime", timeout=2).status_code == 401:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    else:
        sys.exit(f"Server did not start; see {data / 'server.log'}")
    with _client(data) as client:
        primary = client.post("/sessions/primary").json()["session_id"]
    _save(data, {**_state(data), "primary": primary})
    print(f"started on port {port}; primary session {primary}")


def stop(data: Path) -> None:
    state = _state(data)
    pid = state.get("pid")
    if pid:
        # PermissionError: the id now belongs to another user's process, so ours is gone.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGTERM)
            # Return only once the server is gone: it holds bud.lock until shutdown ends, so an
            # immediate `start` would otherwise fail with "Another bud server is already using".
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                os.killpg(pid, 0)  # raises ProcessLookupError once the group has exited
                time.sleep(0.2)
            os.killpg(pid, signal.SIGKILL)
        # Forget the id: once it is free, the system can give it to an unrelated process,
        # and a second `stop` must not signal that one.
        _save(data, {key: value for key, value in state.items() if key != "pid"})
    print("stopped")


def _session(data: Path, client: httpx.Client, which: str) -> str:
    state = _state(data)
    if which == "primary":
        return state["primary"]
    if which == "new":
        session = client.post("/sessions", json={}).json()["session_id"]
        _save(data, {**state, "last": session})
        return session
    if which == "last":
        return state.get("last", state["primary"])
    return which


def _wait(data: Path, client: httpx.Client, session: str, after: int, limit: float = 600) -> dict:
    """Until a run that ended after `after` leaves the session idle or waiting on the owner.

    Right after a message or a decision the next run may not be queued yet, so an idle
    snapshot only counts once the log shows a run ending after `after`.
    """
    deadline = time.time() + limit
    snapshot: dict = {}
    while time.time() < deadline:
        time.sleep(1.5)
        snapshot = client.get(f"/sessions/{session}").json()
        with sqlite3.connect(data / "bud.db") as db:
            ended = db.execute(
                "SELECT 1 FROM events WHERE session_id = ? AND seq > ? AND type IN "
                "('run.completed', 'run.waiting', 'run.failed', 'run.cancelled') LIMIT 1",
                (session, after),
            ).fetchone()
        busy = snapshot["run_active"] or snapshot["queued_messages"]
        # A parked approval stays listed while later runs go on, so wait on the run itself.
        if ended and (snapshot["status"] == "awaiting_approval" or not busy):
            return snapshot
    return {**snapshot, "timed_out": True}


def _turn_summary(data: Path, session: str, after: int) -> dict:
    """What happened in this turn, read from bud's event log."""
    db = sqlite3.connect(data / "bud.db")
    rows = db.execute(
        "SELECT seq, type, data FROM events WHERE session_id = ? AND seq > ? ORDER BY seq",
        (session, after),
    ).fetchall()
    summary: dict = {"tools": [], "auto": [], "asked": [], "failed": None, "reply": None}
    for _seq, kind, raw in rows:
        event = json.loads(raw)
        if kind == "tool.call":
            summary["tools"].append(f"{event['tool']} {json.dumps(event['args'])[:200]}")
        elif kind == "action.decided" and event.get("rule") == "auto-allow":
            summary["auto"].append(event["tool"])
        elif kind == "approval.requested":
            summary["asked"].append(
                {"approval_id": event["approval_id"], "tool": event["tool"], "args": event["args"]}
            )
        elif kind == "run.failed":
            summary["failed"] = {k: event.get(k) for k in ("reason", "kind", "next")}
        elif kind == "run.completed" and not event.get("quiet"):
            # A quiet run closes a superseded proposal; surfaces hide it, and so does this.
            summary["reply"] = reply_text(event.get("messages", []))
    return summary


def say(data: Path, text: str, which: str) -> None:
    with _client(data) as client:
        session = _session(data, client, which)
        before = client.get(f"/sessions/{session}").json()["last_seq"]
        client.post(
            f"/sessions/{session}/messages", json={"text": text, "message_id": uuid4().hex}
        ).raise_for_status()
        snapshot = _wait(data, client, session, before)
    _report_turn(data, session, before, snapshot)


def decide(data: Path, approval_id: str, approved: bool | None, answer: str | None) -> None:
    with _client(data) as client:
        session = _find_session(client, data, approval_id)
        before = client.get(f"/sessions/{session}").json()["last_seq"]
        if answer is not None:
            path = f"/sessions/{session}/approvals/{approval_id}/answer"
            client.post(path, json={"text": answer}).raise_for_status()
        else:
            path = f"/sessions/{session}/approvals/{approval_id}"
            response = client.post(path, json={"approved": approved})
            response.raise_for_status()
            if not response.json()["applied"]:
                # Already decided or withdrawn: nothing will run, so there is no turn to wait for.
                print(json.dumps({"applied": False, "approval_id": approval_id}))
                return
        snapshot = _wait(data, client, session, before)
    _report_turn(data, session, before, snapshot)


def _find_session(client: httpx.Client, data: Path, approval_id: str) -> str:
    db = sqlite3.connect(data / "bud.db")
    row = db.execute(
        "SELECT session_id FROM events WHERE type = 'approval.requested' "
        "AND json_extract(data, '$.approval_id') = ?",
        (approval_id,),
    ).fetchone()
    if row is None:
        sys.exit(f"No pending approval {approval_id}")
    return row[0]


def _report_turn(data: Path, session: str, before: int, snapshot: dict) -> None:
    summary = _turn_summary(data, session, before)
    pending = [
        {"approval_id": p["approval_id"], "tool": p["tool"], "args": p["args"]}
        for p in snapshot.get("pending_approvals", [])
    ]
    print(
        json.dumps(
            {
                "session": session,
                "reply": summary["reply"],
                "tools": summary["tools"],
                "auto_ran": summary["auto"],
                "waiting_for_you": pending,
                "failed": summary["failed"],
                "timed_out": snapshot.get("timed_out", False),
            },
            indent=1,
        )
    )


def report(data: Path) -> None:
    db = sqlite3.connect(data / "bud.db")

    def count(sql: str) -> int:
        return db.execute(sql).fetchone()[0]

    totals = {
        "owner_messages": count("SELECT COUNT(*) FROM events WHERE type = 'user.message'"),
        "approvals_asked": count("SELECT COUNT(*) FROM events WHERE type = 'approval.requested'"),
        "approved": count("SELECT COUNT(*) FROM events WHERE type = 'approval.granted'"),
        "denied": count("SELECT COUNT(*) FROM events WHERE type = 'approval.denied'"),
        "auto_runs": count(
            "SELECT COUNT(*) FROM events WHERE type = 'action.decided' "
            "AND json_extract(data, '$.rule') = 'auto-allow'"
        ),
        "loop_detections": count("SELECT COUNT(*) FROM events WHERE type = 'run.loop_detected'"),
        "run_failures": dict(
            db.execute(
                "SELECT json_extract(data, '$.kind'), COUNT(*) FROM events "
                "WHERE type = 'run.failed' GROUP BY 1"
            ).fetchall()
        ),
        "memory_learned": count("SELECT COUNT(*) FROM events WHERE type = 'memory.learned'"),
    }
    # The gateway's cost ledger shares bud.db with the event log.
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "model_requests" in tables:
        cost = db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM model_requests"
        ).fetchone()
        totals["model_usd"] = round(cost[0], 4)
        totals["model_requests"] = cost[1]
    print(json.dumps(totals, indent=1))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bud.evals.sim")
    parser.add_argument("command")
    parser.add_argument("args", nargs="*")
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8701)
    parser.add_argument("--session", default="primary")
    parser.add_argument("--sender")
    parser.add_argument("--subject")
    parser.add_argument("--body")
    parser.add_argument("--thread")
    a = parser.parse_args(argv)
    data = a.data.resolve()
    match a.command:
        case "start":
            start(data, a.port)
        case "stop":
            stop(data)
        case "day":
            World(data / "world.db").set_now(a.args[0])
            print(f"clock set to {a.args[0]}")
        case "deliver":
            thread = World(data / "world.db").deliver(a.sender, a.subject, a.body, a.thread)
            print(f"delivered in thread {thread}")
        case "say":
            say(data, " ".join(a.args), a.session)
        case "approve" | "deny":
            decide(data, a.args[0], a.command == "approve", None)
        case "answer":
            decide(data, a.args[0], None, " ".join(a.args[1:]))
        case "mode":
            with _client(data) as client:
                print(client.post("/mode", json={"mode": a.args[0]}).json())
        case "report":
            report(data)
        case _:
            parser.error(f"unknown command {a.command}")


if __name__ == "__main__":
    main()
