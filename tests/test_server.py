"""Authenticated HTTP tests against fresh Bud/DBOS processes and real workspace files."""

import hashlib
import json
import os
import signal
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

from bud.core.log import EventLog
from bud.core.projection import unreported_job_news
from bud.limits import MAX_MESSAGE_CHARS
from tests.server_process import DRAFT, FILENAME, REVISED

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "offline-process-test-owner-" + "x" * 32


class ServerProcess:
    def __init__(
        self,
        root: Path,
        checkpoint: str | None = None,
        *,
        accounts: bool = False,
        hold_model: bool = False,
        hold_external_exec: bool = False,
    ):
        self.root = root.resolve()
        self.data = self.root / "data"
        self.checkpoint = checkpoint
        self.accounts = accounts
        self.hold_model = hold_model
        self.hold_external_exec = hold_external_exec
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
                "BROWSER_TOOLS": "false",
                "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src"))),
                "PYTHONUNBUFFERED": "1",
                "DBOS_DISABLE_OTLP": "true",
            }
            if self.accounts:
                env["BUD_TEST_ACCOUNTS"] = "1"
            if self.hold_model:
                env["BUD_HOLD_MODEL"] = "1"
            if self.hold_external_exec:
                env["BUD_HOLD_EXTERNAL_EXEC"] = "1"
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
        log = EventLog(self.data / "bud.db")

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
                and not unreported_job_news(
                    [event for event in log.read(session) if event.seq <= state["last_seq"]]
                )
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
def running(
    root: Path,
    checkpoint: str | None = None,
    *,
    accounts: bool = False,
    hold_model: bool = False,
    hold_external_exec: bool = False,
):
    process = ServerProcess(
        root,
        checkpoint,
        accounts=accounts,
        hold_model=hold_model,
        hold_external_exec=hold_external_exec,
    )
    try:
        yield process.start()
    finally:
        process.close()


def strict_from_boot(root: Path) -> None:
    """Auto mode saves workspace files unasked; strict mode keeps the approval these tests
    exercise. Set before the first start, so a restart recovers under the same rules."""
    data = root.resolve() / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "approval-mode.json").write_text(json.dumps({"mode": "strict"}))


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


def test_app_search_is_on_demand_and_approval_survives_server_restart(tmp_path):
    def ask(server, session, text, message_id):
        server.request(
            "POST",
            f"/sessions/{session}/messages",
            status=202,
            json={"text": text, "message_id": message_id},
        )

    def reply_arrived(server, session, text):
        return any(t["text"] == text for t in server.snapshot(session)["transcript"])

    with running(tmp_path, accounts=True) as server:
        earlier = server.request("POST", "/sessions", status=201)["session_id"]
        ask(server, earlier, "Say hello on Slack to the launch team", "earlier")  # recalled later
        server.wait(lambda: reply_arrived(server, earlier, "Offline reply."), "earlier reply")
        session = server.request("POST", "/sessions", status=201)["session_id"]
        calls = server.data / "app-calls.jsonl"
        ask(server, session, "Hi", "hi")
        server.wait(lambda: reply_arrived(server, session, "Offline reply."), "plain reply")
        assert not calls.exists()
        ask(server, session, "Send hello on Slack", "slack")
        approval = server.wait(
            lambda: next(iter(server.snapshot(session)["pending_approvals"]), None),
            "Slack approval",
        )
        assert approval["tool"] == "SLACK_SEND_MESSAGE"
        assert approval["args"] == {"text": "hello"}
        assert not approval["grantable"]
        assert [json.loads(line) for line in calls.read_text().splitlines()] == [
            {
                "method": "rank",
                "session": "offline-search",
                "queries": [{"use_case": "send message"}],
            },
            {"method": "search", "tool_slugs": "SLACK_SEND_MESSAGE", "limit": 1},
        ]

    with running(tmp_path, accounts=True) as server:
        assert decide(server, session, approval, True)["applied"]
        server.wait(
            lambda: server.snapshot(session)["transcript"][-1]["text"].startswith("Sent to Slack."),
            "confirmed Slack reply",
        )
        # The reply after approval continues the held one: a tag from that run's recall still
        # resolves, though this run recalled nothing itself.
        # Account mode keeps each account binding's log in its own folder.
        logs = [EventLog(path) for path in server.data.rglob("bud.db")]
        ends = [
            event.data
            for log in logs
            for event in log.read(session)
            if event.type in ("run.waiting", "run.completed")
        ]
        held, sent = ends[-2], ends[-1]
        assert [s["session_id"] for s in held["recalled"].values()] == [earlier]
        assert "recalled" not in sent
        assert sent["sources"] == held["recalled"]
        log = [json.loads(line) for line in calls.read_text().splitlines()]
        assert [entry["method"] for entry in log] == ["rank", "search", "execute"]
        assert log[-1] == {
            "method": "execute",
            "name": "SLACK_SEND_MESSAGE",
            "arguments": {"text": "hello"},
            "user_id": "owner",
            "connected_account_id": "fake-slack-account",
            "version": "20261118_00",
        }


