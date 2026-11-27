"""Offline tests for config.env management, live hot-reload onboarding, and ChatGPT OAuth."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr
from pydantic_ai.providers.openai_chatgpt import OpenAIChatGPTCredentials
from pydantic_ai.toolsets import FunctionToolset

from bud.composio.binding import AccountBinding
from bud.composio.connection import Connections
from bud.config import Settings
from bud.core.onboarding import (
    MANAGED_KEYS,
    ChatGPTLoginManager,
    OnboardingError,
    write_env_updates,
)
from bud.core.runner import Runner
from bud.core.server import _create_app


def _clear_managed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for spec in MANAGED_KEYS:
        monkeypatch.delenv(spec.env_name, raising=False)


def _mock_connections(data_dir: Path, settings: Settings) -> Mock:
    found = Mock(spec=Connections)
    found.settings = settings
    found.folder = data_dir / "composio"
    found.user_id = "owner"
    found.capture_binding.return_value = AccountBinding("owner", 1, ())
    found.binding = found.capture_binding.return_value
    found.stored.side_effect = lambda: found.binding
    found.accounts.return_value = {}
    found.restart_required.return_value = False
    found.store = Mock()
    found.store.session_scope.return_value = "scope"
    return found


def test_write_env_updates_atomic_preserves_comments_and_rejects_unsafe_input(tmp_path: Path):
    env_path = tmp_path / "config.env"
    env_path.write_text(
        "# Keep this comment\nBUD_DAILY_USD=5\nOPENAI_API_KEY=old-key\n",
        encoding="utf-8",
    )

    cleared = write_env_updates(
        env_path,
        {
            "OPENAI_API_KEY": "sk-updated-123",
            "EXA_API_KEY": "exa key with spaces",
        },
    )
    assert cleared == set()
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600
    content = env_path.read_text(encoding="utf-8")
    assert "# Keep this comment\n" in content
    assert "BUD_DAILY_USD=5\n" in content
    assert "OPENAI_API_KEY=sk-updated-123\n" in content
    assert 'EXA_API_KEY="exa key with spaces"\n' in content

    # Passing None or whitespace removes the key without disturbing unrelated lines.
    cleared_two = write_env_updates(
        env_path,
        {"OPENAI_API_KEY": None, "EXA_API_KEY": "   "},
    )
    assert cleared_two == {"OPENAI_API_KEY", "EXA_API_KEY"}
    after_remove = env_path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in after_remove
    assert "EXA_API_KEY" not in after_remove
    assert "BUD_DAILY_USD=5\n" in after_remove

    with pytest.raises(OnboardingError, match="Unsupported configuration key"):
        write_env_updates(env_path, {"PATH": "/tmp/evil"})

    for bad_value in ("line1\nline2", "line1\rline2", "nul\x00byte"):
        with pytest.raises(OnboardingError, match="single line"):
            write_env_updates(env_path, {"OPENAI_API_KEY": bad_value})

    symlink_env = tmp_path / "symlink.env"
    symlink_env.symlink_to(env_path)
    with pytest.raises(OnboardingError, match="symbolic link"):
        write_env_updates(symlink_env, {"OPENAI_API_KEY": "sk-new"})


async def test_unconfigured_server_starts_and_hot_reloads_keys_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _clear_managed_env(monkeypatch)
    bud_home = tmp_path / "bud_home"
    data_dir = bud_home / "data"
    models_file = tmp_path / "models.toml"
    models_file.write_text(
        """
[[models]]
provider = "openai"
name = "gpt-5"
tier = "fast"
input = 0
output = 0
cached = 0

[[models]]
provider = "local"
name = "text-only"
tier = "frontier"
billing = "local"
base_url = "http://localhost:1/v1"
protocol = "chat"
supports_tools = false
allow_accounts = true

