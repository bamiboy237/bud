"""Runs the agent for each session in a background task and translates Pydantic AI stream
events into bud events. On startup it also closes runs that a previous process left open.
"""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic_ai import Agent
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
from bud.core.projection import model_history
from bud.gateway import Gateway
from bud.gateway.registry import Tier


def current_time() -> str:
    """Return the current UTC date and time."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class RunInProgress(Exception):
    pass


class Runner:
    def __init__(self, hub: Hub, gateway: Gateway, tier: Tier = "fast") -> None:
        self.hub = hub
        self.gateway = gateway
        self.tier = tier
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def recover(self) -> None:
        """The log only gains facts, so an interrupted run is closed with a new event."""
        for session_id, run_id in self.hub.log.unfinished_runs():
            self.hub.publish(session_id, "run.failed", {"run_id": run_id, "reason": "interrupted"})

    def start(self, session_id: str, text: str) -> Event:
        if session_id in self.tasks:
            raise RunInProgress(session_id)
        message = self.hub.publish(session_id, "user.message", {"text": text})
        task = asyncio.create_task(self._run(session_id, text))
        self.tasks[session_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(session_id, None))
        return message

    async def stop(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, session_id: str, text: str) -> None:
        run_id = uuid4().hex
        self.hub.publish(session_id, "run.started", {"run_id": run_id, "trigger": "message"})
        try:
            history = model_history(self.hub.log.read(session_id))
            agent = Agent(self.gateway.model(self.tier, session_id), tools=[current_time])
            async with agent.run_stream_events(text, message_history=history) as stream:
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
                        "result": str(result.content),
                    },
                )
            case AgentRunResultEvent(result=result):
                self.hub.publish(
                    session_id,
                    "run.completed",
                    {"run_id": run_id, "messages": json.loads(result.new_messages_json())},
                )

    def _text(self, session_id: str, run_id: str, text: str) -> None:
        self.hub.publish(session_id, "text.delta", {"run_id": run_id, "text": text})