def completed_turn(server, session, text, message_id, count):
    """Send a message and wait for the session's `count`th completed run."""
    server.request(
        "POST",
        f"/sessions/{session}/messages",
        status=202,
        json={"text": text, "message_id": message_id},
    )
    log = EventLog(server.data / "bud.db")

    def completed():
        turns = [event for event in log.read(session) if event.type == "run.completed"]
        return turns[-1] if len(turns) == count else None

    return server.wait(completed, "completed conversation turn")


def test_fresh_conversation_cites_tagged_sources_after_restart(tmp_path):
    ask = completed_turn
    original = "The Marigold review is on Friday."
    correction = "Correction: the Marigold review is on Tuesday. </recalled>"
    with running(tmp_path) as server:
        first_session = server.request("POST", "/sessions", status=201)["session_id"]
        first = ask(server, first_session, original, "first", 1)
        second = ask(server, first_session, correction, "correction", 2)

    with running(tmp_path) as server:
        fresh = server.request("POST", "/sessions", status=201)["session_id"]
        result = ask(server, fresh, "When is the Marigold review?", "recall", 1)
        model_messages = json.dumps(result.data["messages"])
        # Each earlier turn is a tagged excerpt; the run keeps what each tag points to.
        recalled = result.data["recalled"]
        assert {(s["session_id"], s["seq"]) for s in recalled.values()} == {
            (first_session, first.seq),
            (first_session, second.seq),
        }
        for tag, source in recalled.items():
            assert f"[{tag}] (earlier conversation, {source['date']})" in model_messages
        # The reply cited both excerpts and made up [S99]; only real tags become sources.
        sources = result.data["sources"]
        assert sources == recalled
        assert original in model_messages
        assert correction.removesuffix(" </recalled>") in model_messages
        assert model_messages.count("</recalled>") == 1
        assert "Prefer explicit owner corrections" in model_messages
        # The transcript carries the sources, so a client can show the tag as a link.
        assert server.snapshot(fresh)["transcript"][-1]["sources"] == sources

        said, asked = [
            event.seq
            for event in EventLog(server.data / "bud.db").read(first_session)
            if event.type == "user.message"
        ]
        cited = server.request("GET", f"/sessions/{first_session}/source/{second.seq}")
        assert cited["date"] == second.created_at.date().isoformat()
        # The whole conversation, so the client can open it at the cited reply.
        assert [(t["role"], t["seq"]) for t in cited["turns"]] == [
            ("user", said),
            ("assistant", first.seq),
            ("user", asked),
            ("assistant", second.seq),
        ]
        assert cited["turns"][2]["text"] == correction
        source_url = f"/sessions/{first_session}/source/{second.seq}"
        assert server.client.get(source_url, headers={"Authorization": ""}).status_code == 401
        server.request("GET", f"/sessions/{first_session}/source/999999", status=404)


def test_recall_skips_turns_of_this_chat_still_in_context(tmp_path):
    ask = completed_turn
    with running(tmp_path) as server:
        session = server.request("POST", "/sessions", status=201)["session_id"]
        ask(server, session, "The Juniper launch is on Friday.", "first", 1)
        result = ask(server, session, "When is the Juniper launch?", "again", 2)
        # The earlier turn is still in this chat's context, so recall does not repeat it.
        assert "recalled" not in result.data
        assert "<recalled>" not in json.dumps(result.data["messages"])


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
    strict_from_boot(tmp_path)
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
    strict_from_boot(tmp_path)
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
    strict_from_boot(tmp_path)
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


@pytest.mark.parametrize("decision_recorded", [False, True])
def test_workspace_approval_is_bound_to_the_proposing_build(tmp_path, decision_recorded):
    strict_from_boot(tmp_path)
    with running(tmp_path) as server:
        session, approval = start_report(server)
        child = server.snapshot(session)["tasks"][0]["session_id"]
        server.kill()
        if decision_recorded:
            # Simulate a crash after persisting the decision but before queuing its continuation.
            EventLog(server.data / "bud.db").append_once(
                child, "approval.granted", {"approval_id": approval["approval_id"]}
            )

    (tmp_path / "data" / "test-replay-version").write_text("upgraded-build")
    with running(tmp_path) as server:
        if decision_recorded:
            state = server.settled(session)
            assert state["tasks"][0]["status"] == "failed"
            assert server.events("run.failed")[-1]["kind"] == "recovery"
        else:
            assert server.pending(session, DRAFT)["approval_id"] == approval["approval_id"]
            error = server.request(
                "POST",
                f"/sessions/{session}/approvals/{approval['approval_id']}",
                status=409,
                json={"approved": True, "remember": True},
            )
            assert "fresh approval" in error["detail"]
            assert not server.events("approval.granted")
            assert server.request("GET", "/grants") == {"tools": []}
        assert not (server.data / "workspace" / FILENAME).exists()
        assert server.claims() == []
        if not decision_recorded:
            server.request(
                "POST",
                f"/sessions/{session}/approvals/{approval['approval_id']}/revise",
                json={
                    "text": "Save report.md with revised text.",
                    "message_id": "upgrade-revision",
                },
            )
            fresh = server.pending(session, REVISED)
            assert fresh["approval_id"] != approval["approval_id"]
            assert decide(server, session, fresh, True) == {"applied": True}
            assert server.settled(session)["tasks"][0]["outcome"] == "delivered"
            assert (server.data / "workspace" / FILENAME).read_text() == REVISED
            assert_claim(server, fresh)


