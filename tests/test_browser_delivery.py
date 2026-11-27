"""Cross-worker browser jobs hold approvals across restart and never reissue a submit."""

import base64
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from bud.browser import Consent, Relay
from bud.core import browser_worker
from bud.core.browser_delivery import BrowserForm, local_routes
from bud.core.browser_view import BrowserView
from bud.core.handoff import WORKER_PATH
from bud.core.handoff_client import work_once
from tests.test_server import running

FORM = {
    "url": "https://portal.example.test/deliver",
    "file_input": "invoice-file",
    "reference_input": "reference",
    "submit_button": "submit-form",
    "receipt": "receipt-number",
}


@pytest.mark.parametrize("browser_count", [0, 1, 2])
async def test_prepare_requires_one_browser(tmp_path, monkeypatch, browser_count):
    consent = Consent(tmp_path / "consent")
    consent.set(True)
    view = BrowserView(tmp_path / "private", consent, Relay(tmp_path), lambda: Path("browse"))
    opened = []

    async def command(_, action, *args, timeout):
        if action == "status":
            browsers = [
                {"id": f"browser-{index}", "connected": True} for index in range(browser_count)
            ]
            return json.dumps(
                {"extension": {"connected": bool(browsers), "browsers": browsers}, "tabs": []}
            ).encode()
        if action == "tabs":
            assert args[0] == "--json"
            opened.append(json.loads(args[1])["browser"])
            return b"{}"
        assert action == "execute"
        return b'{"state":"ready"}'

    monkeypatch.setattr(view, "_command", command)
    app = FastAPI()
    app.include_router(local_routes(view))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/browser/desktop-delivery",
            json={"step": "d" * 32, "form": FORM, "action": "prepare"},
        )
    assert response.json() == {"state": "ready" if browser_count == 1 else "unavailable"}
    assert opened == (["browser-0"] if browser_count == 1 else [])


@pytest.mark.parametrize(
    ("states", "actions", "outcome"),
    [
        (["ready", "ready", "ready", "delivered"], ["fill", "upload", "click"], "delivered"),
        (["ready", "ready", "delivered", "delivered"], ["fill", "upload"], "delivered"),
        (["ready", "other_page"], ["fill"], "uncertain"),
        (["unavailable"], [], "unavailable"),
        (["ready", "ready", "ready", "unavailable"], ["fill", "upload", "click"], "unavailable"),
    ],
)
async def test_local_submit_uses_browse_json_and_rechecks_form(
    tmp_path, monkeypatch, states, actions, outcome
):
    consent = Consent(tmp_path / "consent")
    consent.set(True)
    view = BrowserView(tmp_path / "private", consent, Relay(tmp_path), lambda: Path("browse"))
    step = "d" * 32
    content = b"synthetic document"
    attachment = {"id": "a" * 32, "name": "test invoice.txt"}
    observed = []
    copies = []
    inspections = iter(states)

    async def command(_, action, *args, timeout):
        if action == "execute":
            return b"tab: 7\n" + json.dumps({"state": next(inspections)}).encode()
        flag, encoded = args
        assert flag == "--json"
        payload = json.loads(encoded)
        assert payload.pop("tab") == f"delivery-{step}"
        if action == "upload":
            copy = Path(payload.pop("files"))
            assert copy.name == attachment["name"]
            assert copy.read_bytes() == content
            copies.append(copy)
        expected = {
            "fill": {"selector": "#reference", "value": step},
            "upload": {"selector": "#invoice-file"},
            "click": {"selector": "#submit-form"},
        }
        assert payload == expected[action]
        observed.append(action)
        return b"{}"

    monkeypatch.setattr(view, "_command", command)
    app = FastAPI()
    app.include_router(local_routes(view))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/browser/desktop-delivery",
            json={
                "step": step,
                "form": FORM,
                "action": "submit",
                "file": attachment,
                "sha256": hashlib.sha256(content).hexdigest(),
                "data": base64.b64encode(content).decode(),
            },
        )
    assert response.json() == {"state": outcome}
    assert observed == actions
    assert all(not copy.exists() for copy in copies)


