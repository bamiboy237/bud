from fastapi.testclient import TestClient

from bud.composio.connection import Connections
from bud.config import get_settings
from bud.core import server
from bud.core.auth import owner_token
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.routines import RoutineStore
from bud.core.runner import Runner
from bud.core.server import create_app


def test_staged_runtime_cannot_run_until_committed(tmp_path, monkeypatch):
    from bud.release.runtime_move import pack_runtime

    def non_durable_runner(*args, **kwargs):
        kwargs["durable"] = False
        return Runner(*args, **kwargs)

    monkeypatch.setattr(server, "Runner", non_durable_runner)
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
    log.append(chat, "user.message", {"text": "This conversation follows Bud"})
    routine = RoutineStore(Hub(log)).create(
        feed_id=chat,
        title="Weekly update",
        skill="weekly-update",
        prompt="Summarize the week.",
        cron="0 12 * * 1",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-key")
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir.joinpath("default-model").write_text("deepseek:deepseek-flash")
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
    finally:
        get_settings.cache_clear()
