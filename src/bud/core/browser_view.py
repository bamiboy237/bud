"""Owner-only Browse previews and a persistent pause for private input."""

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
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
        self.active_tab: str | None = None

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

    async def _tab_command(
        self,
        path: Path,
        action: str,
        tab: str,
        *,
        timeout: float,
        output: Path | None = None,
        **arguments: Any,
    ) -> bytes:
        payload = json.dumps({"tab": int(tab) if tab.isdecimal() else tab, **arguments})
        args = ("--output", str(output)) if output is not None else ()
        return await self._command(path, action, "--json", payload, *args, timeout=timeout)

    async def _inventory(self, path: Path) -> dict[str, Any]:
        offline = {"connected": False, "browsers": [], "tabs": []}
        try:
            status = json.loads(await self._command(path, "status", "--json", timeout=5))
            match status:
                case {"relay": {"running": False}}:
                    return offline
                case {
                    "extension": {"connected": bool(connected)} as extension,
                    "tabs": list(items),
                }:
                    sources = extension.get("browsers", [])
                    if not isinstance(sources, list):
                        raise ValueError("Invalid Browse browser list.")
                case _:
                    raise ValueError("Invalid Browse status.")
        except (OSError, ValueError, RuntimeError, TimeoutError):
            raise HTTPException(
                502, "Couldn’t load Browse tabs. Check the Browse connection."
            ) from None

        if not connected:
            return offline

        browsers = []
        for source in sources:
            if not isinstance(source, dict) or source.get("connected") is not True:
                continue
            identity = source.get("id")
            if isinstance(identity, str) and identity:
                browsers.append({"id": identity, "name": str(source.get("name") or identity)[:160]})
        tabs = []
        active = None
        for item in items:
            if not isinstance(item, dict):
                continue
            tab_id = str(item.get("tab", item.get("id", "")))
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tab_id):
                continue
            if self.active_tab is not None and self.active_tab in (tab_id, item.get("id")):
                active = tab_id
            try:
                host = urlsplit(str(item.get("url", ""))).hostname
            except ValueError:
                host = None
            source_id = item.get("browser")
            tabs.append(
                {
                    "id": tab_id,
                    "title": str(item.get("title") or "Browser tab")[:160],
                    "host": host or "",
                    "browser": source_id if isinstance(source_id, str) else None,
                }
            )
        return {"connected": True, "browsers": browsers, "tabs": tabs, "active_tab": active}

    async def request_handoff(self, path: Path, tab: str, message: str) -> dict[str, str]:
        """Run while holding lock. Return only the upstream outcome enum to the model."""
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tab):
            return {"outcome": "unavailable"}
        self.enter()
        try:
            raw = await self._tab_command(
                path, "handoff", tab, timeout=610, message=message, timeoutSeconds=600
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
                return {"private": False, "enabled": True, **await self._inventory(path)}

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
                        await self._tab_command(
                            path,
                            "screenshot",
                            tab,
                            timeout=10,
                            output=output,
                            format="jpeg",
                            quality=55,
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