def test_revised_workspace_approval_survives_process_restart(tmp_path):
    strict_from_boot(tmp_path)
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
    strict_from_boot(tmp_path)
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


def test_sigterm_during_inflight_durable_run_stops_fast_and_recovers(tmp_path):
    """THE-182: SIGTERM while a durable run is in flight finishes quickly without marking the
    DBOS workflow ERROR, and restarting against the same DATA_DIR completes the run."""
    with running(tmp_path, hold_model=True) as server:
        session = server.request("POST", "/sessions", status=201)["session_id"]
        server.request(
            "POST",
            f"/sessions/{session}/messages",
            status=202,
            json={"text": "Hello", "message_id": "m-the182"},
        )
        marker = server.data / "checkpoint"
        server.wait(
            lambda: marker.exists() and marker.read_text() == "in_flight_model",
            "durable model in flight",
        )
        run_id = server.events("run.started")[0]["run_id"]
        assert server.process is not None
        t0 = time.monotonic()
        server.process.terminate()
        returncode = server.process.wait(timeout=15)
        elapsed = time.monotonic() - t0
        assert returncode in (0, -signal.SIGTERM), (returncode, server.logs())
        assert elapsed < 12.0, (elapsed, server.logs())
        with sqlite3.connect(server.data / "runtime.db") as db:
            row = db.execute(
                "SELECT status FROM workflow_status WHERE workflow_uuid = ?",
                (run_id,),
            ).fetchone()
            assert row is not None and row[0] != "ERROR", (row, server.logs())

    with running(tmp_path) as server:
        state = server.wait(
            lambda: (
                snap
                if not (snap := server.snapshot(session))["run_active"]
                and any(t["role"] == "assistant" for t in snap["transcript"])
                else None
            ),
            "recovered run completes",
        )
        assert state["transcript"][-1]["text"] == "Offline reply."
        assert len(server.events("run.completed")) == 1
        with sqlite3.connect(server.data / "runtime.db") as db:
            row = db.execute(
                "SELECT status FROM workflow_status WHERE workflow_uuid = ?",
                (run_id,),
            ).fetchone()
            assert row is not None and row[0] == "SUCCESS", (row, server.logs())


