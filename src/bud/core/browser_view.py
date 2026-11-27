"""Owner-only Browse previews and a persistent pause for private input."""

import asyncio
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send

from bud import browser

TAB_ID = r"^[a-zA-Z0-9_-]{1,64}$"
PREVIEW_PAUSED = "Browser preview is paused for private input."
PREVIEW_UNAVAILABLE = "Browse could not capture the preview."
NO_STORE = {"Cache-Control": "no-store"}
MAX_PREVIEW_BYTES = 8 * 1024 * 1024


class TakeoverRequest(BaseModel):
    tab: str = Field(pattern=TAB_ID)


class PreviewResponse(Response):
    def __init__(self, data: bytes, view: "BrowserView", generation: int):
        super().__init__(data, media_type="image/jpeg", headers=NO_STORE)
        self.view = view
        self.generation = generation

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with self.view.preview_send:
            try:
                self.view.check_preview(self.generation)
            except HTTPException as exc:
                paused = JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code, headers=NO_STORE
                )
            else:
                async with asyncio.timeout(10):
                    await super().__call__(scope, receive, send)
                return
        await paused(scope, receive, send)


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
        self.handoff: asyncio.Task[dict[str, str]] | None = None
        self.active_tab: str | None = None
        self.preview_send = asyncio.Lock()
        self.preview_generation = 0
        self.preview_slots = asyncio.Semaphore(3)
        self.previews: set[asyncio.Task[bytes]] = set()

    @property
    def private(self) -> bool:
        return not self.ready.is_set()

    @property
    def handoff_open(self) -> bool:
        return self.handoff is not None and not self.handoff.done()

    async def enter(self) -> None:
        async with self.preview_send:
            # Finish sending any public frame before latching the private pause.
            try:
                fd = os.open(
                    self.marker, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
                )
                os.close(fd)
            except OSError:
                raise HTTPException(503, "Bud could not save the private browser pause.") from None
            self.ready.clear()
            self.preview_generation += 1

    def resume(self) -> None:
        try:
            self.marker.unlink(missing_ok=True)
        except OSError:
            raise HTTPException(503, "Bud could not clear the private browser pause.") from None
        self.ready.set()

    async def _command(self, path: Path, *args: str, timeout: float) -> bytes:
        return await self.relay.command(path, *args, timeout=timeout)

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
        offline = {"connected": False, "browsers": [], "tabs": [], "_tab_ids": {}}
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
        except (OSError, ValueError, RuntimeError):
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
                name = source.get("name")
                browsers.append(
                    {"id": identity, "name": (name if isinstance(name, str) else identity)[:160]}
                )
        tabs = []
        tab_ids = {}
        active = None
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("tab", item.get("id"))
            if not isinstance(raw_id, str) and type(raw_id) is not int:
                continue
            tab_id = str(raw_id)
            if not re.fullmatch(TAB_ID, tab_id):
                continue
            target = None if item.get("handoff") is not None else tab_id
            tab_ids[tab_id] = target
            alias = item.get("id")
            if isinstance(alias, str) and re.fullmatch(TAB_ID, alias) and not alias.isdecimal():
                tab_ids[alias] = target
            if target is None:
                continue
            if self.active_tab is not None and self.active_tab in (tab_id, alias):
                active = tab_id
            url = item.get("url")
            try:
                host = urlsplit(url).hostname if isinstance(url, str) else None
            except ValueError:
                host = None
            source_id = item.get("browser")
            title = item.get("title")
            tabs.append(
                {
                    "id": tab_id,
                    "title": title[:160] if isinstance(title, str) and title else "Browser tab",
                    "host": host or "",
                    "browser": source_id if isinstance(source_id, str) else None,
                }
            )
        return {
            "connected": True,
            "browsers": browsers,
            "tabs": tabs,
            "active_tab": active,
            "_tab_ids": tab_ids,
        }

    async def _available_tab(self, path: Path, tab: str) -> str:
        status = await self._inventory(path)
        if not status["connected"]:
            raise HTTPException(502, "Browse is offline. Check the Browse connection.")
        key = str(int(tab)) if tab.isdecimal() else tab
        if key not in status["_tab_ids"]:
            raise HTTPException(404, "Unknown tab.")
        target = status["_tab_ids"][key]
        if target is None:
            raise HTTPException(423, PREVIEW_PAUSED)
        return target

    async def request_handoff(
        self, path: Path, tab: str, message: str
    ) -> asyncio.Task[dict[str, str]] | None:
        """Look up the model's tab and start handoff while holding lock."""
        if not re.fullmatch(TAB_ID, tab):
            return None
        try:
            target = await self._available_tab(path, tab)
            return await self.start_handoff(path, target, message)
        except HTTPException:
            return None

    async def start_handoff(
        self,
        path: Path,
        tab: str,
        message: str = "Use the browser privately, then select Continue to return to Bud.",
    ) -> asyncio.Task[dict[str, str]]:
        """Start handoff while holding lock and leave the marker until explicit handback."""
        await self.enter()
        self.handoff = asyncio.create_task(self._handoff(path, tab, message))
        return self.handoff

    async def _handoff(self, path: Path, tab: str, message: str) -> dict[str, str]:
        try:
            # Drain commands started before the pause before opening Chrome for private input.
            await asyncio.gather(*self.previews, return_exceptions=True)
            raw = await self._tab_command(
                path, "handoff", tab, timeout=610, message=message, timeoutSeconds=600
            )
            outcome = json.loads(raw.partition(b"\n")[0])["outcome"]
            if outcome == "continued":
                self.resume()
            if outcome in ("continued", "timeout", "closed", "cancelled"):
                return {"outcome": outcome}
        except (HTTPException, OSError, RuntimeError, KeyError, ValueError, TypeError):
            pass
        finally:
            self.handoff = None
        return {"outcome": "unavailable"}

    def _enabled_path(self) -> Path:
        path = self.locate()
        if not self.consent.on() or path is None:
            raise HTTPException(409, "Connect and enable Browse from chat first.")
        return path

    def check_preview(self, generation: int) -> None:
        if self.private or generation != self.preview_generation:
            raise HTTPException(423, PREVIEW_PAUSED)
        if not self.consent.on():
            raise HTTPException(409, "Connect and enable Browse from chat first.")

    async def _capture(self, path: Path, tab: str, generation: int) -> bytes:
        target = await self._available_tab(path, tab)
        self.check_preview(generation)
        try:
            self.relay.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            with TemporaryDirectory(dir=self.relay.home) as folder:
                output = Path(folder) / "preview.jpg"
                try:
                    await self._tab_command(
                        path,
                        "screenshot",
                        target,
                        timeout=10,
                        output=output,
                        format="jpeg",
                        quality=55,
                    )
                except RuntimeError:
                    # A tab can close between lookup and capture.
                    self.check_preview(generation)
                    await self._available_tab(path, target)
                    raise
                with output.open("rb") as stream:
                    data = stream.read(MAX_PREVIEW_BYTES + 1)
            if (
                len(data) > MAX_PREVIEW_BYTES
                or not data.startswith(b"\xff\xd8\xff")
                or not data.endswith(b"\xff\xd9")
                or b"\xff\xda" not in data
            ):
                raise ValueError("Invalid preview.")
        except (OSError, RuntimeError, ValueError):
            self.check_preview(generation)
            raise HTTPException(502, PREVIEW_UNAVAILABLE) from None
        self.check_preview(generation)
        return data

    async def stop(self) -> None:
        if self.handoff is not None and not self.handoff.done():
            self.handoff.cancel()
            await asyncio.gather(self.handoff, return_exceptions=True)
        self.handoff = None

    def routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/browser/view")
        async def view(response: Response):
            response.headers.update(NO_STORE)
            if self.private:
                return {"private": True, "handoff_open": self.handoff_open, "tabs": []}
            async with self.lock:
                if self.private:
                    return {"private": True, "handoff_open": self.handoff_open, "tabs": []}
                path = self.locate()
                if not self.consent.on() or path is None:
                    return {"private": False, "enabled": False, "tabs": []}
                status = await self._inventory(path)
                status.pop("_tab_ids")
                return {"private": False, "enabled": True, **status}

        @router.get("/browser/view/{tab}/image")
        async def image(tab: str):
            if not re.fullmatch(TAB_ID, tab):
                raise HTTPException(404, "Unknown tab.")
            generation = self.preview_generation
            self.check_preview(generation)
            try:
                async with asyncio.timeout(20), self.preview_slots:
                    self.check_preview(generation)
                    path = self._enabled_path()
                    capture = asyncio.create_task(self._capture(path, tab, generation))
                    self.previews.add(capture)
                    try:
                        data = await capture
                    finally:
                        self.previews.discard(capture)
            except TimeoutError:
                self.check_preview(generation)
                raise HTTPException(502, PREVIEW_UNAVAILABLE) from None
            return PreviewResponse(data, self, generation)

        @router.post("/browser/view/takeover", status_code=202)
        async def takeover(body: TakeoverRequest):
            if self.private:
                raise HTTPException(409, "A private browser session is already active.")
            async with self.lock:
                if self.private:
                    raise HTTPException(409, "A private browser session is already active.")
                path = self._enabled_path()
                tab = await self._available_tab(path, body.tab)
                await self.start_handoff(path, tab)
            return {"private": True}

        @router.post("/browser/view/resume")
        async def resume():
            if not self.private:
                return {"private": False}
            if self.handoff_open:
                raise HTTPException(409, "Use Continue in the Chrome tab to finish handback.")
            async with self.lock:
                if self.handoff_open:
                    raise HTTPException(409, "Use Continue in the Chrome tab to finish handback.")
                self.resume()
            return {"private": False}

        return router
