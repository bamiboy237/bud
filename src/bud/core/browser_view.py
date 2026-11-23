"""Owner-only Browse previews and a persistent pause for private input."""

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from bud import browser

logger = logging.getLogger(__name__)


class TakeoverRequest(BaseModel):
    tab: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class BrowserView:
    def __init__(
        self,
        marker: Path,
        consent: browser.Consent,
        relay: browser.Relay,
        locate: Callable[[], Path | None] = browser.executable,
    ):
        self.marker = marker
        self.consent = consent
        self.relay = relay
        self.locate = locate
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        if not marker.exists():
            self.ready.set()
        self.handoff: asyncio.Task[None] | None = None

    @property
    def private(self) -> bool:
        return not self.ready.is_set()

    @property
    def handoff_open(self) -> bool:
        return self.lock.locked() or (self.handoff is not None and not self.handoff.done())

    def enter(self) -> None:
        # A crash during takeover leaves the browser paused after restart.
        fd = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.ready.clear()

    def resume(self) -> None:
        self.marker.unlink(missing_ok=True)
        self.ready.set()

    async def _command(self, path: Path, *args: str, timeout: float) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            str(path),
            *args,
            env=self.relay.env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode:
            raise RuntimeError("Browse could not perform the requested action.")
        return out

    async def request_handoff(self, path: Path, tab: str, message: str) -> dict[str, str]:
        """Run while holding lock. Return only the upstream outcome enum to the model."""
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tab):
            return {"outcome": "unavailable"}
        self.enter()
        try:
            raw = await self._command(
                path,
                "handoff",
                "--tab",
                tab,
                "--message",
                message,
                "--timeoutSeconds",
                "600",
                timeout=610,
            )
            outcome = json.loads(raw)["outcome"]
        except (OSError, RuntimeError, TimeoutError, KeyError, ValueError, TypeError):
            outcome = "unavailable"
        if outcome == "continued":
            self.resume()
        if outcome not in ("continued", "timeout", "closed", "cancelled", "unavailable"):
            outcome = "unavailable"
        return {"outcome": outcome}

    async def bring_forward(self, path: Path, tab: str) -> None:
        async with self.lock:
            await self.request_handoff(
                path, tab, "Use the browser privately, then select Continue to return to Bud."
            )

    def _enabled_path(self) -> Path:
        path = self.locate()
        if not self.consent.on() or path is None:
            raise HTTPException(409, "Connect and enable Browse from chat first.")
        return path

    async def stop(self) -> None:
        if self.handoff is not None and not self.handoff.done():
            self.handoff.cancel()
            await asyncio.gather(self.handoff, return_exceptions=True)

    def routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/browser/view")
        async def view():
            if self.private:
                return {"private": True, "handoff_open": self.handoff_open, "tabs": []}
            async with self.lock:
                if self.private:
                    return {"private": True, "handoff_open": self.handoff_open, "tabs": []}
                path = self.locate()
                if not self.consent.on() or path is None:
                    return {"private": False, "enabled": False, "tabs": []}
                tabs = []
                for item in await browser._tabs(path, self.relay):
                    tab_id = str(item.get("tab", item.get("id", "")))
                    if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tab_id):
                        try:
                            host = urlsplit(str(item.get("url", ""))).hostname
                        except ValueError:
                            host = None
                        tabs.append(
                            {
                                "id": tab_id,
                                "title": str(item.get("title") or "Browser tab")[:160],
                                "host": host or "",
                            }
                        )
                return {"private": False, "enabled": True, "tabs": tabs}

        @router.get("/browser/view/{tab}/image")
        async def image(tab: str):
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tab):
                raise HTTPException(404, "Unknown tab.")
            if self.private:
                raise HTTPException(423, "Browser preview is paused for private input.")
            async with self.lock:
                if self.private:
                    raise HTTPException(423, "Browser preview is paused for private input.")
                path = self._enabled_path()
                self.relay.home.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    with TemporaryDirectory(dir=self.relay.home) as folder:
                        output = Path(folder) / "preview.jpg"
                        await self._command(
                            path,
                            "screenshot",
                            "--tab",
                            tab,
                            "--format",
                            "jpeg",
                            "--quality",
                            "55",
                            "--output",
                            str(output),
                            timeout=10,
                        )
                        with output.open("rb") as stream:
                            data = stream.read(8 * 1024 * 1024 + 1)
                    if len(data) > 8 * 1024 * 1024 or not data.startswith(b"\xff\xd8"):
                        raise ValueError("Invalid or oversized preview.")
                except (OSError, RuntimeError, TimeoutError, ValueError):
                    raise HTTPException(502, "Browse could not capture the preview.") from None
            return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

        @router.post("/browser/view/takeover", status_code=202)
        async def takeover(body: TakeoverRequest):
            if self.private:
                raise HTTPException(409, "A private browser session is already active.")
            async with self.lock:
                if self.private:
                    raise HTTPException(409, "A private browser session is already active.")
                path = self._enabled_path()
                self.enter()
                self.handoff = asyncio.create_task(self.bring_forward(path, body.tab))
            return {"private": True}

        @router.post("/browser/view/resume")
        async def resume():
            if not self.private:
                return {"private": False}
            if self.handoff_open:
                raise HTTPException(409, "Use Continue in the Chrome tab to finish handback.")
            async with self.lock:
                self.resume()
            return {"private": False}

        return router