@pytest.mark.parametrize("cut", ["before_submit", "after_submit"])
def test_browser_worker_recovers_without_reissuing_submission(tmp_path, monkeypatch, cut):
    monkeypatch.setattr(browser_worker, "_LOCK_DIR", tmp_path)

    attachment = {"id": "a" * 32, "name": "invoice.pdf"}
    content = b"%PDF-1.4 synthetic attachment"
    state = {"signed_in": False, "receipt": False}
    calls: list[str] = []
    job_id = "c" * 32

    def browser(request):
        payload = json.loads(request.content)
        action = payload["action"]
        calls.append(action)
        assert request.headers["authorization"] == "Bearer local-test-only"
        assert payload["form"] == FORM
        if action == "prepare":
            result = "ready" if state["signed_in"] else "sign_in"
        elif action == "submit":
            assert base64.b64decode(payload["data"]) == content
            if cut == "after_submit":
                state["receipt"] = True
            raise httpx.ReadTimeout("Connection cut during submit")
        else:
            result = "delivered" if state["receipt"] else "uncertain"
        return httpx.Response(200, json={"state": result})

    def worker(server, desktop):
        return httpx.Client(
            base_url=server.client.base_url,
            headers={"Authorization": f"Bearer {desktop['token']}"},
            trust_env=False,
        )

    with httpx.Client(
        base_url="http://local",
        transport=httpx.MockTransport(browser),
        headers={"Authorization": "Bearer local-test-only"},
    ) as local:
        with running(tmp_path) as server:
            session = server.request("POST", "/sessions/primary")["session_id"]
            cloud = server.request("POST", "/desktops", status=201, json={"label": "Cloud"})
            mac = server.request("POST", "/desktops", status=201, json={"label": "Mac stand-in"})
            assert (
                server.client.put(
                    f"/sessions/{session}/attachments/{attachment['id']}",
                    params={"name": attachment["name"]},
                    content=content,
                ).status_code
                == 204
            )
            jobs = f"/sessions/{session}/desktop-jobs"
            job = f"{jobs}/{job_id}"
            server.request(
                "POST",
                jobs,
                status=202,
                json={
                    "job_id": job_id,
                    "file": attachment,
                    "desktops": [cloud["id"], mac["id"]],
                    "browser": FORM,
                },
            )
            first, last = [item["id"] for item in server.request("GET", job)["steps"]]
            with worker(server, cloud) as cloud_client, worker(server, mac) as mac_client:
                assert (
                    mac_client.get(WORKER_PATH, params={"browser": True}).json()["step_id"] is None
                )
                assert work_once(cloud_client, tmp_path / "cloud") == first
                server.request("POST", f"{job}/browser/retarget", json={"desktop": cloud["id"]})
                assert mac_client.get(WORKER_PATH, params={"browser": True}).json() == {
                    "step_id": None
                }
                server.request("POST", f"{job}/browser/retarget", json={"desktop": mac["id"]})
                assert mac_client.get(WORKER_PATH).json()["step_id"] is None
                assert mac_client.get(f"{WORKER_PATH}/{last}/file").status_code == 409
                assert cloud_client.get(f"{WORKER_PATH}/{last}/file").status_code == 404
                assert mac_client.post(f"{job}/browser/approve").status_code == 401
                server.request("POST", f"{job}/browser/approve", status=409)
                work_once(mac_client, tmp_path / "mac", local)
                assert server.request("GET", job)["steps"][-1]["browser_state"] == "sign_in"
                state["signed_in"] = True
                work_once(mac_client, tmp_path / "mac", local)
                assert server.request("GET", job)["steps"][-1]["browser_state"] == "ready"
                server.request(
                    "POST", f"{job}/browser/approve", status=409, json={"desktop": cloud["id"]}
                )
                for _ in range(2):  # Idempotent approval binds the reviewed desktop.
                    server.request("POST", f"{job}/browser/approve", json={"desktop": mac["id"]})
                with pytest.raises(httpx.ReadTimeout):
                    work_once(mac_client, tmp_path / "mac", local)
                assert server.request("GET", job)["steps"][-1]["browser_state"] == "sending"

        with running(tmp_path) as server, worker(server, mac) as mac_client:
            assert server.request("POST", "/sessions/primary")["session_id"] == session
            work_once(mac_client, tmp_path / "mac", local)
            final = "delivered" if cut == "after_submit" else "uncertain"
            assert server.request("GET", job)["steps"][-1]["browser_state"] == final
            assert calls == ["prepare", "prepare", "submit", "check"]
            # A worker cannot restore its spent approval, even by calling the API directly.
            assert (
                mac_client.put(
                    WORKER_PATH,
                    json={
                        "step_id": last,
                        "platform": sys.platform,
                        "phase": "sending",
                    },
                ).status_code
                == 409
            )
            if final == "uncertain":
                server.request("POST", f"{job}/browser/approve", status=409)
                state["receipt"] = True
                server.request("POST", f"{job}/browser/verify")
                assert work_once(mac_client, tmp_path / "mac", local) == last
            assert server.request("GET", job)["complete"]
            chat = [turn["text"] for turn in server.snapshot(session)["transcript"]]
            assert (
                sum("confirmed the browser receipt" in text and last in text for text in chat) == 1
            )
            assert all("local-test-only" not in text for text in chat)


