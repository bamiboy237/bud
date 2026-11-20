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

    assert f"Your workspace is the folder {home} on the person's Mac." in seen[0]
    assert home.is_dir()
    # No per-account workspace any more; the account folder keeps only Bud's own state.
    assert not (tmp_path / "composio" / "sessions" / "scope" / "workspace").exists()
