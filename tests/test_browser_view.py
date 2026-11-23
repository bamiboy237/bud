import asyncio
from pathlib import Path
from unittest.mock import Mock

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from bud import browser
from bud.core.auth import OwnerAuthentication
from bud.core.browser_view import BrowserView


async def test_private_input_blocks_preview_agent_reads_and_survives_restart(tmp_path, monkeypatch):
    consent = browser.Consent(tmp_path / "consent.json")
    consent.set(True)
    marker = tmp_path / "private"
    relay = browser.Relay(tmp_path / "browse")
    browse = Path("browse")
    view = BrowserView(marker, consent, relay, lambda: browse)
    app = FastAPI()
    app.include_router(view.routes())
    token = "a" * 43
    app.add_middleware(OwnerAuthentication, token=SecretStr(token))

    counts = {"tabs": 0, "preview": 0, "tool": 0}
    entered = asyncio.Event()
    continue_in_chrome = asyncio.Event()

    async def tabs(*_):
        counts["tabs"] += 1
        return [{"id": "6", "title": "Fixture", "url": "https://example.test/?secret=omitted"}]

    async def command(_, action, *args, timeout):
        if action == "handoff":
            entered.set()
            await continue_in_chrome.wait()
            return b'{"outcome":"continued","secret":"ignored"}'
        counts["preview"] += 1
        Path(args[-1]).write_bytes(b"\xff\xd8fixture")
        return b"{}"

    async def invoke(*_):
        counts["tool"] += 1
        return "read"

    monkeypatch.setattr(browser, "_tabs", tabs)
    monkeypatch.setattr(view, "_command", command)
    monkeypatch.setattr(browser.BrowseTools, "_call", invoke)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/browser/view")).status_code == 401
        client.headers["Authorization"] = f"Bearer {token}"
        status = (await client.get("/browser/view")).json()
        assert status["tabs"] == [{"id": "6", "title": "Fixture", "host": "example.test"}]
        result = await client.get("/browser/view/6/image")
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert (await client.post("/browser/view/takeover", json={"tab": "6"})).status_code == 202
        await asyncio.wait_for(entered.wait(), timeout=1)

        assert (await client.get("/browser/view")).json() == {
            "private": True,
            "handoff_open": True,
            "tabs": [],
        }
        assert (await client.get("/browser/view/6/image")).status_code == 423
        assert (await client.post("/browser/view/resume")).status_code == 409
        assert BrowserView(marker, consent, relay, lambda: browse).private

        tools = browser.BrowseTools(browse, consent, relay, view)
        read = asyncio.create_task(tools.call_tool("browser_snapshot", {"tab": 6}, Mock(), Mock()))
        await asyncio.sleep(0)
        assert not read.done()
        assert counts == {"tabs": 1, "preview": 1, "tool": 0}
        continue_in_chrome.set()
        assert await asyncio.wait_for(read, timeout=1) == "read"
        assert not marker.exists()
        assert counts["tool"] == 1
        await view.stop()
