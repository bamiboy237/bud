from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
from fastapi import FastAPI

from bud.browser import Consent, Pairing, Relay
from bud.core.browser_computers import BrowserComputers
from bud.core.browser_view import BrowserView


async def test_daytona_pairs_with_private_address_without_enabling_browser_access(
    tmp_path: Path, monkeypatch
):
    relay = Relay(tmp_path / "browse")
    pairing = Pairing(relay)
    consent = Consent(tmp_path / "consent")
    view = BrowserView(tmp_path / "private", consent, relay, lambda: Path("/test/browse"))
    mint = Mock(return_value="TEST-CODE")
    monkeypatch.setattr(pairing, "code", mint)
    inventory = AsyncMock(
        return_value={
            "browsers": [{"id": "mac", "name": "Chrome"}, {"id": "vm", "name": "Daytona"}],
            "tabs": [{"private_page": "not for Settings"}],
        },
    )
    monkeypatch.setattr(view, "_inventory", inventory)
    app = FastAPI()
    app.include_router(BrowserComputers(view, pairing, "100.64.1.2").routes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.post("/browser/computers/pair", json={"browser": "remote"})
        assert response.json() == {"code": "TEST-CODE", "address": "100.64.1.2:19989"}
        assert response.headers["cache-control"] == "no-store"
        assert not consent.on()
        assert (await c.get("/browser/computers")).json() == {
            "browsers": [{"id": "mac", "name": "Chrome"}, {"id": "vm", "name": "Daytona"}],
            "available": True,
            "private_network": True,
        }
        view.enter()
        assert (
            await c.post("/browser/computers/pair", json={"browser": "remote"})
        ).status_code == 423
        assert (await c.get("/browser/computers")).json()["browsers"] == []
        mint.assert_called_once()


async def test_pairing_rejects_public_peer_but_still_allows_a_local_browser(
    tmp_path: Path, monkeypatch
):
    relay = Relay(tmp_path / "browse")
    pairing = Pairing(relay)
    mint = Mock(return_value="TEST-CODE")
    monkeypatch.setattr(pairing, "code", mint)
    view = BrowserView(
        tmp_path / "private", Consent(tmp_path / "consent"), relay, lambda: Path("/test/browse")
    )
    app = FastAPI()
    app.include_router(BrowserComputers(view, pairing, "8.8.8.8").routes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (
            await c.post("/browser/computers/pair", json={"browser": "remote"})
        ).status_code == 409
        assert not mint.called
        local = await c.post("/browser/computers/pair", json={"browser": "local"})
        assert local.json()["address"] == "127.0.0.1:19989"
