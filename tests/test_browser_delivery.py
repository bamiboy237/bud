"""Cross-worker browser jobs hold approvals across restart and never reissue a submit."""

import base64
import json
import sys

import httpx
import pytest

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


@pytest.mark.parametrize("cut", ["before_submit", "after_submit"])
def test_browser_worker_recovers_without_reissuing_submission(tmp_path, cut):

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
        base_url="http://127.0.0.1:8420",
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
                server.request("POST", f"{job}/browser/approve")
                server.request("POST", f"{job}/browser/approve")  # Idempotent approval.
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
