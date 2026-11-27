import asyncio
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
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
        await view.enter()
        async with view.lock:
            blocked = await asyncio.wait_for(
                c.post("/browser/computers/pair", json={"browser": "remote"}), timeout=1
            )
        assert blocked.status_code == 423
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


async def test_pairing_reports_unusable_relay_home_without_leaking_its_path(tmp_path):
    relay = Relay(tmp_path / "blocked")
    relay.home.touch()
    pairing = Pairing(relay)
    view = BrowserView(
        tmp_path / "private", Consent(tmp_path / "consent"), relay, lambda: Path("/test/browse")
    )
    app = FastAPI()
    app.include_router(BrowserComputers(view, pairing, None).routes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/browser/computers/pair", json={"browser": "local"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Browse could not start pairing. Try again."}
    assert not pairing.waiting()


async def test_cancelled_pair_request_holds_browser_lock_until_pairing_reaches_code(
    tmp_path, monkeypatch
):
    relay = Relay(tmp_path / "browse")
    pairing = Pairing(relay)
    view = BrowserView(
        tmp_path / "private", Consent(tmp_path / "consent"), relay, lambda: Path("/test/browse")
    )
    loop = asyncio.get_running_loop()
    starting = asyncio.Event()
    finish = Event()

    def code(_):
        loop.call_soon_threadsafe(starting.set)
        assert finish.wait(timeout=2)
        return "TEST-CODE"

    monkeypatch.setattr(pairing, "code", code)
    app = FastAPI()
    app.include_router(BrowserComputers(view, pairing, None).routes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.post("/browser/computers/pair", json={"browser": "local"})
        )
        try:
            await asyncio.wait_for(starting.wait(), timeout=1)
            task.cancel()
            await asyncio.sleep(0)
            assert view.lock.locked()
        finally:
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert not view.lock.locked()