[[models]]
provider = "openai-chatgpt"
name = "signed-out"
tier = "frontier"
billing = "subscription"
""",
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        data_dir=data_dir,
        workspace_dir=bud_home / "workspace",
        models_file=models_file,
        approval_review=True,
    )
    found = _mock_connections(data_dir, settings)
    monkeypatch.setattr("bud.core.server.Connections", lambda _: found)
    monkeypatch.setattr("bud.composio.tools.build_toolset", lambda *args: FunctionToolset[None]([]))

    created: list[Runner] = []

    def non_durable_runner(*args, **kwargs):
        kwargs["durable"] = False
        instance = Runner(*args, **kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr("bud.core.server.Runner", non_durable_runner)
    token = "onboarding-test-" + "a" * 43
    app = _create_app(settings, SecretStr(token), None, simulate=False, model_id=None)
    runner = created[0]

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/onboarding")).status_code == 401

            headers = {"Authorization": f"Bearer {token}"}
            available = "openai:gpt-5"
            session_id = (await client.post("/sessions", headers=headers)).json()["session_id"]

            async def assert_model_choices(expected: list[str]) -> None:
                catalog = await client.get("/models", headers=headers)
                assert catalog.status_code == 200
                rows = catalog.json()
                assert [row["id"] for row in rows] == [
                    available,
                    "local:text-only",
                    "openai-chatgpt:signed-out",
                ]
                assert all(row.keys() == {"id", "tier", "billing", "configured"} for row in rows)
                assert [row["id"] for row in rows if row["configured"]] == expected
                onboarding = await client.get("/onboarding", headers=headers)
                assert onboarding.json()["models"] == expected
                # Exercise the durable model table without starting the global DBOS runtime.
                with monkeypatch.context() as patch:
                    patch.setattr(runner, "durable", True)
                    _, registered = runner._build_models()
                assert list(registered) == expected

            async def assert_rejected(model: str, reason: str) -> None:
                rejected_session = "a" * 32
                for method, path in (
                    ("PUT", "/onboarding/model"),
                    ("POST", f"/sessions/{session_id}/model"),
                    ("POST", "/sessions"),
                    ("POST", "/sessions/primary"),
                ):
                    body = {"model": model}
                    if path == "/sessions":
                        body["session_id"] = rejected_session
                    response = await client.request(method, path, headers=headers, json=body)
                    assert response.status_code == 422
                    assert reason in response.json()["detail"]
                missing = await client.get(f"/sessions/{rejected_session}", headers=headers)
                assert missing.status_code == 404

            await assert_model_choices([])
            await assert_rejected(available, "not configured")
            await assert_rejected("local:text-only", "does not support tools")
            await assert_rejected("openai-chatgpt:signed-out", "not configured")

            runtime_before = await client.get("/runtime", headers=headers)
            assert runtime_before.status_code == 200
            assert runtime_before.json()["has_model"] is False
            assert runtime_before.json()["needs_onboarding"] is True

            state_before = await client.get("/onboarding", headers=headers)
            assert state_before.status_code == 200
            before_json = state_before.json()
            assert before_json["needs_onboarding"] is True
            assert all(not entry["configured"] for entry in before_json["keys"].values())

            secret_openai = "sk-openai-live-secret-999"
            secret_exa = "exa-secret-123"
            secret_composio = "comp-secret-456"
            update_resp = await client.post(
                "/onboarding/keys",
                headers=headers,
                json={
                    "keys": {
                        "OPENAI_API_KEY": secret_openai,
                        "EXA_API_KEY": secret_exa,
                        "COMPOSIO_API_KEY": secret_composio,
                    }
                },
            )
            assert update_resp.status_code == 200
            assert update_resp.headers["cache-control"] == "no-store"
            # Secrets must never be echoed back in responses.
            for secret in (secret_openai, secret_exa, secret_composio):
                assert secret not in update_resp.text

            updated = update_resp.json()
            assert updated["has_model"] is True
            assert updated["needs_onboarding"] is False
            assert updated["keys"]["OPENAI_API_KEY"]["configured"] is True
            assert updated["keys"]["EXA_API_KEY"]["configured"] is True
            assert updated["keys"]["COMPOSIO_API_KEY"]["configured"] is True

            config_env = bud_home / "config.env"
            assert config_env.is_file()
            assert stat.S_IMODE(config_env.stat().st_mode) == 0o600

            runtime_after = await client.get("/runtime", headers=headers)
            assert runtime_after.status_code == 200
            assert runtime_after.json()["has_model"] is True
            assert runtime_after.json()["needs_onboarding"] is False

            assert runner.is_configured is True
            assert runner.harness.reviewer is not None
            assert runner.harness._guards[0].reviewer is runner.harness.reviewer
            assert found.settings.composio_api_key is not None
            assert found.settings.composio_api_key.get_secret_value() == secret_composio

            assert updated["models"] == [available]
            await assert_model_choices([available])
            primary = await client.post(
                "/sessions/primary",
                headers=headers,
                json={"session_id": session_id, "model": available},
            )
            assert primary.status_code == 200
            assert primary.json() == {"session_id": session_id}
            switched = await client.post(
                f"/sessions/{session_id}/model", headers=headers, json={"model": available}
            )
            assert switched.json() == {"session_id": session_id, "model": available}
            selected = await client.put(
                "/onboarding/model", headers=headers, json={"model": available}
            )
            assert selected.status_code == 200
            assert selected.json()["default_model"] == available
            assert runner._inputs("example", "new message")["owner_model"] == available
            await assert_rejected("nonexistent:model", "Unknown model")
            await assert_rejected("local:text-only", "does not support tools")
            assert runner.session_models[session_id] == available
            assert (data_dir / "default-model").read_text() == available
            automatic = await client.put("/onboarding/model", headers=headers, json={"model": None})
            assert automatic.json()["default_model"] is None
            assert runner._inputs("example", "another message")["owner_model"] is None
            assert (
                await client.put("/onboarding/model", headers=headers, json={"model": available})
            ).status_code == 200

            remove_resp = await client.post(
                "/onboarding/keys",
                headers=headers,
                json={"keys": {"OPENAI_API_KEY": None}},
            )
            assert remove_resp.status_code == 200
            assert remove_resp.json()["has_model"] is False
            assert remove_resp.json()["needs_onboarding"] is True
            assert remove_resp.json()["default_model"] is None
            await assert_model_choices([])
            await assert_rejected(available, "not configured")
            assert runner.is_configured is False
            assert runner.harness.reviewer is None
            assert runner.harness._guards[0].reviewer is None
    finally:
        runner.gateway.close()


def test_durable_runner_reload_gateway_updates_dbos_model_table(tmp_path: Path):
    """Run durable=True in a fresh subprocess so DBOS's global registry stays isolated."""
    driver = tmp_path / "durable_reload_driver.py"
    driver.write_text(
        """
import json
import sys
from pathlib import Path

from pydantic_ai.models.function import FunctionModel

from bud.config import Settings
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.runner import Runner
from bud.gateway.ledger import Ledger
from bud.gateway.registry import ModelSpec, Registry
from bud.gateway.router import Gateway
from bud.harness.actions import Policy
from bud.harness.agent import Harness
from tests.runner_support import reply

tmp = Path(sys.argv[1])
settings = Settings(_env_file=None, data_dir=tmp / "data", workspace_dir=tmp / "workspace")
spec = ModelSpec(
    provider="openai", name="gpt-reload-test", tier="fast", input=0, output=0, cached=0
)
empty_gw = Gateway(Registry(models=[]), Ledger(tmp / "ledger.db"), lambda _: None)
harness = Harness(
    [],
    Policy(tools={}, defaults="allow"),
    empty_gw.registry,
    "Test profile",
    state_dir=tmp / "state",
    result_path=tmp / "results.db",
    read_tools=set(),
)
runner = Runner(
    Hub(EventLog(tmp / "bud.db")),
    empty_gw,
    harness,
    durable=True,
    jobs=False,
    allow_unconfigured=True,
)
assert runner.is_configured is False
assert runner._durability is not None
assert spec.id not in runner._durability._models_by_id
assert runner._durability._models_by_id["default"] is runner.agent.model

live_gw = Gateway(
    Registry(models=[spec]),
    Ledger(tmp / "ledger.db"),
    lambda _: FunctionModel(lambda *_: reply()),
)
runner.reload_gateway(live_gw)
assert runner.is_configured is True
assert spec.id in runner.runtime_model_ids
assert runner._durability._models_by_id["default"] is runner.agent.model
assert spec.id in runner._durability._models_by_id
print(json.dumps({"ok": True, "active": runner.active_model_id}))
""",
        encoding="utf-8",
    )
    repo_root = Path(__file__).resolve().parent.parent
    env = {
        **os.environ,
        "PYTHONPATH": str(repo_root),
        "BUD_HOME": str(tmp_path / "isolated-bud"),
    }
    proc = subprocess.run(
        [sys.executable, str(driver), str(tmp_path / "run")],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["active"] == "openai:gpt-reload-test"


async def test_chatgpt_login_manager_start_complete_and_logout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _clear_managed_env(monkeypatch)
    bud_home = tmp_path / "bud_chatgpt"
    data_dir = bud_home / "data"
    settings = Settings(
        _env_file=None,
        data_dir=data_dir,
        workspace_dir=bud_home / "workspace",
        approval_review=True,
    )
    found = _mock_connections(data_dir, settings)
    monkeypatch.setattr("bud.core.server.Connections", lambda _: found)
    monkeypatch.setattr("bud.composio.tools.build_toolset", lambda *args: FunctionToolset[None]([]))

    created: list[Runner] = []

    def non_durable_runner(*args, **kwargs):
        kwargs["durable"] = False
        instance = Runner(*args, **kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr("bud.core.server.Runner", non_durable_runner)
    token = "chatgpt-test-" + "b" * 43
    app = _create_app(settings, SecretStr(token), None, simulate=False, model_id=None)
    runner = created[0]

    loop = asyncio.get_running_loop()
    callback_future: asyncio.Future[OpenAIChatGPTCredentials] = loop.create_future()

    class FakeFlow:
        def __init__(self, **_kwargs):
            self.client_id = "fake-client-id"

        def authorization_url(self, **_kwargs) -> str:
            return "https://auth.openai.com/oauth/authorize?client_id=fake-client-id"

        async def exchange_code_from_callback(self) -> OpenAIChatGPTCredentials:
            return await callback_future

    async def noop_wait(_task):
        return None

    async def fake_refresh(store):
        store.acquire()
        try:
            state = store.state()
            state["models"] = [{"slug": "gpt-test", "display_name": "GPT Test"}]
            state["selected_model"] = "gpt-test"
            store.save_state(state)
        finally:
            store.close()
        return state["models"]

    manager = ChatGPTLoginManager(
        data_dir,
        on_before_auth=lambda: runner.gateway.close(),
        on_complete=app.state.reload_runtime_config,
        flow_factory=FakeFlow,
        wait_listener=noop_wait,
        refresh_fn=fake_refresh,
    )
    app.state.chatgpt_login = manager

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": f"Bearer {token}"}
            started = await client.post("/onboarding/chatgpt/start", headers=headers)
            assert started.status_code == 200
            start_json = started.json()
            assert start_json["chatgpt"]["status"] == "waiting"
            assert start_json["chatgpt"]["url"].startswith("https://auth.openai.com/")

            callback_future.set_result(
                OpenAIChatGPTCredentials(
                    client_id="fake-client-id",
                    access_token="access-tok",
                    refresh_token="refresh-tok",
                    scope="openid profile email chatgpt.tokens.use.direct",
                )
            )
            for _ in range(40):
                state_resp = await client.get("/onboarding", headers=headers)
                if state_resp.json()["chatgpt"]["connected"]:
                    break
                await asyncio.sleep(0.02)

            done_json = state_resp.json()
            assert done_json["chatgpt"]["connected"] is True
            assert done_json["chatgpt"]["status"] == "connected"
            assert done_json["has_model"] is True
            assert done_json["needs_onboarding"] is False
            assert done_json["active_model"] == "openai-chatgpt:gpt-test"
            assert runner.is_configured is True

            logged_out = await client.post("/onboarding/chatgpt/logout", headers=headers)
            assert logged_out.status_code == 200
            out_json = logged_out.json()
            assert out_json["chatgpt"]["connected"] is False
            assert out_json["has_model"] is False
            assert out_json["needs_onboarding"] is True
            assert runner.is_configured is False
    finally:
        await manager.shutdown()
        runner.gateway.close()


def test_blank_openai_key_does_not_initialize_embedder_reviewer_or_verifier(
    monkeypatch: pytest.MonkeyPatch,
):
    from bud.harness.recall import embedder
    from bud.harness.review import build_reviewer
    from bud.harness.verify import build_verifier

    blank = Settings(
        _env_file=None,
        approval_review=True,
        openai_api_key=SecretStr("   "),
    )
    assert embedder(blank) is None
    assert build_reviewer(blank) is None
    monkeypatch.setattr("bud.config.get_settings", lambda: blank)
    assert build_verifier() is None
