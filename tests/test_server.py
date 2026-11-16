"""Authenticated HTTP tests against fresh Bud/DBOS processes and real workspace files."""

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from bud.limits import MAX_MESSAGE_CHARS
from tests.server_process import DRAFT, FILENAME, REVISED

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "offline-process-test-owner-" + "x" * 32


class ServerProcess:
    def __init__(self, root: Path, checkpoint: str | None = None):
        self.root = root.resolve()
        self.data = self.root / "data"
        self.checkpoint = checkpoint
        self.process = None
        self.client = None
        self.last_snapshot = None
        self.log_path = self.root / f"server-{time.monotonic_ns()}.log"

    def logs(self) -> str:
        return self.log_path.read_text(errors="replace")

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        listener = socket.socket()
        log = self.log_path.open("wb")
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(self.root),
                "DATA_DIR": str(self.data),
                "OWNER_TOKEN": TOKEN,
                "APPROVAL_REVIEW": "false",
                "LOOP_DETECTION": "false",
                "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src"))),
                "PYTHONUNBUFFERED": "1",
                "DBOS_DISABLE_OTLP": "true",
            }
            command = [
                sys.executable,
                str(ROOT / "tests" / "server_process.py"),
                "--fd",
                str(listener.fileno()),
            ]
            if self.checkpoint:
                command.extend(("--checkpoint", self.checkpoint))
            self.process = subprocess.Popen(
                command,
                cwd=self.root,
                env=env,
                pass_fds=(listener.fileno(),),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            client = self.client = httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Authorization": f"Bearer {TOKEN}"},
                timeout=1,
                trust_env=False,
            )
            self.wait(
                lambda: client.get("/runtime").status_code == 200,
                "authenticated server startup",
            )
            return self
        except BaseException:
            self.close()
            raise
        finally:
            listener.close()
            log.close()

    def wait(self, condition, description: str, timeout: float = 25):
        assert self.process is not None
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                pytest.fail(
                    f"Server exited ({self.process.returncode}): {description}\n{self.logs()}"
                )
            try:
                latest = condition()
                if latest:
                    return latest
            except (httpx.TransportError, sqlite3.OperationalError) as exc:
                latest = repr(exc)
            threading.Event().wait(0.025)
        pytest.fail(
            f"Timed out: {description}; last result: {latest!r}\n"
            f"Snapshot: {self.last_snapshot!r}\n{self.logs()}"
        )

    def request(self, method: str, path: str, status: int = 200, **kwargs):
        assert self.client is not None
        response = self.client.request(method, path, **kwargs)
        assert response.status_code == status, (response.text, self.logs())
        return response.json()

    def snapshot(self, session: str):
        self.last_snapshot = self.request("GET", f"/sessions/{session}")
        return self.last_snapshot

    def pending(self, session: str, content: str):
        def matching():
            state = self.snapshot(session)
            return next(
                (
                    approval
                    for approval in state["pending_approvals"]
                    if approval["args"].get("content") == content
                ),
                None,
            )

        return self.wait(matching, f"workspace approval for {content!r}")

    def settled(self, session: str):
        def complete():
            state = self.snapshot(session)
            if (
                state["tasks"]
                and all(task["status"] in ("completed", "failed") for task in state["tasks"])
                and all(
                    any(turn["task_id"] == task["task_id"] for turn in state["transcript"])
                    for task in state["tasks"]
                )
                and not state["run_active"]
                and not state["pending_approvals"]
            ):
                return state
            return None

        return self.wait(complete, "settled job and feed")

    def events(self, kind: str):
        with sqlite3.connect(self.data / "bud.db") as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT data FROM events WHERE type=? ORDER BY rowid", (kind,)
                )
            ]

    def claims(self):
        path = self.data / "workspace-dispatches.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def kill(self):
        if self.process is not None:
            self.process.kill()
            self.process.wait(timeout=10)

    def close(self):
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.kill()
        finally:
            if self.client is not None:
                self.client.close()


@contextmanager
def running(root: Path, checkpoint: str | None = None):
    process = ServerProcess(root, checkpoint)
    try:
        yield process.start()
    finally:
        process.close()


