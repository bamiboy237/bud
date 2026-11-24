"""Drive the browser step via Bud running on the paired computer."""

import base64
import hashlib
import sys
from urllib.parse import urlsplit

import httpx

from bud.core.handoff import WORKER_PATH


def browser_step(client: httpx.Client, browser: httpx.Client, work: dict) -> bool:
    step, phase = work["step_id"], work["phase"]

    def publish(target: str) -> dict:
        response = client.put(
            WORKER_PATH, json={"step_id": step, "phase": target, "platform": sys.platform}
        )
        if response.status_code == 409:
            return {"changed": False}
        return response.raise_for_status().json()

    def local(action: str, **kwargs) -> str:
        response = browser.post(
            "/browser/desktop-delivery",
            json={"step": step, "form": work["form"], "action": action, **kwargs},
        )
        # A local Bud without Browse can be started or paired later.
        if response.status_code in (404, 409):
            return "unavailable"
        return response.raise_for_status().json()["state"]

    if phase in ("queued", "sign_in"):
        prepared = local("prepare")
        if prepared in ("ready", "sign_in"):
            publish(prepared)
        return False
    if phase == "sending":
        observed = local("check")
        if observed in ("delivered", "uncertain"):
            publish(observed)
        return observed == "delivered"

    if phase != "approved" or not publish("sending").get("changed"):
        return False
    # This authorization is consumed before touching the form. A disconnect after
    # this point leaves a read-only recovery step, even if the browser did nothing.
    file = client.get(f"{WORKER_PATH}/{step}/file")
    if file.status_code == 409:
        publish("uncertain")
        return False
    file.raise_for_status()
    if hashlib.sha256(file.content).hexdigest() != work["sha256"]:
        publish("uncertain")
        return False
    result = local(
        "submit",
        file=work["file"],
        sha256=work["sha256"],
        data=base64.b64encode(file.content).decode("ascii"),
    )
    if result in ("delivered", "uncertain"):
        publish(result)
    return result == "delivered"


def local_browser(url: str, token: str) -> httpx.Client:
    endpoint = urlsplit(url)
    if (
        endpoint.scheme not in {"http", "https"}
        or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("The browser service must be a local Bud URL.")
    return httpx.Client(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
        timeout=110,
        trust_env=False,
    )
