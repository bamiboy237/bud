"""Pair owner-selected browsers without changing Bud's browser permissions."""

import asyncio
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from bud import browser
from bud.composio.connection import ConnectionError
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

        @api.get("/browser/computers")
        async def computers(response: Response):
            response.headers["Cache-Control"] = "no-store"
            path = self.view.locate()
            if self.view.private or path is None:
                return {"browsers": [], "available": False, "private_network": bool(peer)}
            async with self.view.lock:
                if self.view.private:
                    return {"browsers": [], "available": False, "private_network": bool(peer)}
                result = await self.view._inventory(path)
            return {
                "browsers": result["browsers"],
                "available": True,
                "private_network": bool(peer),
            }

        @api.post("/browser/computers/pair")
        async def pair(body: PairComputer, response: Response):
            response.headers["Cache-Control"] = "no-store"
            host = "127.0.0.1" if body.browser == "local" else peer
            if host is None:
                raise HTTPException(
                    409, "Connect the Bud server to a private browser network first."
                )
            async with self.view.lock:
                if self.view.private:
                    raise HTTPException(423, "Finish private input before adding a computer.")
                path = self.view.locate()
                if path is None:
                    raise HTTPException(409, "Browse isn’t installed on the Bud server.")
                try:
                    code = await asyncio.to_thread(self.pairing.code, path)
                except ConnectionError as exc:
                    raise HTTPException(503, str(exc)) from None
            return {"code": code, "address": f"{host}:{self.view.relay.port}"}

        return api
