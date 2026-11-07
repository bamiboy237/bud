"""Best-effort Photon Beta UI signals, never task or delivery confirmations."""

import asyncio
import logging
from collections import OrderedDict
from contextlib import suppress

from bud.photon_store import digest

logger = logging.getLogger(__name__)


class PhotonSignals:
    def __init__(self, api, project: str, check_scope) -> None:
        self.api, self.project, self.check_scope = api, project, check_scope
        self.enabled = {"read": True, "typing": True}
        self.reads: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
        self.active: dict[str, tuple[str, str, float]] = {}
        self.receipts: OrderedDict[str, str] = OrderedDict()

    def disable(self, kind: str) -> None:
        if self.enabled[kind]:
            self.enabled[kind] = False
            logger.warning(
                "Photon %s signal unavailable; disabled until restart. Chat remains enabled.", kind
            )

    def accepted(self, identity: str) -> None:
        if self.enabled["read"]:
            # Cosmetic signals must not block or reject real messages.
            with suppress(asyncio.QueueFull):
                self.reads.put_nowait(identity)

    def observe(self, event: dict, space: str, agent: str) -> None:
        data, kind = event["data"], event["type"]
        if kind in ("run.started", "run.recovered"):
            self.active[space] = (data["run_id"], agent, asyncio.get_running_loop().time())
        elif kind in (
            "run.waiting",
            "approval.requested",
            "run.completed",
            "run.failed",
            "run.cancelled",
            "run.stop_requested",
        ):
            current = self.active.get(space)
            if current and current[0] == data["run_id"]:
                self.active.pop(space, None)

    def track(self, response, kind: str) -> None:
        while hasattr(response, "root"):
            response = response.root
        identity = getattr(response, "id", None)
        if isinstance(identity, str):
            self.receipts[identity] = kind
            while len(self.receipts) > 256:
                self.receipts.popitem(last=False)
        if getattr(response, "status", None) == "failed":
            self.disable(kind)

    def delivery(self, identity: str, status: str) -> None:
        kind = self.receipts.get(identity)
        if kind and status == "failed":
            self.disable(kind)

    async def mark_read(self) -> None:
        from photon_api.rpc_generated import MarkMessageReadInput

        while True:
            identity = await self.reads.get()
            try:
                if self.enabled["read"]:
                    async with asyncio.timeout(2):
                        await self.check_scope()
                        result = await self.api.projects.mark_message_read(
                            MarkMessageReadInput.model_validate(
                                {
                                    "path": {"id": self.project, "messageId": identity},
                                    "headers": {"idempotency-key": digest("read:" + identity)},
                                }
                            )
                        )
                        self.track(result, "read")
            except Exception:
                # No replay/retry of a UI operation with an uncertain outcome.
                self.disable("read")
            finally:
                self.reads.task_done()

    async def typing(self, space: str, agent: str, value: bool) -> None:
        from photon_api.rpc_generated import SetSpaceTypingInput

        if not self.enabled["typing"]:
            return
        try:
            async with asyncio.timeout(2):
                await self.check_scope()
                result = await self.api.projects.set_space_typing(
                    SetSpaceTypingInput.model_validate(
                        {
                            "path": {"id": self.project, "spaceId": space},
                            "body": {"senderId": agent, "typing": value},
                        }
                    )
                )
                self.track(result, "typing")
        except Exception:
            self.disable("typing")

    async def refresh_typing(self) -> None:
        shown: dict[str, tuple[str, float]] = {}
        try:
            while True:
                now = asyncio.get_running_loop().time()
                # Do not refresh indefinitely during a long tool job.
                desired = {
                    space: agent
                    for space, (_, agent, start) in self.active.items()
                    if now - start < 45
                }
                for space, (agent, _) in list(shown.items()):
                    if space not in desired:
                        await self.typing(space, agent, False)
                        shown.pop(space)
                for space, agent in desired.items():
                    if space not in shown or now - shown[space][1] >= 8:
                        await self.typing(space, agent, True)
                        shown[space] = (agent, now)
                await asyncio.sleep(0.25)
        finally:
            for space, (agent, _) in shown.items():
                await self.typing(space, agent, False)
