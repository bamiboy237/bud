"""Owner-only Photon 2.0 iMessage transport for the existing authenticated core.

Socket.IO carries versioned events. The official Photon SDK sends plain text.
SQLite records transport claims/cursors; the core alone owns conversation history.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr, ValidationError
from starlette.types import ASGIApp

from bud.config import Settings
from bud.core.controls import approval_text, session_snapshot, submit
from bud.core.events import MessageOrigin
from bud.photon_config import PhotonConfig, owner_message
from bud.photon_store import TransportError, TransportStore, digest, fingerprint

API_VERSION = "2026-07-01"
logger = logging.getLogger(__name__)


def notification(event: dict, origin: MessageOrigin) -> str | None:
    """Final output comes from the completed core run, never a partial text stream."""
    data = event["data"]
    if data.get("origin") != origin.model_dump():
        return None
    match event["type"]:
        case "run.recovered":
            return "Recovering interrupted work; completed actions will not be requested again."
        case "approval.requested":
            return approval_text(data)
        case "run.completed":
            for message in reversed(data["messages"]):
                if message.get("kind") == "response":
                    text = "\n".join(
                        part["content"]
                        for part in message.get("parts", [])
                        if part.get("part_kind") == "text"
                    )
                    if text:
                        return text
            return "The request finished without a text reply. Use /status to inspect its state."
        case "run.failed":
            return (
                "The request stopped with an error. Use /status; do not repeat uncertain actions."
            )
        case "run.cancelled":
            uncertain = data.get("uncertain_tools", [])
            return (
                "Stopped; queue paused. Previously authorized actions may still have happened. "
                "Inspect their outcome before requesting them again."
                if uncertain
                else "Stopped; queue paused. Pending permissions are no longer usable."
            )
    return None


class PhotonChat:
    def __init__(
        self,
        settings: Settings,
        config: PhotonConfig,
        core: httpx.AsyncClient,
        api,
        runtime: dict,
        *,
        events: Callable[[str, int], AsyncIterator[dict]],
        status: Callable[[str], None],
    ) -> None:
        self.config, self.core, self.api, self.runtime = config, core, api, runtime
        self.session: str | None = None
        self.session_seq = 0
        self.events = events
        self.status = status
        consumer = digest(f"{config.project}:{config.owner}:{config.agent}:{core.base_url}")
        root = settings.data_dir / "photon"
        # Check each application-owned parent rather than following a preexisting
        # symlink out of private state.
        for folder in (root, root / consumer):
            folder.mkdir(mode=0o700, exist_ok=True)
            info = folder.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise TransportError("Photon state parents must be owned private directories.")
        self.store = TransportStore(root / consumer / digest(runtime["scope"]))
        self.ephemeral: dict[str, str] = {}
        self.followers: dict[str, asyncio.Task] = {}
        self.deliveries = asyncio.Queue(maxsize=128)
        self.stopping = asyncio.Event()
        self.ready = asyncio.Event()
        self.failure: str | None = None
        self.epoch = 0
        if not self.store.get("not_before"):
            self.store.set("not_before", datetime.now(UTC).isoformat())

    def fail(self, reason: str) -> None:
        self.failure = reason
        self.status("failed")
        self.stopping.set()

    def origin(self, space: str) -> MessageOrigin:
        return MessageOrigin(channel="imessage", client_id=digest(f"{self.config.project}:{space}"))

    async def resolve_conversation(self) -> None:
        routes = self.store.routes()
        if self.session is None:
            legacy = {route["session"] for route in routes}
            if len(legacy) > 1:
                raise TransportError(
                    "Multiple saved iMessage conversations need review; none were merged."
                )
            response = await self.core.post(
                "/sessions/primary",
                json={"session_id": next(iter(legacy))} if legacy else None,
            )
            if response.status_code == 409:
                raise TransportError(
                    "The saved iMessage conversation differs from the primary conversation. "
                    "No histories were merged. Attach explicitly to review them."
                )
            response.raise_for_status()
            self.session = response.json()["session_id"]
            snapshot = await session_snapshot(self.core, self.session)
            self.session_seq = snapshot["last_seq"]
        if any(route["session"] != self.session for route in routes):
            raise TransportError(
                "The saved channel route belongs to another conversation; it was not rebound."
            )

    async def check_scope(self) -> dict:
        response = await self.core.get("/runtime")
        response.raise_for_status()
        current = response.json()
        if current["scope"] != self.runtime["scope"] or current["mode"] != self.runtime["mode"]:
            raise TransportError(
                "Bud's account scope or mode changed. Restart the adapter; "
                "old approvals stay isolated."
            )
        return current

    async def ensure_session(self, route: dict) -> None:
        response = await self.core.get(f"/sessions/{route['session']}")
        if response.status_code == 404:
            raise TransportError("The saved core conversation is unavailable; it was not replaced.")
        response.raise_for_status()
        self.store.ready(route["space"])
        if route["space"] not in self.followers:
            self.followers[route["space"]] = asyncio.create_task(self.follow(route))

    def enqueue(self, route: dict, identity: str, text: str, *, private_link: bool = False) -> None:
        if self.runtime["mode"] == "simulate":
            text = "[Simulation]\n" + text
        for key, body in self.store.enqueue(route, identity, text, persist=not private_link):
            if private_link:
                # OAuth links are immediate UI output only, not durable transport
                # history. If this process dies, request a new link explicitly.
                self.ephemeral[key] = body

    async def receive(self, event: dict, meta: dict) -> None:
        from photon_api.generated.models import ReceivedImessageMessage

        seq = meta.get("seq")
        if type(seq) is not int or not 0 < seq <= 9007199254740991:
            raise TransportError("Invalid Photon replay cursor.")
        if seq <= self.store.cursor:
            return
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            raise TransportError("Malformed Photon event; cursor was not advanced.")
        kind = event.get("type")
        if kind != "message.received" or event.get("source") != "imessage.photon":
            # Delivery status is evidence only for an outgoing resource already
            # recorded locally; it can never grant permission or start model work.
            if (
                kind in ("message.delivered", "message.failed", "message.dispatched")
                and event.get("apiVersion") == API_VERSION
                and event.get("source") == "imessage.photon"
                and isinstance(event.get("data"), dict)
                and event["data"].get("projectId") == self.config.project
            ):
                item = event["data"].get("object", {})
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    self.store.delivery(item["id"], kind.removeprefix("message."))
            self.store.set("cursor", str(seq))
            return
        if (
            event.get("apiVersion") != API_VERSION
            or not isinstance(event.get("data"), dict)
            or event["data"].get("projectId") != self.config.project
        ):
            raise TransportError("Photon event contract or project mismatch.")
        try:
            message = ReceivedImessageMessage.model_validate(event["data"]["object"])
        except (ValidationError, KeyError):
            raise TransportError(
                "Unsupported received iMessage shape; no action was taken."
            ) from None
        if not owner_message(message, self.config):
            self.store.set("cursor", str(seq))
            return  # No reply to unknown people, group chats, unsupported content or echoes.
        if datetime.fromisoformat(message.createdAt) < datetime.fromisoformat(
            self.store.get("not_before")
        ):
            self.store.set("cursor", str(seq))
            return
        await self.check_scope()
        text = message.content.text
        effective = {
            "space": message.space.root.id,
            "sender": message.sentBy.root.id,
            "agent": message.receivedBy.root.id,
            "text": text,
        }
        state = self.store.accept(message.id, fingerprint(effective))
        if state == "done":
            self.store.complete(message.id, seq)
            return
        route = self.store.route(
            message.space.root.id,
            message.receivedBy.root.id,
            message.id,
            self.session,
            self.session_seq,
        )
        await self.ensure_session(route)
        # Explicit session attachment starts at the current end, not a replay of
        # private old replies into a newly linked messaging conversation.
        is_control = text.lstrip().startswith("/")
        if text.strip().lower() == "ping":
            result = "Bud connection verified."
        elif state == "dispatching" and is_control:
            result = (
                "Your earlier control may already have applied. I did not repeat it. "
                "Use /status to inspect the current state."
            )
        else:
            try:
                result = await submit(
                    self.core,
                    route["session"],
                    text,
                    message_id=digest("photon:" + message.id),
                    origin=self.origin(route["space"]),
                )
            except httpx.HTTPStatusError as exc:
                result = (
                    f"Bud declined this request (HTTP {exc.response.status_code}). "
                    "Use /status or restart Bud if its account binding changed."
                )
            # Network failures propagate. An ordinary message can safely replay
            # with its stable core ID; controls retain a non-retryable claim.
        if text.strip() == "/status":
            result += (
                f"\nCore session: {route['session']}\n"
                f"Transport deliveries needing review: {self.store.uncertain()}"
            )
        if result:
            self.enqueue(
                route,
                "incoming:" + message.id,
                result,
                private_link=text.split(maxsplit=1)[0] == "/connect",
            )
        self.store.complete(message.id, seq)

    async def follow(self, initial: dict) -> None:
        """Project committed core events into durable, deduplicated notifications."""
        space = initial["space"]
        while not self.stopping.is_set():
            route = next(item for item in self.store.routes() if item["space"] == space)
            try:
                await self.check_scope()
                async for event in self.events(route["session"], route["core_seq"]):
                    route = next(item for item in self.store.routes() if item["space"] == space)
                    text = notification(event, self.origin(space))
                    self.store.project(route, event["seq"], text)
            except Exception:
                self.fail("Core event projection stopped; its cursor was preserved.")
                return

    async def send(self) -> None:
        from photon_api import PhotonError
        from photon_api.rpc_generated import SendMessageInput

        while not self.stopping.is_set():
            try:
                await self.check_scope()
                item = self.store.claim()
                if item is None:
                    await asyncio.sleep(0.25)
                    continue
                body = item["body"] or self.ephemeral.pop(item["id"], None)
                if body is None:
                    self.store.sent(item["id"], None, "unknown")
                    continue
                # Recheck immediately before dispatch, but do not claim atomicity
                # with account refresh or attempt to recall an in-flight message.
                await self.check_scope()
                try:
                    async with asyncio.timeout(15):
                        response = await self.api.projects.send_message(
                            SendMessageInput.model_validate(
                                {
                                    "path": {"id": self.config.project},
                                    "headers": {"idempotency-key": item["id"]},
                                    "body": {
                                        "spaceId": item["space"],
                                        "senderId": item["agent"],
                                        "parentMessageId": item["parent"],
                                        "content": {
                                            "type": "text",
                                            "format": "plain",
                                            "text": body,
                                        },
                                    },
                                }
                            )
                        )
                    receipt = response
                    while hasattr(receipt, "root"):
                        receipt = receipt.root
                    self.store.sent(
                        item["id"],
                        getattr(receipt, "id", None),
                        getattr(receipt, "status", "accepted"),
                    )
                except (PhotonError, TimeoutError):
                    self.store.sent(item["id"], None, "unknown")
                    # A partial multi-message approval must not continue with
                    # missing details. Other pending groups remain recoverable.
                    self.fail("Photon delivery is uncertain. Inspect the chat before restarting.")
            except Exception:
                self.fail("Photon sending stopped; dispatch claims were preserved.")

    async def consume(self) -> None:
        while not self.stopping.is_set():
            generation, event, meta = await self.deliveries.get()
            try:
                if generation == self.epoch:
                    await self.receive(event, meta)
            except Exception:
                self.fail("Incoming processing stopped; the last completed cursor was preserved.")
            finally:
                self.deliveries.task_done()

    async def run(self) -> None:
        import aiohttp
        import socketio

        async with aiohttp.ClientSession(trust_env=False) as http:
            socket = socketio.AsyncClient(
                http_session=http, logger=False, engineio_logger=False, reconnection=True
            )

            @socket.event
            async def connect():
                try:
                    ack = await socket.call(
                        "subscribe",
                        {"startSeq": self.store.cursor, "apiVersion": API_VERSION},
                        timeout=10,
                    )
                    if (
                        ack.get("ok") is not True
                        or ack.get("apiVersion") != API_VERSION
                        or ack.get("projectId") != self.config.project
                    ):
                        raise TransportError("Photon rejected the pinned subscription.")
                    print(
                        f"Photon iMessage ready ({self.runtime['mode']}). "
                        "Only the configured owner direct conversation is accepted.",
                        flush=True,
                    )
                    self.status("connected")
                    self.ready.set()
                except Exception:
                    self.fail("Photon subscription failed; check project access and API version.")

            @socket.event
            async def disconnect(*_):
                self.epoch += 1
                if not self.stopping.is_set():
                    self.status("reconnecting")

            @socket.on("delivery")
            async def delivery(event, meta):
                try:
                    self.deliveries.put_nowait((self.epoch, event, meta))
                except asyncio.QueueFull:
                    self.fail("Incoming queue limit reached; restart to replay retained events.")

            @socket.on("delivery_error")
            async def delivery_error(_):
                self.fail("Photon reported a delivery-stream error; the cursor was preserved.")

            tasks = []
            try:
                await self.resolve_conversation()
                for route in self.store.routes():
                    await self.ensure_session(route)
                tasks = [asyncio.create_task(self.consume()), asyncio.create_task(self.send())]
                await socket.connect(
                    "https://api.photon.codes",
                    headers={"Authorization": f"Bearer {self.config.key.get_secret_value()}"},
                    transports=["websocket"],
                    socketio_path=f"v1/projects/{self.config.project}/events/ws",
                    wait_timeout=15,
                )
                tasks.append(asyncio.create_task(socket.wait()))
                stopped = asyncio.create_task(self.stopping.wait())
                tasks.append(stopped)
                await asyncio.wait((tasks[-2], stopped), return_when=asyncio.FIRST_COMPLETED)
                if not self.stopping.is_set():
                    self.fail("Photon stream ended. Restart bud to reconnect using saved state.")
            except TransportError as exc:
                self.fail(str(exc))
            except Exception:
                self.fail(
                    "Photon connection stopped; inspect access, connectivity and saved state."
                )
            finally:
                try:
                    await socket.disconnect()
                finally:
                    try:
                        for task in [*tasks, *self.followers.values()]:
                            task.cancel()
                        await asyncio.gather(
                            *tasks, *self.followers.values(), return_exceptions=True
                        )
                    finally:
                        self.store.close()
            if self.failure:
                raise TransportError(self.failure)


@asynccontextmanager
async def messaging(
    settings: Settings,
    config: PhotonConfig,
    app: ASGIApp,
    token: SecretStr,
    runtime: dict,
    events: Callable[[str, int], AsyncIterator[dict]],
    status: Callable[[str], None],
) -> AsyncIterator[None]:
    """One server-owned lifetime; in-process commands keep the normal auth/scope gates."""
    from photon_api import AsyncPhoton

    async with (
        httpx.AsyncClient(
            # Preserve the previous default adapter's consumer identity/state.
            # ASGITransport dispatches locally; no loopback listener is needed.
            base_url="http://127.0.0.1:8420",
            transport=httpx.ASGITransport(app=app),
            headers={
                "Authorization": f"Bearer {token.get_secret_value()}",
                "X-Bud-Scope": runtime["scope"],
            },
            trust_env=False,
            follow_redirects=False,
        ) as core,
        httpx.AsyncClient(trust_env=False, follow_redirects=False) as http,
        AsyncPhoton(
            headers={"Authorization": f"Bearer {config.key.get_secret_value()}"},
            client=http,
            timeout=10,
            max_attempts=1,
        ) as api,
    ):
        adapter = PhotonChat(settings, config, core, api, runtime, events=events, status=status)
        service = asyncio.create_task(adapter.run())
        ready = asyncio.create_task(adapter.ready.wait())

        def finished(task: asyncio.Task) -> None:
            if not task.cancelled():
                error = task.exception()
                status("failed")
                logger.error(
                    "%s",
                    str(error)
                    if isinstance(error, TransportError)
                    else "iMessage service stopped. Inspect state and restart bud.",
                )

        service.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait(
                (service, ready), timeout=30, return_when=asyncio.FIRST_COMPLETED
            )
            if service in done:
                await service
                raise TransportError("iMessage service stopped during startup.")
            if ready not in done:
                raise TransportError("iMessage startup timed out; inspect state before restarting.")
            yield
        finally:
            adapter.stopping.set()
            ready.cancel()
            service.cancel()
            await asyncio.gather(ready, service, return_exceptions=True)
            status("stopped")
