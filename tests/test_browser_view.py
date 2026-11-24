import asyncio
import json
import shlex
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from bud import browser
from bud.core.auth import OwnerAuthentication
from bud.core.browser_view import BrowserView


@pytest.mark.parametrize(
    ("output", "exit_code", "expected_status", "connected"),
    [
        ('{"extension":{"connected":true},"tabs":[]}', 0, 200, True),
        ('{"relay":{"running":false}}', 0, 200, False),
        ('{"extension":{"connected":false},"tabs":[]}', 0, 200, False),
        ('{"extension":{"connected":true},"tabs":[]}', 1, 502, None),
        ("not json", 0, 502, None),
        ("{}", 0, 502, None),
    ],
)
async def test_view_distinguishes_empty_offline_and_failed_lookup(
    tmp_path, output, exit_code, expected_status, connected
):
    browse = tmp_path / "browse"
    browse.write_text(
        '#!/bin/sh\n[ "$*" = "status --json" ] || exit 2\n'
        f"printf '%s' {shlex.quote(output)}\nexit {exit_code}\n"
    )
    browse.chmod(0o755)
    consent = browser.Consent(tmp_path / "consent.json")
    consent.set(True)
    view = BrowserView(
        tmp_path / "private", consent, browser.Relay(tmp_path / "relay"), lambda: browse
    )
    app = FastAPI()
    app.include_router(view.routes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/browser/view")
    assert response.status_code == expected_status
    if response.is_success:
        assert response.json()["connected"] is connected
        assert response.json()["tabs"] == []


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

    async def command(_, action, *args, timeout):
        if action == "status":
            counts["tabs"] += 1
            return json.dumps(
                {
                    "extension": {
                        "connected": True,
                        "browsers": [
                            {"id": "local", "name": "Your Chrome", "connected": True},
                            {"id": "remote", "name": "Daytona", "connected": True},
                        ],
                    },
                    "tabs": [
                        {
                            "tab": 6,
                            "id": "fixture",
                            "title": "Fixture",
                            "browser": "local",
                            "url": "https://example.test/?secret=omitted",
                        },
                        {
                            "tab": 7,
                            "title": "Other fixture",
                            "browser": "remote",
                            "url": "https://remote.test/",
                        },
                    ],
                }
            ).encode()
        assert args[0] == "--json"
        payload = json.loads(args[1])
        if action == "handoff":
            assert payload["tab"] == 7
            assert payload["timeoutSeconds"] == 600
            entered.set()
            await continue_in_chrome.wait()
            return b'{"outcome":"continued","secret":"ignored"}'
        assert action == "screenshot"
        assert payload == {"tab": 6, "format": "jpeg", "quality": 55}
        assert args[2] == "--output"
        counts["preview"] += 1
        Path(args[-1]).write_bytes(b"\xff\xd8fixture")
        return b"{}"

    async def invoke(*_):
        counts["tool"] += 1
        return "read"

    monkeypatch.setattr(view, "_command", command)
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.get_tools.return_value = dict.fromkeys(
        ("browser_navigate", "browser_snapshot", "browser_tabs"), Mock()
    )
    monkeypatch.setattr(browser.BrowseTools, "_connect", lambda _: connection)
    tools = browser.BrowseTools(browse, consent, relay, view)
    ctx = Mock()

    async def use(name, args, result):
        connection.call_tool.side_effect = None
        connection.call_tool.return_value = result
        return await tools.call_tool(name, args, ctx, Mock())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/browser/view")).status_code == 401
        client.headers["Authorization"] = f"Bearer {token}"
        status = (await client.get("/browser/view")).json()
        assert status["connected"] is True
        assert status["browsers"] == [
            {"id": "local", "name": "Your Chrome"},
            {"id": "remote", "name": "Daytona"},
        ]
        assert status["tabs"] == [
            {"id": "6", "title": "Fixture", "host": "example.test", "browser": "local"},
            {"id": "7", "title": "Other fixture", "host": "remote.test", "browser": "remote"},
        ]
        assert status["active_tab"] is None
        await use("browser_navigate", {"url": "https://remote.test/"}, {"tab": 7})
        assert (await client.get("/browser/view")).json()["active_tab"] == "7"
        result = await client.get("/browser/view/6/image")
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        await use("browser_tabs", {"action": "list"}, {"tabs": [{"tab": 6}]})
        assert (await client.get("/browser/view")).json()["active_tab"] == "7"
        assert (await client.post("/browser/view/takeover", json={"tab": "7"})).status_code == 202
        await asyncio.wait_for(entered.wait(), timeout=1)

        assert (await client.get("/browser/view")).json() == {
            "private": True,
            "handoff_open": True,
            "tabs": [],
        }
        assert (await client.get("/browser/view/6/image")).status_code == 423
        assert (await client.post("/browser/view/resume")).status_code == 409
        assert BrowserView(marker, consent, relay, lambda: browse).private

        calls_before = counts["tabs"]
        connection.call_tool.side_effect = invoke
        read = asyncio.create_task(
            tools.call_tool("browser_snapshot", {"tab": "fixture"}, ctx, Mock())
        )
        await asyncio.sleep(0)
        assert not read.done()
        assert counts == {"tabs": calls_before, "preview": 1, "tool": 0}
        continue_in_chrome.set()
        assert await asyncio.wait_for(read, timeout=1) == "read"
        assert not marker.exists()
        assert counts["tool"] == 1
        assert (await client.get("/browser/view")).json()["active_tab"] == "6"
        await view.stop()
