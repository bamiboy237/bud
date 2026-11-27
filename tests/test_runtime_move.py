from time import monotonic, sleep

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart
from pydantic_ai.profiles import ModelProfile

from bud.composio.connection import Connections
from bud.config import get_settings
from bud.core import server
from bud.core.auth import owner_token
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.routines import RoutineStore
from bud.core.runner import Runner
from bud.core.server import create_app
from tests.runner_support import reply, scripted_model
from tests.test_attachments import PNG


def _wait_for_run(client, chat, run_count, status="completed"):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        snapshot = client.get(f"/sessions/{chat}").raise_for_status().json()
        recent = snapshot["recent_runs"]
        if len(recent) >= run_count and not snapshot["run_active"]:
            assert recent[-1]["status"] == status, recent[-1]
            return snapshot["transcript"][-1]["text"]
        sleep(0.025)
    pytest.fail("The conversation did not reach the expected run count.")


@pytest.mark.parametrize("failed_turn", [False, True], ids=["snapshot", "event-history"])
def test_staged_runtime_resumes_chat_only_after_commit(tmp_path, monkeypatch, failed_turn):
    from bud.release.runtime_move import pack_runtime

    def non_durable_runner(*args, **kwargs):
        kwargs["durable"] = False
        return Runner(*args, **kwargs)

    def respond(messages, _info):
        if any(
            isinstance(part, UserPromptPart)
            and isinstance(part.content, str)
            and part.content.startswith("Simulate a failed turn.")
            for part in messages[-1].parts
        ):
            raise RuntimeError("Synthetic request failure.")
        images = [
            item.data
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, list)
            for item in part.content
            if isinstance(item, BinaryContent)
        ]
        return reply("I can see the original image." if images == [PNG] else "Image unavailable.")

    build_gateway = server.build_gateway

    def offline_gateway(*args, **kwargs):
        gateway = build_gateway(*args, **kwargs)
        gateway.build_model = lambda _: scripted_model(
            respond, profile=ModelProfile(supports_image_input=True)
        )
        return gateway

    monkeypatch.setattr(server, "Runner", non_durable_runner)
    monkeypatch.setattr(server, "build_gateway", offline_gateway)
    source = tmp_path / "source"
    archive = tmp_path / "move.zip"
    monkeypatch.delenv("DATA_DIR")
    monkeypatch.delenv("WORKSPACE_DIR")
    monkeypatch.setenv("BUD_HOME", str(source))
    get_settings.cache_clear()
    settings = get_settings()
    connections = Connections(settings)
    scope = connections.store.session_scope(connections.stored())
    log = EventLog(connections.folder / "sessions" / scope / "bud.db")
    chat = log.primary_session()
    routines = RoutineStore(Hub(log))
    routine = routines.create(
        feed_id=chat,
        title="Weekly update",
        skill="weekly-update",
        prompt="Summarize the week.",
        cron="0 12 * * 1",
    )
    occurrence, _ = routines.claim_manual_occurrence(routine.routine_id)
    report = source / "workspace" / "reports" / "weekly.md"
    report.parent.mkdir(parents=True)
    report.write_text("The report from before the move.")
    routines.sync_execution(occurrence.session_id, "completed")
    routines.confirm_delivery(occurrence.occurrence_id, report_path=str(report))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-key")
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir.joinpath("default-model").write_text("deepseek:deepseek-flash")
    attachment = {"id": "c" * 32, "name": "test.png"}
    route_attachment = f"/sessions/{chat}/attachments/{attachment['id']}"
    route_message = f"/sessions/{chat}/messages"
    source_token = owner_token(settings).get_secret_value()
    with TestClient(create_app(), headers={"Authorization": f"Bearer {source_token}"}) as local:
        assert (
            local.put(route_attachment, params={"name": "test.png"}, content=PNG).status_code == 204
        )
        message = {"text": "Inspect this image.", "attachments": [attachment]}
        assert local.post(route_message, json=message).status_code == 202
        assert _wait_for_run(local, chat, 1) == "I can see the original image."
        if failed_turn:
            failure = local.post(route_message, json={"text": "Simulate a failed turn."})
            assert failure.status_code == 202
            _wait_for_run(local, chat, 2, status="failed")

    pack_runtime(settings, archive)
    monkeypatch.delenv("DEEPSEEK_API_KEY")

    monkeypatch.setenv("BUD_HOME", str(tmp_path / "server"))
    monkeypatch.setenv("BUD_RECEIVE_MOVE", "true")
    get_settings.cache_clear()
    token = owner_token(get_settings()).get_secret_value()
    headers = {"Authorization": f"Bearer {token}"}
    route = "/runtime-move/" + "a" * 32
    try:
        with TestClient(create_app(), headers=headers) as receiver:
            assert receiver.get("/runtime").json()["mode"] == "receiving"
            assert receiver.put(route, content=archive.read_bytes()).json()["state"] == "staged"
            assert receiver.post("/sessions/primary").status_code == 404
            cancelled = "/runtime-move/" + "b" * 32
            assert receiver.delete(cancelled).json()["state"] == "cancelled"
            assert receiver.put(cancelled, content=archive.read_bytes()).status_code == 409
            assert receiver.post(f"{cancelled}/activate").status_code == 409

        monkeypatch.delenv("BUD_RECEIVE_MOVE")
        get_settings.cache_clear()
        with TestClient(create_app(), headers=headers) as receiver:
            assert receiver.get(route).json()["state"] == "staged"
            assert receiver.post(f"{route}/activate").json()["state"] == "committed"
            assert receiver.delete(route).status_code == 409

        get_settings.cache_clear()
        with TestClient(create_app(), headers=headers) as active:
            assert active.get("/runtime").json()["received_move_id"] == "a" * 32
            assert active.get(f"/sessions/{chat}").status_code == 200
            assert active.get("/onboarding").json()["default_model"] == "deepseek:deepseek-flash"
            view = active.get(f"/routines/{routine.routine_id}").json()
            assert view["status"] == "active"
            response = active.get(
                f"/routines/{routine.routine_id}/occurrences/{occurrence.occurrence_id}/report"
            )
            assert response.status_code == 200, response.text
            assert response.json()["content"] == "The report from before the move."
            assert active.get(route_attachment, params={"name": "test.png"}).content == PNG
            assert active.post(route_message, json={"text": "Look at it again."}).status_code == 202
            assert _wait_for_run(active, chat, 2 + failed_turn) == "I can see the original image."
    finally:
        get_settings.cache_clear()


def test_activation_confirmation_waits_for_proxy_restart():
    from bud.release.runtime_move_client import _wait_for_runtime

    transfer = "d" * 32
    responses = iter(
        (
            httpx.Response(502),
            httpx.Response(200, json={"mode": "receiving"}),
            httpx.Response(200, json={"mode": "accounts", "received_move_id": transfer}),
        )
    )
    with httpx.Client(
        base_url="https://bud.example.test",
        transport=httpx.MockTransport(lambda request: next(responses)),
    ) as client:
        _wait_for_runtime(client, transfer)
