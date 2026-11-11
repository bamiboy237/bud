"""Owner-only Photon v2 text transport for Bud's existing conversation core."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr, ValidationError
from starlette.types import ASGIApp

from bud.config import Settings
from bud.core.controls import session_snapshot, submit
from bud.core.events import MessageOrigin
from bud.photon_config import PhotonConfig, owner_message
from bud.photon_store import (
    TransportError,
    TransportStore,
    digest,
    fingerprint,
    private_directory,
    proposal_hash,
)
from bud.photon_ui import action_preview, status_text

API_VERSION = "2026-07-01"
logger = logging.getLogger(__name__)


def permission_intent(text: str) -> bool:
    """Recognize unbound consent so it cannot accidentally become permission."""
    return text.strip().casefold().rstrip(".!") in {
        "yes",
        "yep",
        "yes please",
        "approve",
        "go ahead",
        "do it",
        "👍",
    }


def notification(event: dict, origin: MessageOrigin) -> str | None:
    data = event["data"]
    if data.get("origin") != origin.model_dump():
        return None
    match event["type"]:
        case "approval.requested" | "task.approval":
            return action_preview(data)
        case "task.completed" | "task.failed" | "task.cancelled":
            return f"Task {data['task_id']}: {data['text']}"
        case "run.completed":
            if data.get("quiet"):
                return None
            for message in reversed(data["messages"]):
                if message.get("kind") == "response":
                    body = "\n".join(
                        part["content"]
                        for part in message.get("parts", [])
                        if part.get("part_kind") == "text"
                    )
                    if body:
                        return body
        case "run.failed":
            return "That didn't finish. I haven't retried it."
        case "run.cancelled":
            return (
                "Stopped. An action already in progress may still complete."
                if data.get("uncertain_tools")
                else "Stopped."
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
        self.events, self.status = events, status
        self.session: str | None = None
        self.session_seq = 0
        # Preserve the published v2 consumer identity, routes and replay cursor.
        consumer = digest(f"{config.project}:{config.owner}:{config.agent}:{core.base_url}")
        root = settings.data_dir / "photon"
        for folder in (root, root / consumer):
            private_directory(folder)
        self.store = TransportStore(root / consumer / digest(runtime["scope"]))
        self.ephemeral: dict[str, str] = {}
        self.followers: dict[str, asyncio.Task] = {}
        self.deliveries: asyncio.Queue = asyncio.Queue(maxsize=128)
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

    async def check_scope(self) -> None:
        response = await self.core.get("/runtime")
        response.raise_for_status()
        current = response.json()
        # Connection controls, denial and Stop remain deliverable during account refresh.
        if current["scope"] != self.runtime["scope"] or current["mode"] != self.runtime["mode"]:
            raise TransportError("Bud's account connection changed. Restart before continuing.")

    async def resolve_conversation(self) -> None:
        saved = {route["session"] for route in self.store.routes()}
        if len(saved) > 1:
            raise TransportError("Saved conversations need review; none were merged.")
        response = await self.core.post(
            "/sessions/primary", json={"session_id": next(iter(saved))} if saved else None
        )
        response.raise_for_status()
        self.session = response.json()["session_id"]
        if saved and saved != {self.session}:
            raise TransportError("The saved conversation was not rebound.")
        self.session_seq = (await session_snapshot(self.core, self.session))["last_seq"]

    async def ensure_session(self, route: dict) -> None:
        await session_snapshot(self.core, route["session"])
        self.store.ready(route["space"])
        if route["space"] not in self.followers:
            self.followers[route["space"]] = asyncio.create_task(self.follow(route))

    def enqueue(
        self,
        route: dict,
        identity: str,
        body: str,
        *,
        private_link: bool = False,
        approval: dict | None = None,
    ) -> None:
        for key, value in self.store.enqueue(
            route, identity, body, persist=not private_link, approval=approval
        ):
            if private_link:
                # Authorization links never enter durable transport bodies or model history.
                self.ephemeral[key] = value

    async def receive(self, event: dict, meta: dict) -> None:
        from photon_api.generated.models import ReceivedImessageMessage

        seq = meta.get("seq")
        if type(seq) is not int or not 0 < seq <= 9007199254740991:
            raise TransportError("Invalid Photon replay cursor.")
        if seq <= self.store.cursor:
            return
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            raise TransportError("Malformed Photon event; its cursor was preserved.")
        kind = event.get("type")
        if kind != "message.received" or event.get("source") != "imessage.photon":
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
                "Unsupported incoming iMessage; its cursor was preserved."
            ) from None
        if not owner_message(message, self.config) or (
            datetime.fromisoformat(message.createdAt)
            < datetime.fromisoformat(self.store.get("not_before"))
        ):
            self.store.set("cursor", str(seq))
            return
        await self.check_scope()
        parent = message.parentMessageId if isinstance(message.parentMessageId, str) else None
        # Keep the existing v2 digest algorithm for duplicates retained across restarts.
        state = self.store.accept(
            message.id,
            fingerprint(
                {
                    "space": message.space.root.id,
                    "sender": message.sentBy.root.id,
                    "agent": message.receivedBy.root.id,
                    "text": message.content.text,
                }
            ),
            parent=parent,
        )
        if state == "done":
            self.store.complete(message.id, seq)
            return
        assert self.session is not None
        route = self.store.route(
            message.space.root.id,
            message.receivedBy.root.id,
            message.id,
            self.session,
            self.session_seq,
        )
        await self.ensure_session(route)
        text = message.content.text.strip()
        command = text.casefold()
        controls = {
            "stop": "/stop",
            "stop please": "/stop",
            "cancel": "/stop",
            "pause": "/pause",
            "resume": "/resume",
        }
        is_control = text.startswith("/") or command in controls or permission_intent(text)
        if state == "dispatching" and is_control:
            self.enqueue(
                route,
                "replay:" + message.id,
                "That reply may already have been handled. Send /status to check.",
            )
        else:
            try:
                snapshot = await session_snapshot(self.core, route["session"])
                pending = snapshot["pending_approvals"]
                question = next((p for p in pending if p["tool"] == "ask_user"), None)
                if (
                    question is not None
                    and len(pending) == 1
                    and not is_control
                    and command not in controls
                ):
                    # A question blocks the run, so a plain reply is the answer.
                    result = await submit(
                        self.core,
                        route["session"],
                        f"/answer {question['approval_id']} {text.strip()}",
                        message_id=digest("photon:" + message.id),
                        origin=self.origin(route["space"]),
                    )
                    if result and result.startswith("[not a pending question]"):
                        result = "That question is no longer waiting. Send /status to check."
                    self.enqueue(route, "control:" + message.id, result or "Got it.")
                elif command in ("status", "/status") or (permission_intent(text) and pending):
                    for proposal in pending:
                        self.enqueue(
                            route,
                            "status:" + message.id + ":" + proposal_hash(proposal),
                            action_preview(proposal),
                            approval=proposal,
                        )
                    if not snapshot["pending_approvals"]:
                        self.enqueue(route, "status:" + message.id, status_text(snapshot))
                else:
                    result = await submit(
                        self.core,
                        route["session"],
                        controls.get(command, message.content.text),
                        message_id=digest("photon:" + message.id),
                        origin=self.origin(route["space"]),
                    )
                    # No duplicate approval bubble: the continuation reports the actual result.
                    if result and not result.startswith(("[approved]", "[denied]")):
                        result = {
                            "[queue paused]": "Paused.",
                            "[queue resumed]": "Back on it.",
                            "[queue paused; no active run to stop]": "Stopped.",
                            "[already decided]": "That request is no longer waiting.",
                        }.get(result, result)
                        if result.startswith("[stop requested;"):
                            result = "Stopping."
                        elif result.startswith("[not pending]"):
                            result = (
                                "That request is no longer waiting. "
                                "Send /status for current changes."
                            )
                        self.enqueue(
                            route,
                            "control:" + message.id,
                            result,
                            private_link=text.split(maxsplit=1)[0] == "/connect",
                        )
            except httpx.HTTPStatusError as exc:
                self.enqueue(
                    route,
                    "declined:" + message.id,
                    "Your connection changed. Restart Bud before we continue."
                    if exc.response.status_code == 409
                    else "I couldn't accept that request. Nothing new was approved.",
                )
        self.store.complete(message.id, seq)

    async def follow(self, route: dict) -> None:
        try:
            async for event in self.events(route["session"], route["core_seq"]):
                data = event["data"]
                body = notification(event, self.origin(route["space"]))
                approval = (
                    data
                    if body and event["type"] in ("approval.requested", "task.approval")
                    else None
                )
                if approval:
                    snapshot = await session_snapshot(self.core, route["session"])
                    if not any(
                        proposal_hash(p) == proposal_hash(approval)
                        for p in snapshot["pending_approvals"]
                    ):
                        body, approval = None, None
                self.store.project(route, event["seq"], body, approval=approval)
            if not self.stopping.is_set():
                self.fail("The conversation connection stopped.")
        except Exception:
            self.fail("The conversation connection stopped; its cursor was preserved.")

    async def send(self) -> None:
        from photon_api import PhotonError
        from photon_api.rpc_generated import SendMessageInput

        while not self.stopping.is_set():
            await self.check_scope()
            item = self.store.claim()
            if item is None:
                await asyncio.sleep(0.25)
                continue
            if item["proposal_hash"]:
                route = next(r for r in self.store.routes() if r["space"] == item["space"])
                state = await session_snapshot(self.core, route["session"])
                if not any(
                    proposal_hash(p) == item["proposal_hash"] for p in state["pending_approvals"]
                ):
                    self.store.block(item["id"])
                    continue
            body = item["body"] or self.ephemeral.pop(item["id"], None)
            if body is None:
                self.store.sent(item["id"], None, "unknown")
                continue
            await self.check_scope()
            try:
                async with asyncio.timeout(15):
                    receipt = await self.api.projects.send_message(
                        SendMessageInput.model_validate(
                            {
                                "path": {"id": self.config.project},
                                "headers": {"idempotency-key": item["id"]},
                                "body": {
                                    "spaceId": item["space"],
                                    "senderId": item["agent"],
                                    "content": {"type": "text", "format": "plain", "text": body},
                                },
                            }
                        )
                    )
                while hasattr(receipt, "root"):
                    receipt = receipt.root
                identity, status = (
                    getattr(receipt, "id", None),
                    getattr(receipt, "status", "unknown"),
                )
                if not isinstance(identity, str) or not identity:
                    identity, status = None, "unknown"
                elif not isinstance(status, str):
                    status = "unknown"
                self.store.sent(item["id"], identity, status if identity else "unknown")
                if not identity or status in ("failed", "unknown"):
                    self.fail("An iMessage reply needs checking before retrying.")
            except (PhotonError, TimeoutError):
                self.store.sent(item["id"], None, "unknown")
                self.fail("An iMessage delivery needs checking before retrying.")

    async def confirm_previews(self) -> None:
        """Recover missed receipt events by inspecting only our known preview sends."""
        from photon_api import PhotonError
        from photon_api.rpc_generated import GetMessageInput

        attempts: dict[str, int] = {}
        while not self.stopping.is_set():
            for item in self.store.waiting_deliveries():
                await self.check_scope()
                try:
                    async with asyncio.timeout(3):
                        message = await self.api.projects.get_message(
                            GetMessageInput.model_validate(
                                {
                                    "path": {
                                        "id": self.config.project,
                                        "messageId": item["receipt"],
                                    },
                                }
                            )
                        )
                    while hasattr(message, "root"):
                        message = message.root
                    status = getattr(message, "status", None)
                    if (
                        getattr(message, "id", None) == item["receipt"]
                        and getattr(message, "projectId", None) == self.config.project
                        and status in ("delivered", "failed")
                    ):
                        self.store.delivery(item["receipt"], status)
                        attempts.pop(item["id"], None)
                        continue
                except (PhotonError, TimeoutError):
                    pass
                attempts[item["id"]] = attempts.get(item["id"], 0) + 1
                if attempts[item["id"]] >= 15:
                    self.store.sent(item["id"], item["receipt"], "unknown")
                    self.fail(
                        "A preview could not be delivered completely. Nothing new was approved."
                    )
            await asyncio.sleep(1)

    async def consume(self) -> None:
        while not self.stopping.is_set():
            epoch, event, meta = await self.deliveries.get()
            try:
                if epoch == self.epoch:
                    await self.receive(event, meta)
            finally:
                self.deliveries.task_done()

    async def run(self) -> None:
        import aiohttp
        import socketio

        tasks = []
        try:
            await self.resolve_conversation()
            for route in self.store.routes():
                await self.ensure_session(route)
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
                        self.status("connected")
                        self.ready.set()
                        print("Photon v2 text messaging connected.", flush=True)
                    except Exception:
                        self.fail(
                            "Photon subscription failed; check project access and API version."
                        )

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
                        self.fail("Incoming queue is full; the replay cursor was preserved.")

                @socket.on("delivery_error")
                async def delivery_error(_):
                    self.fail("Photon reported a stream error; the replay cursor was preserved.")

                try:
                    await socket.connect(
                        "https://api.photon.codes",
                        headers={"Authorization": f"Bearer {self.config.key.get_secret_value()}"},
                        transports=["websocket"],
                        socketio_path=f"v1/projects/{self.config.project}/events/ws",
                        wait_timeout=15,
                    )
                    tasks = [
                        asyncio.create_task(self.consume()),
                        asyncio.create_task(self.send()),
                        asyncio.create_task(self.confirm_previews()),
                        asyncio.create_task(self.stopping.wait()),
                        asyncio.create_task(socket.wait()),
                    ]
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    if not self.stopping.is_set():
                        self.fail("The iMessage connection stopped.")
                finally:
                    await socket.disconnect()
        except Exception as exc:
            logger.error("Photon messaging failure: %s", type(exc).__name__)
            self.fail("Photon v2 could not start or continue.")
        finally:
            for task in [*tasks, *self.followers.values()]:
                task.cancel()
            await asyncio.gather(*tasks, *self.followers.values(), return_exceptions=True)
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
    from photon_api import AsyncPhoton

    async with (
        httpx.AsyncClient(
            base_url="http://127.0.0.1:8420",
            transport=httpx.ASGITransport(app),
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
        try:
            done, _ = await asyncio.wait(
                (service, ready), timeout=30, return_when=asyncio.FIRST_COMPLETED
            )
            if service in done:
                await service
                raise TransportError("iMessage stopped during startup.")
            if ready not in done:
                raise TransportError("iMessage startup timed out.")
            yield
        finally:
            adapter.stopping.set()
            ready.cancel()
            service.cancel()
            await asyncio.gather(ready, service, return_exceptions=True)
            status("stopped")
