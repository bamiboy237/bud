"""Runs the agent for each session in a background task and translates Pydantic AI stream
events into bud events. A run that needs approval saves itself to the log and exits, and a new
run continues after the decision. On startup it closes runs a previous process left open.
"""

import asyncio
import json
from uuid import uuid4

from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolApproved, ToolDenied
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.run import AgentRunResultEvent

from bud.core.events import Event
from bud.core.hub import Hub
from bud.core.projection import decisions, model_history, undecided_approvals, waiting_batch
from bud.gateway import Gateway
from bud.gateway.registry import Tier
from bud.harness.agent import RUN_LIMITS, Harness


class RunInProgress(Exception):
    pass


class AwaitingApproval(Exception):
    pass


class UnknownApproval(Exception):
    pass


class Runner:
    def __init__(self, hub: Hub, gateway: Gateway, harness: Harness, tier: Tier = "fast") -> None:
        self.hub = hub
        self.gateway = gateway
        self.harness = harness
        self.tier = tier
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def recover(self) -> None:
        """The log only gains facts, so an interrupted run is closed with a new event."""
        for session_id, run_id in self.hub.log.unfinished_runs():
            self.hub.publish(session_id, "run.failed", {"run_id": run_id, "reason": "interrupted"})
        for session_id in self.hub.log.sessions_with_decisions():
            self._resume_if_ready(session_id)

    def start(self, session_id: str, text: str, message_id: str) -> Event | None:
        if session_id in self.tasks:
            raise RunInProgress(session_id)
        if undecided_approvals(self.hub.log.read(session_id)):
            raise AwaitingApproval(session_id)
        message = self.hub.publish_once(
            session_id, "user.message", {"text": text, "message_id": message_id}
        )
        if message is not None:
            self._launch(session_id, self._run(session_id, text))
        return message

    def decide(self, session_id: str, approval_id: str, approved: bool) -> bool:
        """Return True when this call made the decision, False when an earlier one did."""
        events = self.hub.log.read(session_id)
        requested = any(
            event.type == "approval.requested" and event.data["approval_id"] == approval_id
            for event in events
        )
        if not requested:
            raise UnknownApproval(approval_id)
        event_type = "approval.granted" if approved else "approval.denied"
        event = self.hub.publish_once(session_id, event_type, {"approval_id": approval_id})
        if event is None:
            return False
        self._resume_if_ready(session_id)
        return True

    def _resume_if_ready(self, session_id: str) -> None:
        events = self.hub.log.read(session_id)
        batch = waiting_batch(events)
        if not batch or undecided_approvals(events) or session_id in self.tasks:
            return
        decided = decisions(events)
        results = DeferredToolResults(
            approvals={
                request["call_id"]: (
                    ToolApproved()
                    if decided[request["approval_id"]]
                    else ToolDenied("The user denied this action.")
                )
                for request in batch
            },
            metadata={
                request["call_id"]: {"approval_id": request["approval_id"]} for request in batch
            },
        )
        self._launch(session_id, self._run(session_id, None, results))

    def _launch(self, session_id: str, run) -> None:
        task = asyncio.create_task(run)
        self.tasks[session_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(session_id, None))

    async def stop(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(
        self, session_id: str, text: str | None, results: DeferredToolResults | None = None
    ) -> None:
        run_id = uuid4().hex
        trigger = "message" if results is None else "approval"
        history = model_history(self.hub.log.read(session_id))
        self.hub.publish(
            session_id,
            "run.started",
            {"run_id": run_id, "trigger": trigger, "prompt": self.harness.prompt_version},
        )
        try:
            agent = self.harness.agent(
                self.gateway.model(self.tier, session_id),
                lambda decision: self.hub.publish(
                    session_id, "action.decided", {"run_id": run_id, **decision}
                ),
            )
            async with agent.run_stream_events(
                text,
                message_history=history,
                deferred_tool_results=results,
                usage_limits=RUN_LIMITS,
            ) as stream:
                async for event in stream:
                    self._translate(session_id, run_id, event)
        except asyncio.CancelledError:
            self.hub.publish(session_id, "run.failed", {"run_id": run_id, "reason": "cancelled"})
            raise
        except Exception as exc:
            self.hub.publish(
                session_id,
                "run.failed",
                {"run_id": run_id, "reason": f"{type(exc).__name__}: {exc}"[:500]},
            )

    def _translate(self, session_id: str, run_id: str, event: object) -> None:
        match event:
            case PartStartEvent(part=TextPart(content=text)) if text:
                self._text(session_id, run_id, text)
            case PartDeltaEvent(delta=TextPartDelta(content_delta=text)) if text:
                self._text(session_id, run_id, text)
            case FunctionToolCallEvent(part=part):
                self.hub.publish(
                    session_id,
                    "tool.call",
                    {
                        "run_id": run_id,
                        "call_id": part.tool_call_id,
                        "tool": part.tool_name,
                        "args": part.args_as_dict(),
                    },
                )
            case FunctionToolResultEvent() as result:
                self.hub.publish(
                    session_id,
                    "tool.result",
                    {
                        "run_id": run_id,
                        "call_id": result.tool_call_id,
                        "result": str(result.part.content),
                    },
                )
            case AgentRunResultEvent(result=result):
                messages = json.loads(result.new_messages_json())
                if isinstance(result.output, DeferredToolRequests):
                    for call in result.output.approvals:
                        self.hub.publish(
                            session_id,
                            "approval.requested",
                            {
                                "run_id": run_id,
                                "approval_id": uuid4().hex,
                                "call_id": call.tool_call_id,
                                "tool": call.tool_name,
                                "args": call.args_as_dict(),
                            },
                        )
                    self.hub.publish(
                        session_id, "run.waiting", {"run_id": run_id, "messages": messages}
                    )
                else:
                    self.hub.publish(
                        session_id, "run.completed", {"run_id": run_id, "messages": messages}
                    )

    def _text(self, session_id: str, run_id: str, text: str) -> None:
        self.hub.publish(session_id, "text.delta", {"run_id": run_id, "text": text})
