import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from dbos._dbos import _get_or_create_dbos_registry
from pydantic import SecretStr
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart

from bud.config import get_settings
from bud.core import server
from bud.core.background import Background
from bud.core.events import MessageOrigin
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.projection import RoutineDestination, snapshot
from bud.core.routines import RoutineStore
from bud.harness.preferences import PreferenceStore
from tests.runner_support import create_note, reply, running_bud
from tests.server_process import offline_gateway

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
TOKEN = "offline-always-on-owner-" + "x" * 32


def create_routine(runner, **overrides):
    options = dict(
        feed_id="owner",
        title="Briefing",
        skill="attention-briefing",
        prompt="Prepare a short summary.",
        cron="0 * * * *",
    )
    return runner.create_routine(**(options | overrides))


def tool_call(name, args):
    return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=name)])


def tool_returns(messages):
    return [p for message in messages for p in message.parts if isinstance(p, ToolReturnPart)]


async def drain_timers(background):
    async with asyncio.timeout(2):
        while background._tasks:
            await asyncio.sleep(0.01)


async def test_edit_preserves_the_owner_words_and_keeps_a_revised_prompt_separate(tmp_path):
    async with running_bud(tmp_path, lambda m, i: reply(), jobs=True) as bud:
        for session in ("owner", "alternate"):
            bud.log.append(session, "session.created")
        original = "  Tell me about my team every morning.\n"
        routine = create_routine(bud.runner, owner_request=original)
        for edit in (
            {"title": "Team briefing"},
            {"timezone": "America/Chicago"},
            {"destination": RoutineDestination(feed_id="alternate")},
            {"prompt": "Rewritten instructions"},
        ):
            routine = bud.runner.edit_routine(routine.routine_id, **edit)
            assert routine.owner_request == original
        revised = bud.runner.edit_routine(routine.routine_id, owner_request="  Check calendar. ")
        assert revised.owner_request == "  Check calendar. "
        assert revised.prompt == "Rewritten instructions"


async def test_report_then_quiet_preserves_context_without_a_chat_report(tmp_path, monkeypatch):
    prompts = []
    response = ["Budget updated. https://example.org/budget"]

    def respond(messages, info):
        prompts.append("\n".join(str(p.content) for m in messages for p in m.parts))
        return reply(response[0])

    async with running_bud(tmp_path, respond, jobs=True, preferences=True) as bud:
        bud.log.append("owner", "session.created")
        prefs = bud.runner.harness.preferences
        await prefs.set("global", "report-style", "Use concise lists.")
        store = bud.runner.routines
        routine = create_routine(bud.runner)
        bud.runner.run_routine_now(routine.routine_id)
        await bud.settle()

        response[0] = "[[QUIET]]\nBudget was unchanged."
        # A previous failure must be cleared by a quiet occurrence.
        with bud.log._connect() as db:
            db.execute(
                "UPDATE routines SET consecutive_failures=2 WHERE routine_id=?",
                (routine.routine_id,),
            )
        streamed = []
        monkeypatch.setattr(bud.runner.hub, "broadcast", lambda *args: streamed.append(args))
        occurrence = bud.runner.run_routine_now(routine.routine_id)
        await bud.settle()

        updated = store.get(routine.routine_id)
        assert updated.last_reported_summary == "Budget updated. https://example.org/budget"
        assert updated.last_reported_items == ["https://example.org/budget"]
        assert updated.consecutive_failures == 0
        assert "Already reported last time" in prompts[1]
        assert "https://example.org/budget" in prompts[1]
        quiet = store.occurrence(occurrence.occurrence_id)
        assert (quiet.execution_status, quiet.delivery_status) == ("quiet", "quiet")
        assert store.run_view(quiet).quiet_note == "Budget was unchanged."
        assert not any(args[1] == "text.delta" for args in streamed)
        assert not bud.runner.snapshot(quiet.session_id).transcript
        [report] = [e for e in bud.log.read("owner") if e.type == "routine.completed"]
        assert report.data["memory_used"] == prefs.prompt_records()
        assert bud.runner.snapshot("owner").transcript[-1].memory_used[0].text == (
            "Use concise lists."
        )


