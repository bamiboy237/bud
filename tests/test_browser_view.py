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

JPEG = b"\xff\xd8\xff\xda fixture \xff\xd9"


@pytest.mark.parametrize(
    ("output", "exit_code", "expected_status", "connected"),
    [
        ('{"extension":{"connected":true},"tabs":[]}', 0, 200, True),
        ('{"relay":{"running":false}}', 0, 200, False),
        ('{"extension":{"connected":false},"tabs":[]}', 0, 200, False),
        ('{"extension":{"connected":true},"tabs":[]}', 1, 502, None),
        ("not json", 0, 502, None),
        ("[" * 2000, 0, 502, None),
        ('{"extension":{"connected":true,"browsers":{}},"tabs":[]}', 0, 502, None),
        ("{}", 0, 502, None),
    ],
)
async def test_view_distinguishes_empty_offline_and_failed_lookup(
    tmp_path, output, exit_code, expected_status, connected
):
    browse = tmp_path / "browse"
    browse.write_text(
        '#!/bin/sh\n[ "$*" = "status --json" ] || exit 2\n'
        f"printf '%s' {shlex.quote(output)}\nprintf 'internal socket detail' >&2\n"
        f"exit {exit_code}\n"
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
    else:
        assert response.json() == {
            "detail": "Couldn’t load Browse tabs. Check the Browse connection."
        }


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
        Path(args[-1]).write_bytes(JPEG)
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
        assert (
            await client.post("/browser/view/control/key", json={"tab": "7", "key": "Enter"})
        ).status_code == 401
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
        assert (
            await client.post("/browser/view/takeover", json={"tab": "7", "interactive": False})
        ).status_code == 202
        await asyncio.wait_for(entered.wait(), timeout=1)

        assert (await client.get("/browser/view")).json() == {
            "private": True,
            "handoff_open": True,
            "tabs": [],
        }
        assert (await client.get("/browser/view/6/image")).status_code == 423
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


@pytest.fixture
def preview_app(tmp_path, monkeypatch):
    consent = browser.Consent(tmp_path / "consent")
    consent.set(True)
    view = BrowserView(
        tmp_path / "private", consent, browser.Relay(tmp_path), lambda: Path("browse")
    )
    app = FastAPI()
    app.include_router(view.routes())
    status = {
        "extension": {"connected": True},
        "tabs": [
            {"tab": 6, "id": "fixture"},
            {"tab": 7},
            {"tab": 8, "title": "private title", "handoff": {"message": "private message"}},
            {"tab": None},
        ],
    }

    def install(command):
        async def dispatch(path, action, *args, timeout):
            if action == "status":
                return json.dumps(status).encode()
            return await command(path, action, *args, timeout=timeout)

        monkeypatch.setattr(view, "_command", dispatch)

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return app, client, view, status, install


async def test_preview_targets_background_tabs_and_rejects_unmanaged_or_private_ids(preview_app):
    _, client, view, _, install = preview_app
    view.active_tab = "7"
    captured = []
    concurrent = asyncio.Event()
    release = asyncio.Event()

    async def command(_, action, *args, timeout):
        assert action == "screenshot"
        tab = json.loads(args[1])["tab"]
        captured.append(tab)
        if len(captured) == 3:
            concurrent.set()
        if len(captured) > 1:
            await release.wait()
        Path(args[-1]).write_bytes(JPEG.replace(b"fixture", str(tab).encode()))
        return b"{}"

    install(command)
    async with client:
        first = await client.get("/browser/view/fixture/image")
        assert first.content == JPEG.replace(b"fixture", b"6")
        first_background = asyncio.create_task(client.get("/browser/view/6/image"))
        second_background = asyncio.create_task(client.get("/browser/view/7/image"))
        try:
            await asyncio.wait_for(concurrent.wait(), timeout=1)
        finally:
            release.set()
            responses = await asyncio.gather(first_background, second_background)
        assert [r.content for r in responses] == [
            JPEG.replace(b"fixture", b"6"),
            JPEG.replace(b"fixture", b"7"),
        ]
        assert [r.status_code for r in responses] == [200, 200]
        assert (await client.get("/browser/view/99/image")).status_code == 404
        assert (await client.get("/browser/view/None/image")).status_code == 404
        assert (await client.get("/browser/view/8/image")).status_code == 423
        inventory = await client.get("/browser/view")
        assert inventory.headers["cache-control"] == "no-store"
        assert [tab["id"] for tab in inventory.json()["tabs"]] == ["6", "7"]
        assert inventory.json()["active_tab"] == "7"
        assert "private title" not in inventory.text and "_tab_ids" not in inventory.text
        assert captured == [6, 6, 7]


async def test_takeover_discards_in_flight_preview_and_closed_handoff_can_resume(preview_app):
    _, client, view, status, install = preview_app
    capturing = asyncio.Event()
    release = asyncio.Event()
    handoff = asyncio.Event()
    closed = asyncio.Event()

    async def command(_, action, *args, timeout):
        if action == "screenshot":
            capturing.set()
            await release.wait()
            Path(args[-1]).write_bytes(JPEG)
        else:
            assert action == "handoff"
            assert release.is_set()
            handoff.set()
            await closed.wait()
            status["tabs"].pop(0)
            return b'{"outcome":"closed","detail":"internal data"}'
        return b"{}"

    install(command)
    async with client:
        unknown = await client.post("/browser/view/takeover", json={"tab": "999"})
        assert unknown.status_code == 404 and not view.private
        image = asyncio.create_task(client.get("/browser/view/6/image"))
        try:
            await asyncio.wait_for(capturing.wait(), timeout=1)
            takeover = await asyncio.wait_for(
                client.post(
                    "/browser/view/takeover", json={"tab": "fixture", "interactive": False}
                ),
                timeout=1,
            )
            assert takeover.status_code == 202 and view.private
            assert not handoff.is_set()
            assert (await client.get("/browser/view/7/image")).status_code == 423
            release.set()
            response = await image
            assert response.status_code == 423 and JPEG not in response.content
            await asyncio.wait_for(handoff.wait(), timeout=1)
            task = view.handoff
            assert task is not None
            closed.set()
            assert await task == {"outcome": "closed"}
            assert view.handoff is None and view.private
            assert (await client.post("/browser/view/resume")).json() == {"private": False}
            assert not view.marker.exists()
            assert (await client.get("/browser/view/6/image")).status_code == 404
        finally:
            release.set()
            closed.set()
            await asyncio.gather(image, return_exceptions=True)
            await view.stop()


async def test_interactive_takeover_controls_one_tab_and_unblocks_agent(preview_app, monkeypatch):
    _, client, view, status, install = preview_app
    status["tabs"][0].update(title="about:blank", url="https://www.youtube.com/?private=data")
    status["tabs"][1].update(title="", url="https://example.test/")
    calls = []

    async def command(_, action, *args, timeout):
        if action == "screenshot":
            assert json.loads(args[1])["tab"] == 6
            Path(args[-1]).write_bytes(JPEG)
        elif args[0] == "--json":
            calls.append((action, json.loads(args[1])))
        else:
            calls.append((action, *args))
        return b"[800, 600]" if action == "eval" else b"{}"

    install(command)
    tools = browser.BrowseTools(Path("browse"), view.consent, view.relay, view)
    agent_read = AsyncMock(return_value="agent can read")
    monkeypatch.setattr(tools, "_call", agent_read)

    async with client:
        result = await client.post("/browser/view/takeover", json={"tab": "fixture"})
        assert result.status_code == 202
        assert result.json() == {
            "private": True,
            "interactive": True,
            "tab": "6",
            "handoff_open": False,
        }
        assert view.marker.exists() and not view.ready.is_set()
        inventory = (await client.get("/browser/view")).json()
        assert inventory["private"] and inventory["interactive"] and inventory["connected"]
        assert not inventory["handoff_open"]
        assert inventory["takeover_tab"] == inventory["active_tab"] == "6"
        assert [tab["title"] for tab in inventory["tabs"]] == ["www.youtube.com", "example.test"]
        assert (await client.get("/browser/view/6/image")).content == JPEG
        assert (await client.get("/browser/view/7/image")).status_code == 423

        async def control(action, tab="6", **args):
            return await client.post(f"/browser/view/control/{action}", json={"tab": tab, **args})

        assert (await control("click", nx=1.1, ny=0.5)).status_code == 422
        assert (await control("navigate", url="file:///tmp/private")).status_code == 422
        assert (await control("key", tab="7", key="Enter")).status_code == 423
        read = asyncio.create_task(
            tools.call_tool("browser_snapshot", {"tab": "6"}, Mock(), Mock())
        )
        try:
            for action, args in [
                ("click", {"nx": 0.25, "ny": 1}),
                ("scroll", {"dx": -5, "dy": 25.5}),
                ("type", {"text": 'hello "there"'}),
                ("key", {"key": "Enter"}),
                ("navigate", {"url": "https://example.test/"}),
            ]:
                assert (await control(action, **args)).json() == {"ok": True}
            assert not read.done()
            agent_read.assert_not_awaited()
            assert calls == [
                ("eval", {"tab": 6, "expression": "[innerWidth, innerHeight]"}),
                ("click", "--tab", "6", "--x", "200", "--y", "599", "--force"),
                ("scroll", "--tab", "6", "--deltaX", "-5.0", "--deltaY", "25.5"),
                ("type", {"tab": 6, "text": 'hello "there"'}),
                ("press", {"tab": 6, "key": "Enter"}),
                ("navigate", {"tab": 6, "url": "https://example.test/"}),
            ]
            status.pop("extension")
            disconnected = (await client.get("/browser/view")).json()
            assert disconnected["takeover_tab"] == "6" and not disconnected["connected"]
            assert (await client.post("/browser/view/resume")).json() == {"private": False}
            assert await asyncio.wait_for(read, timeout=1) == "agent can read"
            assert view.takeover_tab is None and not view.marker.exists()
            assert (await control("type", text="too late")).status_code == 423
        finally:
            read.cancel()
            await asyncio.gather(read, return_exceptions=True)


async def test_resume_cancels_open_chrome_handoff(preview_app):
    _, client, view, _, install = preview_app
    opened = asyncio.Event()

    async def command(_, action, *args, timeout):
        assert action == "handoff"
        opened.set()
        await asyncio.Event().wait()

    install(command)
    async with client:
        await client.post("/browser/view/takeover", json={"tab": "6", "interactive": False})
        try:
            await asyncio.wait_for(opened.wait(), timeout=1)
            task = view.handoff
            assert task is not None
            assert (await client.post("/browser/view/resume")).json() == {"private": False}
            assert await asyncio.wait_for(task, timeout=1) == {"outcome": "cancelled"}
            assert not view.handoff_open and view.ready.is_set() and not view.marker.exists()
        finally:
            await view.stop()


async def test_captured_response_is_discarded_if_private_input_starts_before_send(preview_app):
    app, client, view, _, install = preview_app

    async def command(_, action, *args, timeout):
        assert action == "screenshot"
        Path(args[-1]).write_bytes(JPEG)
        return b"{}"

    install(command)
    capture = next(
        route.endpoint for route in view.routes().routes if route.path.endswith("/{tab}/image")
    )

    @app.get("/delayed-frame")
    async def delayed_frame():
        response = await capture("6")
        await view.enter()
        view.resume()
        return response

    async with client:
        response = await client.get("/delayed-frame")
    assert response.status_code == 423 and JPEG not in response.content


@pytest.mark.parametrize("failure", ["closed", "daemon", "timeout", "png", "truncated", "missing"])
async def test_preview_returns_safe_errors(preview_app, failure):
    _, client, _, status, install = preview_app

    async def command(_, action, *args, timeout):
        assert action == "screenshot"
        if failure == "closed":
            status["tabs"].pop(0)
        if failure in ("closed", "daemon"):
            raise RuntimeError("raw daemon stack")
        if failure == "timeout":
            raise TimeoutError("internal socket timeout")
        if failure in ("png", "truncated"):
            Path(args[-1]).write_bytes(b"\x89PNG\r\n\x1a\n" if failure == "png" else JPEG[:-2])
        return b"{}"

    install(command)
    async with client:
        response = await client.get("/browser/view/6/image")
    if failure == "closed":
        assert response.status_code == 404 and response.json() == {"detail": "Unknown tab."}
    else:
        assert response.status_code == 502
        assert response.json() == {"detail": "Browse could not capture the preview."}


async def test_missing_executable_disables_inventory_and_preview(preview_app, monkeypatch):
    _, client, view, _, _ = preview_app
    monkeypatch.setattr(view, "locate", lambda: None)
    async with client:
        assert (await client.get("/browser/view")).json() == {
            "private": False,
            "enabled": False,
            "tabs": [],
        }
        assert (await client.get("/browser/view/6/image")).status_code == 409
        assert (await client.post("/browser/view/takeover", json={"tab": "6"})).status_code == 409
    assert not view.marker.exists()


async def test_takeover_fails_safely_when_private_marker_cannot_be_written(preview_app):
    _, client, view, _, install = preview_app
    install(AsyncMock(side_effect=AssertionError("Handoff must not start")))
    view.marker.mkdir()
    async with client:
        response = await client.post("/browser/view/takeover", json={"tab": "6"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Bud could not save the private browser pause."}
    assert not view.handoff_open and not view.private