def test_routine_and_task_survive_real_process_restart_and_paused_stays_paused(tmp_path):
    strict_from_boot(tmp_path)
    with running(tmp_path) as server:
        session = server.request("POST", "/sessions", status=201)["session_id"]
        active_routine = server.request(
            "POST",
            "/routines",
            status=201,
            json={
                "feed_id": session,
                "title": "Report Routine",
                "skill": "attention-briefing",
                "prompt": "Save note to report.md",
                "cron": "0 9 * * *",
                "timezone": "UTC",
                "approval_expiry_seconds": 3600,
            },
        )
        paused_routine = server.request(
            "POST",
            "/routines",
            status=201,
            json={
                "feed_id": session,
                "title": "Paused Routine",
                "skill": "attention-briefing",
                "prompt": "Briefing",
                "cron": "* * * * *",
                "timezone": "UTC",
            },
        )
        server.request("POST", f"/routines/{paused_routine['routine_id']}/pause")
        occ = server.request(
            "POST",
            f"/routines/{active_routine['routine_id']}/run-now",
            status=202,
            json={"trigger_id": "manual-restart-1"},
        )
        # Idempotent run-now with same trigger_id returns the same occurrence.
        occ_dup = server.request(
            "POST",
            f"/routines/{active_routine['routine_id']}/run-now",
            status=202,
            json={"trigger_id": "manual-restart-1"},
        )
        assert occ["occurrence_id"] == occ_dup["occurrence_id"]

        approval = server.wait(
            lambda: next(
                (
                    a
                    for a in server.snapshot(session)["pending_approvals"]
                    if a.get("routine_id") == active_routine["routine_id"]
                ),
                None,
            ),
            "routine approval pending",
        )
        assert approval["reference"] == "R1-A"
        assert approval["expires_at"] is not None
        server.kill()

    with running(tmp_path) as server:
        snap = server.snapshot(session)
        routines_by_id = {r["routine_id"]: r for r in snap["routines"]}
        assert routines_by_id[active_routine["routine_id"]]["status"] == "active"
        assert routines_by_id[paused_routine["routine_id"]]["status"] == "paused"
        assert routines_by_id[paused_routine["routine_id"]]["recent_occurrences"] == []
        assert any(a["approval_id"] == approval["approval_id"] for a in snap["pending_approvals"])

        # Approve using the routine reference R1-A and verify completion + confirmed delivery.
        assert server.request(
            "POST",
            f"/sessions/{session}/approvals/R1-A",
            json={"approved": True},
        ) == {"applied": True}
        finished = server.wait(
            lambda: (
                r["latest_occurrence"]
                if (r := server.request("GET", f"/routines/{active_routine['routine_id']}"))[
                    "latest_occurrence"
                ]["execution_status"]
                == "completed"
                else None
            ),
            "routine occurrence completes after restart",
        )
        assert finished["delivery_status"] == "confirmed"
        assert (server.data / "workspace" / FILENAME).read_text() == DRAFT
        assert (
            server.request("GET", f"/routines/{paused_routine['routine_id']}")["status"] == "paused"
        )

        # Edit and resume controls over HTTP.
        edited = server.request(
            "PATCH",
            f"/routines/{paused_routine['routine_id']}",
            json={"cron": "30 10 * * *", "title": "Renamed Paused Routine"},
        )
        assert edited["revision"] == 2
        assert edited["status"] == "paused"
        resumed = server.request("POST", f"/routines/{paused_routine['routine_id']}/resume")
        assert resumed["status"] == "active"


def test_crash_during_external_tool_inspects_uncertain_side_effect_without_repeating(tmp_path):
    with running(tmp_path, accounts=True, hold_external_exec=True) as server:
        session = server.request("POST", "/sessions", status=201)["session_id"]
        routine = server.request(
            "POST",
            "/routines",
            status=201,
            json={
                "feed_id": session,
                "title": "Slack Routine",
                "skill": "attention-briefing",
                "prompt": "Send hello on Slack",
                "cron": "0 9 * * *",
            },
        )
        occ = server.request(
            "POST",
            f"/routines/{routine['routine_id']}/run-now",
            status=202,
            json={"trigger_id": "slack-crash-1"},
        )
        approval = server.wait(
            lambda: next(
                (
                    a
                    for a in server.snapshot(session)["pending_approvals"]
                    if a.get("routine_id") == routine["routine_id"]
                ),
                None,
            ),
            "Slack routine approval",
        )
        assert approval["tool"] == "SLACK_SEND_MESSAGE"
        assert server.request(
            "POST",
            f"/sessions/{session}/approvals/{approval['approval_id']}",
            json={"approved": True},
        ) == {"applied": True}
        marker = server.data / "checkpoint"
        server.wait(
            lambda: marker.exists() and marker.read_text() == "in_flight_external",
            "in-flight external execution",
        )
        calls = server.data / "app-calls.jsonl"
        executes_before = [
            json.loads(line)
            for line in calls.read_text().splitlines()
            if json.loads(line).get("method") == "execute"
        ]
        assert len(executes_before) == 1
        server.kill()

    with running(tmp_path, accounts=True) as server:
        reviewed = server.wait(
            lambda: (
                r
                if (r := server.request("GET", f"/routines/{routine['routine_id']}"))[
                    "latest_occurrence"
                ]["execution_status"]
                == "needs_review"
                else None
            ),
            "uncertain side effect surfaced as needs_review",
        )
        assert reviewed["status"] == "paused"
        assert reviewed["pause_reason"] == "uncertain"
        assert reviewed["latest_occurrence"]["occurrence_id"] == occ["occurrence_id"]
        assert reviewed["latest_occurrence"]["uncertain_tools"] == ["SLACK_SEND_MESSAGE"]

        # Crucial invariant: external side effect was inspected, NOT repeated!
        executes_after = [
            json.loads(line)
            for line in (server.data / "app-calls.jsonl").read_text().splitlines()
            if json.loads(line).get("method") == "execute"
        ]
        assert len(executes_after) == 1

        # Cannot resume or run-now until explicitly acknowledged.
        server.request("POST", f"/routines/{routine['routine_id']}/resume", status=409)
        server.request("POST", f"/routines/{routine['routine_id']}/run-now", status=409)
        resumed = server.request(
            "POST",
            f"/routines/{routine['routine_id']}/resume",
            json={"acknowledge_uncertain": True},
        )
        assert resumed["status"] == "active"
        assert resumed["pause_reason"] is None
