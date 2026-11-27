"""Pair owner-selected browsers without changing Bud's browser permissions."""

import asyncio
import json
from dataclasses import dataclass
from hashlib import pbkdf2_hmac
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from bud import browser
from bud.composio.binding import ConnectionError
from bud.core.browser_view import BrowserView

_PRIVATE_NETWORKS = tuple(
    IPv4Network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
)


def private_peer(ip: str | None) -> str | None:
    if ip is None:
        return None
    try:
        address = IPv4Address(ip)
    except ValueError:
        return None
    return str(address) if any(address in network for network in _PRIVATE_NETWORKS) else None


def _pairing_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _pending_receipt(home: Path, code: str) -> str | None:
    # Browse stores the receipt ID alongside the code's hash until the browser redeems it.
    codes = _pairing_file(home / "pairing-codes.json").get("codes")
    if not isinstance(codes, list):
        return None
    key = pbkdf2_hmac(
        "sha256", code.replace("-", "").encode(), b"opencode-browser-pairing-v3|code", 100_000
    ).hex()
    for entry in codes:
        match entry:
            case {"key": entry_key, "receipt": str(receipt)} if entry_key == key and receipt:
                return receipt
    return None


def _confirmed_browser(home: Path, receipt: str) -> dict[str, str] | None:
    for entry in _pairing_file(home / "browser-tokens.json").values():
        match entry:
            case {"receipt": entry_receipt, "lastSeen": int(), "id": str(id_), "name": str(name)}:
                if entry_receipt == receipt and id_:
                    return {"id": id_, "name": (name or id_)[:160]}
    return None


class PairComputer(BaseModel):
    browser: Literal["local", "remote"]


@dataclass(frozen=True)
class BrowserComputers:
    view: BrowserView
    pairing: browser.Pairing
    peer_ip: str | None

    def routes(self) -> APIRouter:
        api = APIRouter()
        peer = private_peer(self.peer_ip)
        home = self.pairing.relay.home
        pending_pairing: tuple[str, str] | None = None
        confirmed_pairing: dict[str, str] | None = None

        @api.get("/browser/computers")
        async def computers(response: Response):
            nonlocal pending_pairing, confirmed_pairing
            response.headers["Cache-Control"] = "no-store"
            path = self.view.locate()
            unavailable = {"browsers": [], "available": False, "private_network": bool(peer)}
            if self.view.private or path is None:
                return unavailable
            async with self.view.lock:
                if self.view.private:
                    return unavailable
                result = await self.view._inventory(path)
                if pending_pairing is not None:
                    code, receipt = pending_pairing
                    paired = await asyncio.to_thread(_confirmed_browser, home, receipt)
                    if paired is not None:
                        confirmed_pairing = {"code": code, **paired}
                        pending_pairing = None
                if self.view.private:
                    return unavailable
                inventory = {
                    "browsers": result["browsers"],
                    "available": True,
                    "private_network": bool(peer),
                }
                if confirmed_pairing is not None:
                    inventory["pairing"] = confirmed_pairing
                return inventory

        @api.post("/browser/computers/pair")
        async def pair(body: PairComputer, response: Response):
            nonlocal pending_pairing, confirmed_pairing
            response.headers["Cache-Control"] = "no-store"
            host = "127.0.0.1" if body.browser == "local" else peer
            if host is None:
                raise HTTPException(
                    409, "Connect the Bud server to a private browser network first."
                )
            if self.view.private:
                raise HTTPException(423, "Finish private input before adding a computer.")
            async with self.view.lock:
                if self.view.private:
                    raise HTTPException(423, "Finish private input before adding a computer.")
                path = self.view.locate()
                if path is None:
                    raise HTTPException(409, "Browse isn’t installed on the Bud server.")
                pairing = asyncio.create_task(asyncio.to_thread(self.pairing.code, path))
                try:
                    code = await asyncio.shield(pairing)
                except asyncio.CancelledError:
                    await asyncio.gather(pairing, return_exceptions=True)
                    raise
                except (ConnectionError, OSError):
                    raise HTTPException(503, "Browse could not start pairing. Try again.") from None
                receipt = await asyncio.to_thread(_pending_receipt, home, code)
                pending_pairing = (code, receipt) if receipt is not None else None
                confirmed_pairing = None
            return {"code": code, "address": f"{host}:{self.view.relay.port}"}

        return api
