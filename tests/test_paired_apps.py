import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bud.config import get_settings
from bud.core.auth import OwnerAuthentication, owner_token
from bud.core.handoff import DesktopHandoffs
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.paired_apps import MAC_WORK_PATH, PairedApps


async def test_mac_worker_is_scoped_and_cannot_reclaim_a_write(tmp_path):
    token = owner_token(get_settings())
    hub = Hub(EventLog(tmp_path / "bud.db"))
    handoffs = DesktopHandoffs(tmp_path / "handoffs.db", tmp_path, hub)
    apps = PairedApps(handoffs)
    app = FastAPI()
    app.include_router(handoffs.routes())
    app.include_router(apps.routes())
    app.add_middleware(OwnerAuthentication, token=token, desktop_worker=handoffs.authorized)
    headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
    with TestClient(app, headers=headers) as owner:
        first = owner.post("/macs", json={"label": "My Mac"}).json()
        second = owner.post("/macs", json={"label": "Other Mac"}).json()
        worker = {"Authorization": f"Bearer {first['token']}"}
        other = {"Authorization": f"Bearer {second['token']}"}
        assert owner.get("/runtime", headers=worker).status_code == 401
        assert owner.post(MAC_WORK_PATH, headers=worker).json() == {}
        assert owner.get("/macs").json()["macs"][0]["online"] is True
        task = asyncio.create_task(apps.dispatch(first["id"], "notes_create", ["Plan", "Draft"]))
        await asyncio.sleep(0)
        assert owner.post(MAC_WORK_PATH, headers=other).json() == {}
        work = owner.post(MAC_WORK_PATH, headers=worker).json()
        assert work["action"] == "notes_create" and work["args"] == ["Plan", "Draft"]
        assert owner.post(MAC_WORK_PATH, headers=worker).json() == {}
        receipt = {"id": work["id"], "state": "ok", "text": "Created."}
        assert owner.put(MAC_WORK_PATH, headers=other, json=receipt).status_code == 204
        assert not task.done()
        assert owner.put(MAC_WORK_PATH, headers=worker, json=receipt).status_code == 204
        assert await task == {"ok": True, "result": "Created.", "mac": "My Mac"}

        document = b"Only the selected Mac receives this."
        task = asyncio.create_task(
            apps.dispatch(first["id"], "open_in_app", ["report.txt"], document)
        )
        await asyncio.sleep(0)
        work = owner.post(MAC_WORK_PATH, headers=worker).json()
        file_route = f"{MAC_WORK_PATH}/{work['id']}/file"
        assert owner.get(file_route, headers=other).status_code == 404
        assert owner.get(file_route, headers=worker).content == document
        owner.put(MAC_WORK_PATH, headers=worker, json={"id": work["id"], "state": "ok", "text": ""})
        assert (await task)["ok"]
        assert owner.get(file_route, headers=worker).status_code == 404
        assert owner.delete(f"/desktops/{first['id']}").status_code == 204
        assert owner.post(MAC_WORK_PATH, headers=worker).status_code == 401
