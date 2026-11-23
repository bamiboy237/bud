"""A cross-desktop job recovers the same step and publishes once in its original chat."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from bud.core.handoff import WORKER_PATH
from bud.core.handoff_client import work_once
from tests.test_server import running


def test_handoff_resumes_after_worker_and_server_restart(tmp_path):
    selected = {"id": "a" * 32, "name": "sample.txt"}
    unselected = {"id": "b" * 32, "name": "other.txt"}
    data = b"Sample receipt.\n"
    digest = hashlib.sha256(data).hexdigest()

    with running(tmp_path) as bud:
        session = bud.request("POST", "/sessions/primary")["session_id"]
        cloud = bud.request("POST", "/desktops", status=201, json={"label": "Cloud"})
        mac = bud.request("POST", "/desktops", status=201, json={"label": "Mac stand-in"})
        for file in (selected, unselected):
            response = bud.client.put(
                f"/sessions/{session}/attachments/{file['id']}",
                params={"name": file["name"]},
                content=data if file == selected else b"private",
            )
            assert response.status_code == 204

        body = {"job_id": "c" * 32, "file": selected, "desktops": [cloud["id"], mac["id"]]}
        jobs = f"/sessions/{session}/desktop-jobs"
        for _ in range(2):
            assert bud.request("POST", jobs, status=202, json=body) == {"id": body["job_id"]}
        status = f"{jobs}/{body['job_id']}"
        cloud_step, mac_step = [step["id"] for step in bud.request("GET", status)["steps"]]

        calls = []

        def interrupt(request):
            calls.append((request.method, request.url.path))
            if request.method == "PUT":
                raise httpx.ReadTimeout("Disconnected before uploading receipt")
            with httpx.HTTPTransport() as transport:
                response = transport.handle_request(request)
                response.read()
                return response

        with (
            httpx.Client(
                base_url=bud.client.base_url,
                headers={"Authorization": f"Bearer {mac['token']}"},
                trust_env=False,
            ) as worker,
            httpx.Client(
                base_url=bud.client.base_url,
                transport=httpx.MockTransport(interrupt),
                headers={"Authorization": f"Bearer {cloud['token']}"},
            ) as flaky,
        ):
            assert worker.get(WORKER_PATH).json()["step_id"] is None
            assert worker.get(f"{WORKER_PATH}/{cloud_step}/file").status_code == 404
            assert worker.get(f"/sessions/{session}").status_code == 401
            assert (
                worker.get(f"/sessions/{session}/attachments/{unselected['id']}").status_code == 401
            )
            with pytest.raises(httpx.ReadTimeout):
                work_once(flaky, tmp_path / "cloud")
        assert ("GET", f"{WORKER_PATH}/{cloud_step}/file") in calls

    with running(tmp_path) as bud:
        assert bud.request("POST", "/sessions/primary")["session_id"] == session
        # Resume via a new OS process. The selected file is absent, so it can only
        # deliver the locally saved receipt rather than perform the work again.
        attachment = tmp_path / "data" / "workspace" / "attachments" / session
        file = attachment / selected["id"] / selected["name"]
        file.rename(file.with_suffix(".offline"))
        for desktop, label, step in [(cloud, "cloud", cloud_step), (mac, "mac", mac_step)]:
            result = subprocess.run(
                [
                    str(Path(sys.executable).with_name("bud")),
                    "desktop",
                    "--url",
                    str(bud.client.base_url),
                    "run",
                    "--once",
                    "--state",
                    str(tmp_path / label),
                ],
                env={**os.environ, "BUD_DESKTOP_TOKEN": desktop["token"]},
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            assert step in result.stdout
            if label == "cloud":
                file.with_suffix(".offline").rename(file)
        assert bud.request("GET", status)["complete"]
        transcript = [turn["text"] for turn in bud.snapshot(session)["transcript"]]
        assert len(transcript) == 2
        assert transcript[0].startswith(f"Cloud ({sys.platform})")
        assert transcript[1].startswith(f"Mac stand-in ({sys.platform})")
        assert all(digest in text for text in transcript)
        receipt = {"step_id": cloud_step, "sha256": digest, "platform": sys.platform}
        headers = {"Authorization": f"Bearer {cloud['token']}"}
        assert bud.client.put(WORKER_PATH, json=receipt, headers=headers).status_code == 200
        assert len(bud.snapshot(session)["transcript"]) == 2
        assert bud.client.delete(f"/desktops/{cloud['id']}").status_code == 204
        assert bud.client.get(WORKER_PATH, headers=headers).status_code == 401