async def test_failure_notices_are_first_and_fifth_with_idempotent_accounting(tmp_path):
    def fail(messages, info):
        raise RuntimeError("Source offline")

    async with running_bud(tmp_path, fail, jobs=True) as bud:
        bud.log.append("owner", "session.created")
        store = bud.runner.routines
        routine = create_routine(bud.runner)
        for attempt in range(1, 6):
            occ = bud.runner.run_routine_now(routine.routine_id)
            await bud.settle()
            bud.runner.tick_routines()
            assert store.occurrence(occ.occurrence_id).execution_status == "failed"
            current = store.get(routine.routine_id)
            assert current.consecutive_failures == attempt
            assert current.last_failure_error == "RuntimeError"
            notices = [e.data for e in bud.log.read("owner") if e.type == "routine.failed"]
            assert [n["consecutive_failures"] for n in notices] == ([1, 5] if attempt == 5 else [1])
        assert current.status == "paused"
        assert current.next_run_at is None
        assert "paused after 5 failures" in notices[-1]["text"]


async def test_pinned_destination_and_atomic_local_fallback(tmp_path, monkeypatch):
    async with running_bud(tmp_path, lambda m, i: reply("Briefing ready"), jobs=True) as bud:
        for session in ("owner", "before", "after"):
            bud.log.append(session, "session.created")
        store = bud.runner.routines
        destination = RoutineDestination(
            feed_id="before",
            origin=MessageOrigin(channel="imessage"),
            save_report=False,
        )
        routine = create_routine(bud.runner, destination=destination)
        occ, _ = store.claim_manual_occurrence(routine.routine_id, now=NOW)
        bud.runner.edit_routine(routine.routine_id, destination=RoutineDestination(feed_id="after"))

        confirm = store._confirm_in_tx

        def fail_confirmation(*args, **kwargs):
            raise OSError("simulated crash before commit")

        monkeypatch.setattr(store, "_confirm_in_tx", fail_confirmation)
        assert [o.occurrence_id for o in bud.runner.tick_routines()] == [occ.occurrence_id]
        await bud.settle()
        assert store.occurrence(occ.occurrence_id).delivery_status == "pending"
        assert not any(e.type == "routine.completed" for e in bud.log.read("before"))

        monkeypatch.setattr(store, "_confirm_in_tx", confirm)
        bud.runner.tick_routines()
        stored = store.occurrence(occ.occurrence_id)
        assert stored.destination == destination
        assert (stored.delivery_status, stored.delivery_channel) == ("confirmed", "local")
        [report] = [e for e in bud.log.read("before") if e.type == "routine.completed"]
        assert report.id == stored.delivery_receipt
        assert report.data["delivery_fallback"] == "imessage_unavailable"
        assert report.data["origin"]["channel"] == "api"
        assert not any(e.type == "routine.completed" for e in bud.log.read("after"))
        assert (
            len([e for e in bud.log.read("before") if e.type == "routine.delivery_confirmed"]) == 1
        )