def start_report(server: ServerProcess) -> tuple[str, dict]:
    session = server.request("POST", "/sessions", status=201)["session_id"]
    server.request(
        "POST",
        f"/sessions/{session}/messages",
        status=202,
        json={"text": "Save report.md with the draft text.", "message_id": "owner-request"},
    )
    approval = server.pending(session, DRAFT)
    assert approval["tool"] == "workspace_create"
    assert approval["task_id"] == 1
    assert not (server.data / "workspace" / FILENAME).exists()
    assert server.claims() == []
    return session, approval


def decide(server: ServerProcess, session: str, approval: dict, approved: bool):
    return server.request(
        "POST",
        f"/sessions/{session}/approvals/{approval['approval_id']}",
        json={"approved": approved},
    )


def assert_claim(server: ServerProcess, approval: dict):
    claims = server.claims()
    assert len(claims) == 1
    assert claims[0]["claim"] == hashlib.sha256(approval["approval_id"].encode()).hexdigest()
    assert len(claims[0]["request"]) == 64
    assert [event["approval_id"] for event in server.events("approval.granted")] == [
        approval["approval_id"]
    ]


def test_api_authentication_and_validation(tmp_path):
    with running(tmp_path) as server:
        assert server.client.get("/runtime", headers={"Authorization": ""}).status_code == 401
        assert (
            server.client.get("/runtime", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        response = server.client.post(
            "/sessions/unknown/messages",
            headers={"Authorization": ""},
            content="not-json",
        )
        assert response.status_code == 401
        runtime = server.request("GET", "/runtime")
        assert runtime["mode"] == "simulate"
        assert runtime["messaging"] == "disabled_in_simulation"
        assert server.client.get("/runtime").headers["cache-control"] == "no-store"
        server.request("GET", "/sessions/missing", status=404)
        server.request("POST", "/sessions", status=422, json={"session_id": "../escape"})
        session_id = "a" * 32
        for _ in range(2):
            assert server.request(
                "POST", "/sessions", status=201, json={"session_id": session_id}
            ) == {"session_id": session_id}
        assert len(server.events("session.created")) == 1
        for body in (
            {"text": ""},
            {"text": "x" * (MAX_MESSAGE_CHARS + 1)},
            {"text": "hello", "message_id": "../bad"},
        ):
            server.request("POST", f"/sessions/{session_id}/messages", status=422, json=body)
        server.request(
            "POST",
            f"/sessions/{session_id}/approvals/missing",
            status=404,
            json={"approved": True},
        )
        server.request("POST", "/mode", status=400, json={"mode": "unknown"})
        server.request("GET", "/connections", status=409)
        body = {"text": "hello", "message_id": "same-message"}
        path = f"/sessions/{session_id}/messages"
        assert server.request("POST", path, status=202, json=body) == {"duplicate": False}
        assert server.request("POST", path, status=202, json=body) == {"duplicate": True}
        state = server.wait(
            lambda: (
                state
                if not (state := server.snapshot(session_id))["run_active"] and state["transcript"]
                else None
            ),
            "offline reply",
        )
        assert any(turn["text"] == "Offline reply." for turn in state["transcript"])
        assert len(server.events("user.message")) == 1


def test_denied_workspace_write_does_not_dispatch(tmp_path):
    with running(tmp_path) as server:
        session, approval = start_report(server)
        assert decide(server, session, approval, False) == {"applied": True}
        state = server.settled(session)
        assert state["tasks"][0]["outcome"] == "not_delivered"
        assert "No file was saved." in state["tasks"][0]["result"]
        assert not (server.data / "workspace" / FILENAME).exists()
        assert server.claims() == []
        assert decide(server, session, approval, True) == {"applied": False}
        assert [event["approval_id"] for event in server.events("approval.denied")] == [
            approval["approval_id"]
        ]


def test_always_allow_records_a_grant_and_revoke_restores_asking(tmp_path):
    with running(tmp_path) as server:
        session, approval = start_report(server)
        assert approval["grantable"] is True
        assert server.request(
            "POST",
            f"/sessions/{session}/approvals/{approval['approval_id']}",
            json={"approved": True, "remember": True},
        ) == {"applied": True}
        assert server.request("GET", "/grants") == {"tools": ["workspace_create"]}
        server.settled(session)
        assert server.request("DELETE", "/grants/workspace_create") == {"removed": True}
        assert server.request("GET", "/grants") == {"tools": []}


def test_a_grant_is_recorded_only_for_an_approved_decision(tmp_path):
    """A deny with "remember" must not leave a standing permission behind."""
    with running(tmp_path) as server:
        session, approval = start_report(server)
        assert server.request(
            "POST",
            f"/sessions/{session}/approvals/{approval['approval_id']}",
            json={"approved": False, "remember": True},
        ) == {"applied": True}
        assert server.request("GET", "/grants") == {"tools": []}
        assert server.settled(session)["tasks"][0]["outcome"] == "not_delivered"


def test_revised_workspace_approval_survives_process_restart(tmp_path):
    with running(tmp_path) as server:
        session, original = start_report(server)
        server.request(
            "POST",
            f"/sessions/{session}/approvals/{original['approval_id']}/revise",
            json={"text": "Save report.md with revised text.", "message_id": "owner-revision"},
        )
        revised = server.pending(session, REVISED)
        assert revised["approval_id"] != original["approval_id"]
        assert [event["approval_id"] for event in server.events("approval.requested")] == [
            original["approval_id"],
            revised["approval_id"],
        ]
        assert server.events("approval.denied")[0]["revision_message_id"] == "owner-revision"
        assert not (server.data / "workspace" / FILENAME).exists()
        server.kill()

    with running(tmp_path) as server:
        assert server.pending(session, REVISED)["approval_id"] == revised["approval_id"]
        assert decide(server, session, original, True) == {"applied": False}
        assert decide(server, session, revised, True) == {"applied": True}
        before = server.settled(session)
        assert (server.data / "workspace" / FILENAME).read_bytes() == REVISED.encode()
        assert before["tasks"][0]["artifacts"][0]["path"] == FILENAME
        assert before["tasks"][0]["outcome"] == "delivered"
        assert_claim(server, revised)
        server.kill()

    with running(tmp_path) as server:
        after = server.settled(session)
        assert after["tasks"] == before["tasks"]
        assert after["transcript"] == before["transcript"]
        target = server.data / "workspace" / FILENAME
        saved_stat = target.stat()
        assert decide(server, session, revised, True) == {"applied": False}
        assert target.read_bytes() == REVISED.encode()
        assert target.stat().st_ino == saved_stat.st_ino
        assert target.stat().st_mtime_ns == saved_stat.st_mtime_ns
        assert_claim(server, revised)


@pytest.mark.parametrize("checkpoint", ["before_dispatch", "after_claim", "after_write"])
def test_crash_at_workspace_dispatch_boundary(tmp_path, checkpoint):
    with running(tmp_path, checkpoint) as server:
        session, approval = start_report(server)
        assert decide(server, session, approval, True) == {"applied": True}
        marker = server.data / "checkpoint"
        server.wait(
            lambda: marker.exists() and marker.read_text() == checkpoint,
            f"crash checkpoint {checkpoint}",
        )
        assert len(server.claims()) == (0 if checkpoint == "before_dispatch" else 1)
        target = server.data / "workspace" / FILENAME
        assert target.exists() == (checkpoint == "after_write")
        if target.exists():
            assert target.read_bytes() == DRAFT.encode()
            saved_stat = target.stat()
        server.kill()

    with running(tmp_path) as server:
        state = server.settled(session)
        assert decide(server, session, approval, True) == {"applied": False}
        assert_claim(server, approval)
        target = server.data / "workspace" / FILENAME
        if checkpoint == "after_claim":
            assert not target.exists()
            assert state["tasks"][0]["outcome"] == "not_delivered"
            assert state["tasks"][0]["artifacts"] == []
        else:
            assert target.read_bytes() == DRAFT.encode()
            assert state["tasks"][0]["outcome"] == "delivered"
            assert state["tasks"][0]["artifacts"][0]["path"] == FILENAME
            if checkpoint == "after_write":
                assert target.stat().st_ino == saved_stat.st_ino
                assert target.stat().st_mtime_ns == saved_stat.st_mtime_ns
        assert any(event["run_id"] for event in server.events("run.recovered"))