@pytest.mark.parametrize(
    ("tab_status", "inspection", "expected"),
    [
        ("offline", None, "unavailable"),
        ("closed", None, "uncertain"),
        ("private", None, "unavailable"),
        ("open", b"invalid JSON", "unavailable"),
        ("open", b'{"state":"ready"}', "uncertain"),
        ("open", b'{"state":"delivered"}', "delivered"),
    ],
)
async def test_receipt_check_distinguishes_missing_receipts_from_outages(
    tmp_path, monkeypatch, tab_status, inspection, expected
):
    consent = Consent(tmp_path / "consent")
    consent.set(True)
    view = BrowserView(tmp_path / "private", consent, Relay(tmp_path), lambda: Path("browse"))
    step = "d" * 32

    async def command(_, action, *args, timeout):
        if action == "status":
            tab = {"tab": 7, "id": f"delivery-{step}"}
            if tab_status == "private":
                tab["handoff"] = {}
            return json.dumps(
                {
                    "extension": {"connected": tab_status != "offline"},
                    "tabs": [tab] if tab_status in ("open", "private") else [],
                }
            ).encode()
        assert action == "execute" and inspection is not None
        return inspection

    monkeypatch.setattr(view, "_command", command)
    app = FastAPI()
    app.include_router(local_routes(view))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {"step": step, "form": FORM, "action": "check"}
        response = await client.post("/browser/desktop-delivery", json=body)
        assert response.json() == {"state": expected}
        await view.enter()
        consent.set(False)
        response = await client.post("/browser/desktop-delivery", json=body)
        assert response.json() == {"state": "sign_in"}


async def test_submit_rejects_file_names_with_a_null_byte_before_using_browser(tmp_path):
    consent = Consent(tmp_path / "consent")
    consent.set(True)
    view = BrowserView(tmp_path / "private", consent, Relay(tmp_path), lambda: Path("browse"))
    app = FastAPI()
    app.include_router(local_routes(view))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/browser/desktop-delivery",
            json={
                "step": "d" * 32,
                "form": FORM,
                "action": "submit",
                "file": {"id": "a" * 32, "name": "invoice\0.pdf"},
                "sha256": hashlib.sha256(b"sample").hexdigest(),
                "data": base64.b64encode(b"sample").decode(),
            },
        )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid file name."}


def test_second_worker_cannot_check_a_submission_still_in_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_worker, "_LOCK_DIR", tmp_path)
    downloading = Event()
    release_file = Event()
    updates = []
    actions = []
    content = b"sample"

    def coordinator(request):
        if request.method == "PUT":
            updates.append(json.loads(request.content)["phase"])
            return httpx.Response(200, json={"changed": True})
        assert request.method == "GET"
        downloading.set()
        assert release_file.wait(timeout=2)
        return httpx.Response(200, content=content)

    def local(request):
        action = json.loads(request.content)["action"]
        actions.append(action)
        return httpx.Response(
            200, json={"state": "delivered" if action == "submit" else "uncertain"}
        )

    work = {
        "step_id": "d" * 32,
        "phase": "approved",
        "form": FORM,
        "file": {"id": "a" * 32, "name": "invoice.pdf"},
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    with (
        httpx.Client(
            base_url="http://coordinator", transport=httpx.MockTransport(coordinator)
        ) as c,
        httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(local)) as b,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        first = executor.submit(browser_worker.browser_step, c, b, work)
        try:
            assert downloading.wait(timeout=1)
            assert not browser_worker.browser_step(c, b, {**work, "phase": "sending"})
            assert actions == []
            assert updates == ["sending"]
        finally:
            release_file.set()
        assert first.result(timeout=1) is True
    assert actions == ["submit"]
    assert updates == ["sending", "delivered"]


@pytest.mark.parametrize(
    "reply",
    [httpx.Response(503), httpx.Response(200, json=[]), httpx.Response(200, text="invalid")],
)
def test_worker_keeps_receipt_pending_on_invalid_local_response(tmp_path, monkeypatch, reply):
    monkeypatch.setattr(browser_worker, "_LOCK_DIR", tmp_path)

    def publish(request):
        pytest.fail(f"Unexpected worker update for {request.method}")

    with (
        httpx.Client(base_url="http://coordinator", transport=httpx.MockTransport(publish)) as c,
        httpx.Client(
            base_url="http://localhost", transport=httpx.MockTransport(lambda _: reply)
        ) as b,
    ):
        work = {"step_id": "d" * 32, "phase": "sending", "form": FORM}
        assert not browser_worker.browser_step(c, b, work)


@pytest.mark.parametrize(("phase", "changed"), [("approved", "true"), ("sending", False)])
def test_worker_requires_an_acknowledged_phase_change(tmp_path, monkeypatch, phase, changed):
    monkeypatch.setattr(browser_worker, "_LOCK_DIR", tmp_path)

    def coordinator(request):
        assert request.method == "PUT"
        assert json.loads(request.content)["phase"] == (
            "sending" if phase == "approved" else "delivered"
        )
        return httpx.Response(200, json={"changed": changed})

    def local(request):
        assert phase == "sending" and json.loads(request.content)["action"] == "check"
        return httpx.Response(200, json={"state": "delivered"})

    with (
        httpx.Client(
            base_url="http://coordinator", transport=httpx.MockTransport(coordinator)
        ) as c,
        httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(local)) as b,
    ):
        work = {"step_id": "d" * 32, "phase": phase, "form": FORM}
        assert not browser_worker.browser_step(c, b, work)


def test_form_and_local_browser_reject_even_an_empty_credential_field():
    url = "http://@localhost/"
    with pytest.raises(ValidationError, match="without credentials"):
        BrowserForm.model_validate({**FORM, "url": url})
    with pytest.raises(ValueError, match="local Bud URL"):
        browser_worker.local_browser(url, "test-token")