async def test_live_tick_recovers_and_serializes_queue(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def hold() -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "Ready"

    def respond(messages, info):
        return reply("Reported") if tool_returns(messages) else tool_call("hold", {})

    async with running_bud(
        tmp_path, respond, jobs=True, extra_tools=[hold], extra_policy={"hold": "allow"}
    ) as bud:
        bud.log.append("owner", "session.created")
        clock = [NOW]
        bud.runner.harness.clock = lambda: clock[0]
        routine = create_routine(bud.runner, overlap_policy="queue")
        store = bud.runner.routines
        pending, _ = store.claim_manual_occurrence(routine.routine_id, now=NOW)
        assert store.occurrence(pending.occurrence_id).execution_status == "pending"
        assert [occ.occurrence_id for occ in bud.runner.tick_routines()] == [pending.occurrence_id]
        await asyncio.wait_for(entered.wait(), 1)
        clock[0] += timedelta(hours=1, seconds=1)
        assert bud.runner.tick_routines() == []
        [waiting] = store.pending_dispatches()
        assert not bud.log.read(waiting.session_id, after=1)
        assert calls == 1
        release.set()
        await bud.settle()
        assert store.occurrence(pending.occurrence_id).execution_status == "completed"
        assert [occ.occurrence_id for occ in bud.runner.tick_routines()] == [waiting.occurrence_id]
        await bud.settle()
        assert store.occurrence(waiting.occurrence_id).execution_status == "completed"
        assert calls == 2


@pytest.mark.parametrize("event_was_written", [False, True])
async def test_reminder_recovers_on_either_side_of_the_event_commit(tmp_path, event_was_written):
    log = EventLog(tmp_path / "events.db")
    log.append("owner", "session.created")
    hub = Hub(log)
    path = tmp_path / "background.db"
    background = Background(path, tmp_path, None, now=lambda: NOW)

    class Crash:
        def publish_once(self, *args, **kwargs):
            if event_was_written:
                hub.publish_once(*args, **kwargs)
            raise OSError("simulated crash")

    background.bind(Crash())
    item = background.remind("owner", "Check calendar", NOW)
    await asyncio.sleep(0)
    assert background.items("owner")[0].status == "running"
    await background.close()

    restarted = Background(path, tmp_path, None, now=lambda: NOW)
    restarted.bind(hub)
    await restarted.resume()
    await drain_timers(restarted)
    [notice] = [e for e in log.read("owner") if e.type == "background.finished"]
    assert notice.data["id"] == item.id
    assert restarted.items("owner") == []


@pytest.mark.parametrize("answer", ["Check the invoice draft.", "[[QUIET]]"])
async def test_follow_up_wakes_the_owner_session_once_and_can_end_quietly(tmp_path, answer):
    prompts = []

    def respond(messages, info):
        prompts.append(str(messages[-1].parts[-1].content))
        return reply(answer)

    async with running_bud(tmp_path, respond) as bud:
        bud.log.append("owner", "session.created")
        background = Background(tmp_path / "background.db", bud.root, None, now=lambda: NOW)
        background.bind(bud.runner.hub, bud.runner.wake_follow_up)
        item = background.follow_up("owner", "See whether it was paid", "thread:Q78", NOW)
        await drain_timers(background)
        await bud.settle()
        assert bud.runner.wake_follow_up(item)
        [queued] = [e for e in bud.log.read("owner") if e.type == "run.queued"]
        assert queued.data["trigger"] == "follow_up"
        assert queued.data["request"] is None
        assert "thread:Q78" in prompts[0]
        assert "See whether it was paid" in prompts[0]
        assert len(prompts) == 1
        assert [turn.text for turn in bud.runner.snapshot("owner").transcript] == (
            [] if answer == "[[QUIET]]" else [answer]
        )


async def test_chat_approval_expires_between_click_and_tool_dispatch(tmp_path, monkeypatch):
    def respond(messages, info):
        return reply("Action checked.") if tool_returns(messages) else create_note()

    async with running_bud(tmp_path, respond) as bud:
        clock = [NOW]
        bud.runner.harness.clock = lambda: clock[0]
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Create note.txt.", "note")
        await bud.settle()
        [approval] = bud.runner.snapshot("owner").pending_approvals
        expiry = datetime.fromisoformat(approval.expires_at)
        assert expiry == NOW + timedelta(days=1)
        launch = bud.runner._launch
        queued = []
        monkeypatch.setattr(bud.runner, "_launch", queued.append)
        clock[0] = expiry - timedelta(seconds=1)
        assert bud.runner.decide("owner", approval.approval_id, True)
        [run] = queued
        clock[0] = expiry
        launch(run)
        await bud.settle()
        assert not (bud.root / "note.txt").exists()
        receipt = bud.runner.snapshot("owner").transcript[-1].approval_receipts[0]
        assert (receipt.approval_id, receipt.decision, receipt.status) == (
            approval.approval_id,
            "expired",
            "denied",
        )


async def test_sources_receipts_preferences_and_browser_frames_survive_projection(tmp_path):
    async def search_web() -> dict:
        return {
            "results": [
                {"title": "One", "url": "https://a.example/#top", "text": "Snippet"},
                {"title": "Two", "url": "https://b.example/"},
            ]
        }

    async def read_webpage() -> str:
        return "Title: One\nURL: https://a.example/\nText: The full page."

    async def browser_navigate(url: str) -> dict:
        return {"tab": 5, "url": url, "title": "Account"}

    async def capture(tab: str):
        assert tab == "5"
        return "https://c.example/", "Account", b"offline frame"

    def respond(messages, info):
        returned = {p.tool_name for p in tool_returns(messages)}
        for name in ("search_web", "read_webpage", "browser_navigate"):
            if name not in returned:
                return tool_call(
                    name, {"url": "https://c.example/"} if name == "browser_navigate" else {}
                )
        if "workspace_create" not in returned:
            return create_note()
        return reply("The note is ready.")

    tools = [search_web, read_webpage, browser_navigate]
    async with running_bud(
        tmp_path,
        respond,
        preferences=True,
        extra_tools=tools,
        extra_policy=dict.fromkeys((tool.__name__ for tool in tools), "allow"),
    ) as bud:
        bud.runner.bind_browser_capture(capture)
        prefs = bud.runner.harness.preferences
        await prefs.set("global", "shape", "Use concise lists.")
        await prefs.set("global", "old", "Use formal greetings.")
        await prefs.forget("global", "old")
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Read https://c.example/ and write note.txt.", "note")
        await bud.settle()
        [approval] = bud.runner.snapshot("owner").pending_approvals
        assert bud.runner.decide("owner", approval.approval_id, True)
        await bud.settle()

        events = EventLog(bud.log.path).read("owner")
        [complete] = [e for e in events if e.type == "run.completed"]
        turn = snapshot("owner", events).transcript[-1].model_dump()
        for key in ("sources", "approval_receipts", "memory_used", "browser_frames"):
            assert turn[key] == complete.data[key]
        assert {entry["domain"]: entry["status"] for entry in turn["sources"]} == {
            "a.example": "read",
            "b.example": "found",
            "c.example": "read",
        }
        assert turn["sources"][0]["excerpt"] == "The full page."
        assert turn["memory_used"] == prefs.prompt_records(scopes={"global"})
        [receipt] = turn["approval_receipts"]
        assert (receipt["approval_id"], receipt["decision"], receipt["status"]) == (
            approval.approval_id,
            "approved",
            "confirmed",
        )
        [frame] = turn["browser_frames"]
        assert bud.runner.browser_frame(frame["frame_id"]) == b"offline frame"


async def test_preference_forget_frame_and_provenance_routes(monkeypatch):
    settings = get_settings()
    settings.data_dir.mkdir(parents=True)
    gateway = offline_gateway(settings)
    monkeypatch.setattr(server, "build_gateway", lambda *args, **kwargs: gateway)
    runner_cls = server.Runner
    runners = []

    def non_durable_runner(*args, **kwargs):
        kwargs["durable"] = False
        runner = runner_cls(*args, **kwargs)
        runners.append(runner)
        return runner

    monkeypatch.setattr(server, "Runner", non_durable_runner)
    app = server._create_app(settings, SecretStr(TOKEN), None, simulate=True, model_id=None)
    [runner] = runners
    prefs: PreferenceStore = runner.harness.preferences
    await prefs.set("global", "tone", "Keep replies concise.")
    [record] = prefs.prompt_records()
    pref_id = record["id"]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            response = await client.post(f"/preferences/{pref_id}/forget")
            assert response.status_code == 200
            assert response.json() == {"ok": True, "id": pref_id}
            assert prefs.prompt_records() == []
            assert (await client.post(f"/preferences/{pref_id}/forget")).status_code == 404

            with runner.hub.log._connect() as db:
                db.execute(
                    "INSERT INTO browser_frames VALUES (?, ?, ?, ?, ?, ?)",
                    ("f" * 32, "run", "https://example.org", "A tab", NOW.isoformat(), b"jpeg"),
                )
            frame = await client.get("/browser/frames/" + "f" * 32)
            assert frame.status_code == 200
            assert frame.headers["content-type"] == "image/jpeg"
            assert frame.content == b"jpeg"
            assert (await client.get("/browser/frames/" + "a" * 32)).status_code == 404

            log = runner.hub.log
            log.append("owner", "session.created")
            citations = {"S1": {"label": "Earlier turn"}}
            web = [
                {
                    "title": "Public source",
                    "url": "https://example.com/",
                    "domain": "example.com",
                    "excerpt": "Page excerpt",
                    "status": "read",
                }
            ]
            event = log.append(
                "owner",
                "run.completed",
                {
                    "run_id": "one",
                    "sources": citations,
                    "web_sources": web,
                    "messages": [
                        {
                            "kind": "response",
                            "parts": [{"part_kind": "text", "content": "See [S1]."}],
                        }
                    ],
                },
            )
            legacy = (await client.get("/sessions/owner")).json()["transcript"][-1]
            assert legacy["sources"] == citations and legacy["web_sources"] == web
            modern = (await client.get("/sessions/owner/conversation")).json()["transcript"][-1]
            assert modern["sources"] == web and modern["citation_sources"] == citations

            async def stream_one(session_id, after):
                assert session_id == "owner"
                yield event

            monkeypatch.setattr(runner.hub, "subscribe", stream_one)
            response = await client.get("/sessions/owner/events?source_format=web")
            data = next(
                line.removeprefix("data: ")
                for line in response.text.splitlines()
                if line.startswith("data: ")
            )
            assert json.loads(data)["data"]["sources"] == web
            assert json.loads(data)["data"]["citation_sources"] == citations
    finally:
        await runner.stop()
        gateway.close()


async def test_routine_create_approval_contains_exact_schedule(tmp_path):
    args = {
        "title": "Briefing",
        "skill": "attention-briefing",
        "prompt": "Check the inbox.",
        "cron": "0 9 * * *",
        "timezone": "America/Chicago",
    }
    async with running_bud(
        tmp_path, lambda m, i: tool_call("create_routine", args), jobs=True
    ) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Create a daily inbox routine at 9 Chicago time.", "routine")
        await bud.settle()
        [card] = bud.runner.snapshot("owner").pending_approvals
        assert card.timezone == "America/Chicago"
        assert datetime.fromisoformat(card.next_run_at) == datetime(2026, 11, 6, 15, tzinfo=UTC)


async def test_legacy_sqlite_rows_migrate_and_keep_the_queued_destination(tmp_path):
    async with running_bud(tmp_path, lambda m, i: reply(), jobs=True) as bud:
        for session in ("owner", "next"):
            bud.log.append(session, "session.created")
        store = bud.runner.routines
        old_destination = RoutineDestination(
            feed_id="owner", origin=MessageOrigin(channel="imessage"), save_report=False
        )
        routine = create_routine(
            bud.runner, destination=old_destination, owner_request="Owner words"
        )
        occ, _ = store.claim_manual_occurrence(routine.routine_id, now=NOW)
        bud.log.append(
            occ.session_id,
            "run.queued",
            {"run_id": occ.run_id, "destination": old_destination.model_dump()},
        )
        receipt = bud.log.append("owner", "routine.completed", {"text": "Legacy report"})
        store.edit(routine.routine_id, destination=RoutineDestination(feed_id="next"))
        with bud.log._connect() as db:
            db.execute(
                "UPDATE routine_occurrences SET execution_status='completed', "
                "delivery_status='dispatched', delivery_receipt=? WHERE occurrence_id=?",
                (receipt.id, occ.occurrence_id),
            )
            for column in (
                "last_reported_summary",
                "last_reported_items_json",
                "consecutive_failures",
                "last_failure_error",
            ):
                db.execute(f"ALTER TABLE routines DROP COLUMN {column}")
            for column in ("destination_json", "quiet_note", "queued_at", "delivery_channel"):
                db.execute(f"ALTER TABLE routine_occurrences DROP COLUMN {column}")

        migrated = RoutineStore(bud.runner.hub)
        assert migrated.get(routine.routine_id).owner_request == "Owner words"
        assert migrated.get(routine.routine_id).last_reported_items == []
        saved = migrated.occurrence(occ.occurrence_id)
        assert saved.destination == old_destination
        assert (saved.delivery_status, saved.delivery_receipt) == ("failed", None)
        assert RoutineStore(bud.runner.hub).occurrence(occ.occurrence_id) == saved


async def test_durable_job_approval_expires_before_dispatch(monkeypatch):
    settings = get_settings()
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "approval-mode.json").write_text('{"mode":"strict"}')
    gateway = offline_gateway(settings)
    monkeypatch.setattr(server, "build_gateway", lambda *args, **kwargs: gateway)
    monkeypatch.setattr(_get_or_create_dbos_registry(), "instance_info_map", {})
    compose = server.compose_runner
    runners = []

    def capture_runner(*args, **kwargs):
        runner = compose(*args, **kwargs)
        runners.append(runner)
        return runner

    monkeypatch.setattr(server, "compose_runner", capture_runner)
    app = server._create_app(settings, SecretStr(TOKEN), None, simulate=True, model_id=None)
    [runner] = runners
    clock = [NOW]
    runner.harness.clock = lambda: clock[0]

    async with app.router.lifespan_context(app):
        runner.hub.log.append("owner", "session.created")
        runner.start("owner", "Save the report.", "job")
        async with asyncio.timeout(15):
            while not (cards := runner.snapshot("owner").pending_approvals):
                await asyncio.sleep(0.02)
            while runner.tasks:
                await asyncio.sleep(0.02)
        [card] = cards
        [job] = runner.work_items.items("owner")
        assert card.task_id == job.task_id
        expiry = datetime.fromisoformat(card.expires_at)
        assert expiry == NOW + timedelta(hours=24)
        launch = runner._launch
        queued = []
        with monkeypatch.context() as patch:
            patch.setattr(runner, "_launch", queued.append)
            clock[0] = expiry - timedelta(seconds=1)
            assert runner.decide(job.session_id, card.approval_id, True)
            [continuation] = queued
        clock[0] = expiry
        launch(continuation)
        async with asyncio.timeout(15):
            while runner.tasks:
                await asyncio.sleep(0.02)
        assert not (runner.workspace / "report.md").exists()
        expired = [e for e in runner.hub.log.read(job.session_id) if e.type == "approval.expired"]
        assert [e.data["approval_id"] for e in expired] == [card.approval_id]
