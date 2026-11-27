from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr
from pydantic_ai import Tool
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from bud.composio.binding import AccountBinding
from bud.composio.connection import Connections
from bud.config import Settings
from bud.core.log import EventLog
from bud.core.runner import Runner
from bud.core.server import _create_app
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, Registry
from bud.gateway.router import Gateway
from bud.harness.context import BackgroundCompaction
from tests.runner_support import reply


@pytest.fixture
def setup_api(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    found = Mock(spec=Connections)
    found.settings = settings
    found.folder = tmp_path / "composio"
    found.user_id = "owner"
    found.capture_binding.return_value = AccountBinding("owner", 1, ())
    found.binding = found.capture_binding.return_value
    # The store holds what was adopted unless a test changes it.
    found.stored.side_effect = lambda: found.binding

    def adopt(expected):
        found.binding = expected

    found.adopt.side_effect = adopt
    found.accounts.return_value = {}
    found.restart_required.return_value = False
    found.store = Mock()
    found.store.session_scope.return_value = "scope"
    found.connect.return_value = "https://example.test/private-sign-in"
    found.pending_url.return_value = "https://example.test/existing-sign-in"
    found.statuses.return_value = {"github": "ACTIVE"}
    monkeypatch.setattr("bud.core.server.Connections", lambda _: found)
    remote = [FunctionToolset[None]([])]
    monkeypatch.setattr("bud.composio.tools.build_toolset", lambda *args: remote[0])
    spec = ModelSpec(provider="openai", name="scripted", tier="fast", input=0, output=0, cached=0)
    gateway = Gateway(
        Registry(models=[spec]),
        Ledger(tmp_path / "ledger.db"),
        lambda _: FunctionModel(lambda *_: reply()),
    )
    monkeypatch.setattr("bud.core.server.build_gateway", lambda *args, **kwargs: gateway)

    # Finite setup requests do not launch workflows; keep DBOS's process-global registry out.
    created = []

    def runner(*args, **kwargs):
        kwargs["durable"] = False
        instance = Runner(*args, **kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr("bud.core.server.Runner", runner)
    token = "offline-setup-test-" + "x" * 43
    app = _create_app(settings, SecretStr(token), None, simulate=False, model_id=None)
    log = EventLog(found.folder / "sessions" / "scope" / "bud.db")
    log.append("session", "session.created")
    log.append(
        "session",
        "capability.setup",
        {"capability": "github", "label": "GitHub", "kind": "url", "status": "waiting"},
    )
    try:
        yield app, token, found, log, created[0], remote
    finally:
        gateway.close()


async def test_mac_can_recover_a_discovered_apps_link_and_check_it(setup_api):
    app, token, found, log, _, _ = setup_api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        route = "/sessions/session/setup/github/check"
        assert (await client.post(route)).status_code == 401
        client.headers["Authorization"] = f"Bearer {token}"
        link = await client.post("/connections/github/connect")
        assert link.status_code == 200
        assert link.headers["cache-control"] == "no-store"
        assert link.json()["url"] == "https://example.test/private-sign-in"
        found.connect.assert_called_once_with("github")
        resumed = await client.post("/connections/github/connect", json={"resume": True})
        assert resumed.status_code == 200
        assert resumed.json()["url"] == "https://example.test/existing-sign-in"
        assert found.connect.call_count == 1
        found.pending_url.assert_called_once_with("github")
        recover_only = {"resume": True, "refresh": False}
        recovered = await client.post("/connections/github/connect", json=recover_only)
        assert recovered.json()["url"] == "https://example.test/existing-sign-in"
        assert recovered.json()["expired"] is False
        found.pending_url.return_value = None
        expired = await client.post("/connections/github/connect", json=recover_only)
        assert expired.status_code == 200
        assert expired.json()["expired"] is True
        assert expired.json()["url"] is None
        assert found.connect.call_count == 1
        refreshed = await client.post("/connections/github/connect", json={"resume": True})
        assert refreshed.json()["expired"] is False
        assert refreshed.json()["url"] == "https://example.test/private-sign-in"
        assert found.connect.call_count == 2
        found.connect.assert_called_with("github", replace_active=False)
        found.statuses.return_value = {"github": "EXPIRED"}
        expired_check = await client.post(route)
        assert expired_check.status_code == 200
        assert expired_check.json()["status"] == "failed"
        assert "expired" in expired_check.json()["reason"]
        found.statuses.return_value = {"github": "ACTIVE"}
        checked = await client.post(route)
        assert checked.status_code == 200
        assert checked.json()["status"] == "connected"
        assert checked.headers["cache-control"] == "no-store"
        snapshot = (await client.get("/sessions/session")).json()
        assert snapshot["pending_setup"] == []
        assert "private-sign-in" not in str(log.read("session"))
        assert (await client.post("/sessions/unknown/setup/github/check")).status_code == 404
        assert (await client.post("/sessions/session/setup/missing/check")).status_code == 404
        assert (await client.post("/connections/Bad%20Slug/connect")).status_code == 400


async def test_connections_inventory_preserves_status_keys_and_can_disconnect(setup_api):
    app, token, found, _, _, _ = setup_api
    accounts = [
        {
            "toolkit": "github",
            "account_id": "acct-gh",
            "display_name": "GitHub",
            "status": "expired",
        }
    ]
    found.inventory.return_value = (
        {"gmail": "ACTIVE", "googlecalendar": "NOT_CONNECTED"},
        accounts,
    )
    found.disconnect.return_value = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/connections")).status_code == 401
        client.headers["Authorization"] = f"Bearer {token}"
        for response in (
            await client.get("/connections"),
            await client.post("/connections/refresh"),
        ):
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == {
                "statuses": {"gmail": "ACTIVE", "calendar": "NOT_CONNECTED"},
                "accounts": accounts,
                "restart_required": False,
                "refresh_boundary": "next_message",
            }
        disconnected = await client.post("/connections/calendar/disconnect")
        assert disconnected.status_code == 200
        assert disconnected.json()["disconnected"] is True
        found.disconnect.assert_called_once_with("googlecalendar")


async def test_routine_history_pages_keep_review_details_without_reports(setup_api):
    app, token, _, log, runner, _ = setup_api
    store = runner.routines
    assert store is not None
    routine = runner.create_routine(
        feed_id="session",
        title="Review history",
        skill="attention-briefing",
        prompt="Summarize recent messages.",
        cron="0 9 * * *",
    )
    now = datetime.now(UTC)
    occurrence_ids = []
    for number in range(12):
        occ, _ = store.claim_manual_occurrence(routine.routine_id, now=now)
        occurrence_ids.append(occ.occurrence_id)
        if number < 11:
            store.sync_execution(occ.session_id, "failed", error=f"Failure {number}")
    action = {
        "run_id": occ.run_id,
        "call_id": "slack-send",
        "tool": "SLACK_SEND_MESSAGE",
        "args": {"channel": "team", "text": "Status update"},
    }
    log.append(occ.session_id, "tool.call", action)
    log.append(occ.session_id, "action.decided", {**action, "outcome": "approved"})
    returned = {**action, "call_id": "returned"}
    log.append(occ.session_id, "action.decided", {**returned, "outcome": "allowed"})
    log.append(occ.session_id, "tool.result", returned)
    store.sync_execution(
        occ.session_id,
        "needs_review",
        uncertain_tools=[action["tool"]],
        error="Slack delivery could not be confirmed.",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        route = f"/routines/{routine.routine_id}"
        initial = (await client.get(route)).json()
        assert initial["has_more"] is True
        assert len(initial["recent_occurrences"]) == 10
        review = initial["latest_occurrence"]
        assert review["execution_status"] == "needs_review"
        assert review["report_path"] is None
        assert review["error"] == "Slack delivery could not be confirmed."
        assert review["unconfirmed_actions"] == [action]
        assert initial["recent_occurrences"][0] == review
        assert (await client.post(f"{route}/resume")).status_code == 409

        older = (await client.get(f"{route}/occurrences?offset=10&limit=2")).json()
        assert older["has_more"] is False
        assert [run["occurrence_id"] for run in older["occurrences"]] == occurrence_ids[1::-1]
        assert older["occurrences"][-1]["error"] == "Failure 0"
        paged_detail = (await client.get(f"{route}?offset=10&limit=1")).json()
        assert paged_detail["has_more"] is True
        assert paged_detail["latest_occurrence"] == review
        assert paged_detail["recent_occurrences"] == older["occurrences"][:1]
        assert (await client.get(f"{route}/occurrences?offset=-1")).status_code == 422
        assert (await client.get(f"{route}/occurrences?limit=0")).status_code == 422
        resumed = await client.post(f"{route}/resume", json={"acknowledge_uncertain": True})
        assert resumed.json()["status"] == "active"


async def test_pause_stops_a_waiting_run_and_cancels_its_approval(setup_api):
    app, token, _, log, runner, _ = setup_api
    store = runner.routines
    assert store is not None
    routine = runner.create_routine(
        feed_id="session",
        title="Pause while waiting",
        skill="attention-briefing",
        prompt="Send an update.",
        cron="0 9 * * *",
    )
    occ, _ = store.claim_manual_occurrence(routine.routine_id)
    log.append(occ.session_id, "run.queued", {"run_id": occ.run_id, "trigger": "routine"})
    log.append(
        occ.session_id,
        "approval.requested",
        {
            "run_id": occ.run_id,
            "approval_id": "ask-send",
            "call_id": "send",
            "tool": "send_email",
            "args": {"subject": "Update"},
        },
    )
    log.append(occ.session_id, "run.waiting", {"run_id": occ.run_id, "messages": []})
    store.sync_execution(occ.session_id, "awaiting_approval")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        route = f"/routines/{routine.routine_id}"
        detail = (await client.get(route)).json()
        response = await client.post(f"{route}/pause")
        assert response.status_code == 200
        paused = response.json()
        assert paused["status"] == "paused"
        assert paused["next_run_at"] is None
        assert paused["stop_requested"] is True
        assert paused["active_run_stopped"] is True
        assert paused["active_occurrence"] is None
        assert paused["latest_occurrence"]["execution_status"] == "cancelled"
        assert paused["pending_approvals"] == []
        pending = detail["active_occurrence"]["pending_approvals"]
        assert pending[0]["args"] == {"subject": "Update"}
        assert detail["pending_approvals"] == pending
        assert (await client.post(f"{route}/pause")).json()["stop_requested"] is False


async def test_a_changed_account_binding_loads_in_place(setup_api):
    """A reconnect or removal is adopted in the same chat instead of asking for a restart."""
    app, token, found, _, _, _ = setup_api
    binding = AccountBinding("owner", 2, ())
    found.restart_required.side_effect = lambda: found.binding != binding
    found.stored.side_effect = lambda: binding  # Gmail was replaced in the store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        checked = await client.post("/sessions/session/setup/github/check")
    assert checked.status_code != 409
    found.adopt.assert_called_once_with(binding)
    assert found.binding == binding


async def test_cancelling_dismisses_the_setup_and_not_the_active_accounts(setup_api):
    app, token, found, log, _, _ = setup_api
    found.store.cancel_connect.return_value = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        route = "/sessions/session/setup/github/cancel"
        assert (await client.post(route)).status_code == 401
        client.headers["Authorization"] = f"Bearer {token}"
        cancelled = await client.post(route)
        assert cancelled.status_code == 200
        assert cancelled.headers["cache-control"] == "no-store"
        assert cancelled.json()["status"] == "cancelled"
        assert (await client.get("/sessions/session")).json()["pending_setup"] == []
        idempotent = await client.post(route)
        assert idempotent.status_code == 200
        assert idempotent.headers["cache-control"] == "no-store"
    found.store.cancel_connect.assert_called_once_with("github")
    found.connect.assert_not_called()
    assert log.read("session")[-1].data["status"] == "cancelled"


async def test_model_instructions_reflect_apps_connected_after_startup(setup_api):
    app, token, found, log, runner, remote = setup_api
    seen = []
    tools_seen = []

    def respond(messages, info):
        seen.append(info.instructions)
        tools_seen.append({tool.name for tool in info.function_tools})
        return reply()

    subject = runner.harness.agent(
        FunctionModel(respond),
        lambda _: None,
        context=BackgroundCompaction(record=lambda _: None),
    )
    await subject.run("Which apps can you use?")

    def read_slack() -> str:
        return "offline Slack result"

    runner.harness.policy.tools["SLACK_READ"] = "allow"
    remote[0] = FunctionToolset([Tool(read_slack, name="SLACK_READ")])
    found.accounts.return_value = {"slack": "account-slack"}
    # Adding an app changes the binding too: refresh() adopts the additive change.
    found.binding = AccountBinding("owner", 1, (("slack", "account-slack"),))
    found.statuses.return_value = {"slack": "ACTIVE"}
    log.append(
        "session",
        "capability.setup",
        {"capability": "slack", "label": "Slack", "status": "waiting"},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        result = await client.post("/sessions/session/setup/slack/check")
        assert result.status_code == 200
        assert result.json()["status"] == "connected"
    await subject.run("And now?")
    assert "Connected apps: none" in seen[0]
    assert "Connected apps: slack" in seen[1]
    assert "SLACK_READ" not in tools_seen[0]
    assert "SLACK_READ" in tools_seen[1]


async def test_account_mode_works_in_the_home_workspace(setup_api, tmp_path):
    _, _, _, _, runner, _ = setup_api
    home = tmp_path.resolve() / "bud-home" / "workspace"
    seen = []

    def respond(messages, info):
        seen.append(info.instructions)
        return reply()

    subject = runner.harness.agent(
        FunctionModel(respond),
        lambda _: None,
        context=BackgroundCompaction(record=lambda _: None),
    )
    await subject.run("Where do you keep files?")

    assert f"Your workspace is the folder {home} on the machine running the Bud server." in seen[0]
    assert "Local file paths resolve on that machine" in seen[0]
    assert home.is_dir()
    # No per-account workspace any more; the account folder keeps only Bud's own state.
    assert not (tmp_path / "composio" / "sessions" / "scope" / "workspace").exists()
