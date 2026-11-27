import asyncio
import json
from hashlib import pbkdf2_hmac
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from bud.browser import Consent, Pairing, Relay
from bud.core.browser_computers import BrowserComputers
from bud.core.browser_view import BrowserView


async def test_browser_inventory_confirms_only_the_browser_that_used_the_issued_code(
    tmp_path: Path, monkeypatch
):
    relay = Relay(tmp_path / "browse")
    relay.home.mkdir()
    pairing = Pairing(relay)
    code = "AB12-CD34"
    key = pbkdf2_hmac("sha256", b"AB12CD34", b"opencode-browser-pairing-v3|code", 100_000).hex()
    codes = relay.home / "pairing-codes.json"
    codes.write_text(
        json.dumps(
            {
                "codes": [
                    {"key": key, "receipt": "issued-receipt", "expiresAt": 4_102_444_800_000},
                    {"key": "0" * 64, "receipt": "other-receipt", "expiresAt": 4_102_444_800_000},
                ]
            }
        )
    )
    monkeypatch.setattr(pairing, "code", Mock(side_effect=[code, "EF56-GH78"]))
    view = BrowserView(
        tmp_path / "private", Consent(tmp_path / "consent"), relay, lambda: Path("/test/browse")
    )
    browsers = [{"id": "other", "name": "Chrome"}]
    monkeypatch.setattr(view, "_inventory", AsyncMock(return_value={"browsers": browsers}))
    tokens = relay.home / "browser-tokens.json"
    entries = {
        "other-profile": {
            "token": "X" * 43,
            "receipt": "other-receipt",
            "pairedAt": 1,
            "lastSeen": 2,
            "id": "other",
            "name": "Chrome",
        },
        "vm-profile": {"token": "Y" * 43, "receipt": "issued-receipt", "pairedAt": 1},
    }
    tokens.write_text(json.dumps(entries))
    app = FastAPI()
    app.include_router(BrowserComputers(view, pairing, None).routes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        issued = await client.post("/browser/computers/pair", json={"browser": "local"})
        assert issued.json() == {"code": code, "address": "127.0.0.1:19989"}

        codes.unlink()  # The code has been redeemed, but the browser has not connected yet.
        waiting = (await client.get("/browser/computers")).json()
        assert waiting["browsers"] == browsers
        assert "pairing" not in waiting

        entries["vm-profile"].update(lastSeen=2, id="vm", name="Chrome")
        tokens.write_text(json.dumps(entries))
        browsers.append({"id": "vm", "name": "Chrome"})
        confirmed = await client.get("/browser/computers")
        assert confirmed.headers["cache-control"] == "no-store"
        assert confirmed.json() == {
            **waiting,
            "browsers": browsers,
            "pairing": {"code": code, "id": "vm", "name": "Chrome"},
        }

        await view.enter()
        assert "pairing" not in (await client.get("/browser/computers")).json()
        view.resume()
        assert (await client.get("/browser/computers")).json() == confirmed.json()

        await client.post("/browser/computers/pair", json={"browser": "local"})
        assert "pairing" not in (await client.get("/browser/computers")).json()


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
