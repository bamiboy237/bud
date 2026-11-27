"""A paired worker can deliver a selected file through an explicitly configured web form."""

import base64
import binascii
import hashlib
import json
import os
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from bud.core.browser_view import BrowserView
from bud.core.projection import Attachment
from bud.workspace import ATTACHMENT_BYTES

HTMLId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
JobId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class BrowserForm(BaseModel):
    url: str = Field(max_length=2048)
    file_input: HTMLId
    reference_input: HTMLId
    submit_button: HTMLId
    receipt: HTMLId

    @field_validator("url")
    @classmethod
    def valid_url(cls, url: str) -> str:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise ValueError("Enter a valid form URL.") from None
        loopback = parts.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.scheme not in ({"http", "https"} if loopback else {"https"})
            or (port is not None and port < 1)
            or "\\" in url
            or any(c.isspace() for c in url)
        ):
            raise ValueError("Use an HTTPS form URL without credentials, a query, or a fragment.")
        # Browser location includes a slash on a bare origin.
        return url if parts.path else url + "/"

    @property
    def origin(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"


class LocalBrowserStep(BaseModel):
    step: JobId
    form: BrowserForm
    action: Literal["prepare", "submit", "check"]
    file: Attachment | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    data: str | None = Field(default=None, max_length=4 * ((ATTACHMENT_BYTES + 2) // 3))


class BrowserUpdate(BaseModel):
    step_id: JobId
    platform: Literal["darwin", "linux", "win32"]
    phase: Literal["sign_in", "ready", "sending", "uncertain", "delivered"]


# This code is fixed. It reads the target form structure, two expected element IDs,
# and an exact receipt match. Page text, cookies, storage and input values are never returned.
_INSPECT = """
return {state: await page.evaluate(({form, reference}) => {
    if (location.origin !== new URL(form.url).origin) return "other_page";
    const receipt = document.getElementById(form.receipt);
    if (receipt && receipt.textContent.trim() === reference) return "delivered";
    if (location.href !== new URL(form.url).href) return "other_page";
    const upload = document.getElementById(form.file_input);
    const number = document.getElementById(form.reference_input);
    const submit = document.getElementById(form.submit_button);
    if (!upload && !number && !submit) return "sign_in";
    if (!(upload instanceof HTMLInputElement) || upload.type !== "file" ||
        !(number instanceof HTMLInputElement) || number.type !== "text" ||
        !(submit instanceof HTMLButtonElement) || submit.type !== "submit" ||
        !upload.form || number.form !== upload.form || submit.form !== upload.form)
        return "invalid_form";
    const action = submit.hasAttribute("formaction") ? submit.formAction : upload.form.action;
    const method = submit.hasAttribute("formmethod") ? submit.formMethod : upload.form.method;
    const encoding = submit.hasAttribute("formenctype") ? submit.formEnctype : upload.form.enctype;
    const target = submit.getAttribute("formtarget") || upload.form.target ||
        document.querySelector("base[target]")?.target || "";
    if (!upload.name || !number.name || upload.name === number.name ||
        [upload, number, submit].some(element => element.matches(":disabled")) ||
        method !== "post" || encoding !== "multipart/form-data" ||
        !["", "_self"].includes(target) || new URL(action).origin !== location.origin)
        return "invalid_form";
    return "ready";
}, ARGS)};
"""


async def _inspect(view: BrowserView, path: Path, tab: str, body: LocalBrowserStep) -> str:
    args = json.dumps({"form": body.form.model_dump(), "reference": body.step})
    cmd = _INSPECT.replace("ARGS", args)
    raw = await view._command(path, "execute", cmd, f"--tab={tab}", timeout=15)
    # The CLI can add tab diagnostics before its JSON result.
    for line in reversed(raw.decode("utf-8", "replace").splitlines()):
        with suppress(ValueError, AttributeError, TypeError, RecursionError):
            value = json.loads(line).get("state")
            if value in ("ready", "sign_in", "delivered", "other_page", "invalid_form"):
                return value
    return "unavailable"


async def _inspect_existing(view: BrowserView, path: Path, tab: str, body: LocalBrowserStep) -> str:
    try:
        return await _inspect(view, path, tab, body)
    except (OSError, RuntimeError, TimeoutError):
        return "unavailable"


def _receipt_state(state: str) -> dict[str, str]:
    return {"state": state if state in ("delivered", "unavailable") else "uncertain"}


def local_routes(view: BrowserView) -> APIRouter:
    router = APIRouter()

    @router.post("/browser/desktop-delivery")
    async def local_step(body: LocalBrowserStep):
        """The local worker calls its own Bud server using the local owner credential."""
        tab = f"delivery-{body.step}"
        async with view.lock:
            if view.private:
                return {"state": "sign_in"}
            path = view._enabled_path()
            if body.action == "check":
                try:
                    target = await view._available_tab(path, tab)
                except HTTPException as exc:
                    return {"state": "uncertain" if exc.status_code == 404 else "unavailable"}
                return _receipt_state(await _inspect_existing(view, path, target, body))
            if body.action == "prepare":
                try:
                    browsers = (await view._inventory(path))["browsers"]
                except HTTPException:
                    return {"state": "unavailable"}
                if len(browsers) != 1:
                    return {"state": "unavailable"}
                # Reopening this ID keeps the existing tab and its unsaved form.
                params = {
                    "action": "new",
                    "id": tab,
                    "browser": browsers[0]["id"],
                    "url": body.form.url,
                    "keepOpen": True,
                    "allowedOrigins": [body.form.origin],
                }
                try:
                    await view._command(path, "tabs", "--json", json.dumps(params), timeout=30)
                except (OSError, RuntimeError, TimeoutError):
                    return {"state": "unavailable"}
                state = await _inspect_existing(view, path, tab, body)
                if state in ("sign_in", "other_page"):
                    # Alternate sign-in pages can be opened by the owner in another tab
                    # on this profile. No sign-in inputs go through the worker.
                    await view.start_handoff(path, tab)
                    return {"state": "sign_in"}
                return {"state": state}
            if body.data is None or body.file is None or body.sha256 is None:
                raise HTTPException(422, "Expected the selected file.")
            try:
                data = base64.b64decode(body.data, validate=True)
            except binascii.Error:
                raise HTTPException(422, "Invalid file encoding.") from None
            if len(data) > ATTACHMENT_BYTES or hashlib.sha256(data).hexdigest() != body.sha256:
                raise HTTPException(422, "The file does not match the approved copy.")
            if body.file.name in {".", ".."} or "\0" in body.file.name:
                raise HTTPException(422, "Invalid file name.")
            state = await _inspect_existing(view, path, tab, body)
            if state != "ready":
                return _receipt_state(state)
            try:
                # Browse includes /tmp on Unix, even if Bud and the relay use different TMPDIRs.
                with TemporaryDirectory(
                    dir="/tmp" if os.name == "posix" else None, prefix="bud-delivery-"
                ) as folder:
                    copy = Path(folder) / body.file.name
                    if copy.parent != Path(folder):
                        raise HTTPException(422, "Invalid file name.")
                    copy.write_bytes(data)
                    await view._tab_command(
                        path,
                        "fill",
                        tab,
                        selector=f"#{body.form.reference_input}",
                        value=body.step,
                        timeout=12,
                    )
                    state = await _inspect_existing(view, path, tab, body)
                    if state != "ready":
                        return _receipt_state(state)
                    await view._tab_command(
                        path,
                        "upload",
                        tab,
                        selector=f"#{body.form.file_input}",
                        files=str(copy),
                        timeout=30,
                    )
                    # Do not click if the form changed or a receipt already appeared.
                    if await _inspect_existing(view, path, tab, body) == "ready":
                        await view._tab_command(
                            path,
                            "click",
                            tab,
                            selector=f"#{body.form.submit_button}",
                            timeout=18,
                        )
            except (OSError, RuntimeError, TimeoutError):
                pass
            return _receipt_state(await _inspect_existing(view, path, tab, body))

    return router
