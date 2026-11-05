"""Agent runs with durable inputs, DBOS recovery, and application-owned approvals."""

import asyncio
import json
from collections.abc import AsyncIterable
from contextvars import ContextVar
from dataclasses import replace
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

from dbos import DBOS, DBOSConfiguredInstance, SetWorkflowID
from pydantic import TypeAdapter
from pydantic_ai import (
    ConversationTypeAdapter,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    ToolApproved,
    ToolDenied,
)
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessagesTypeAdapter,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from bud.core.events import Event
from bud.core.hub import Hub
from bud.core.projection import decisions, model_history, undecided_approvals, waiting_batch
from bud.core.runtime import replay_version
from bud.gateway import Gateway
from bud.gateway.errors import error_summary
from bud.gateway.registry import Tier
from bud.gateway.tracked import request_session
from bud.harness.agent import RUN_LIMITS, Harness
from bud.harness.context import BackgroundCompaction

RESULTS = TypeAdapter(DeferredToolResults)


class RunInProgress(Exception):
    pass


class AwaitingApproval(Exception):
    pass


class UnknownApproval(Exception):
    pass


class RecoveryRefused(Exception):
    pass


@DBOS.dbos_class(class_name="bud.runner")
class Runner(DBOSConfiguredInstance):
    def __init__(
        self,
        hub: Hub,
        gateway: Gateway,
        harness: Harness,
        tier: Tier = "fast",
        *,
        durable: bool = False,
    ) -> None:
        self.hub = hub
        self.gateway = gateway
        self.harness = harness
        self.tier = tier
        self.durable = durable
        self.tasks: dict[str, asyncio.Task] = {}
        self._monitors: set[asyncio.Task] = set()
        self._current: ContextVar[dict] = ContextVar("bud_run")
        self.version = replay_version()
        self.fingerprint = sha256(
            (
                harness.prompt_version
                + gateway.registry.model_dump_json()
                + json.dumps(harness.policy.tools, sort_keys=True)
                + str(gateway.selected_model)
                + tier
                + str(harness.results.retention.total_seconds())
            ).encode()
        ).hexdigest()
        if durable:
            super().__init__("core")
        durability = (
            DBOSDurability(
                name="bud",
                event_stream_handler=self._stream,
                parallel_execution_mode="sequential",
                model_step_config={"retries_allowed": False},
                mcp_step_config={"retries_allowed": False},
            )
            if durable
            else None
        )
        self.context = BackgroundCompaction(
            spend_for_run=harness.spend_for_run, record=self._record_context
        )
        self.agent = harness.agent(
            gateway.model(tier),
            self._record_decision,
            context=self.context,
            durability=durability,
        )

    def recover(self) -> None:
        pending = self.hub.log.pending_runs()
        known = {run["run_id"] for run in pending}
        # Old runs have no immutable inputs or journal and cannot be replayed safely.
        for session_id, run_id in self.hub.log.unfinished_runs():
            if run_id not in known:
                self.hub.publish(
                    session_id,
                    "run.failed",
                    {
                        "run_id": run_id,
                        "reason": "interrupted before durable execution was enabled",
                    },
                )
        for run in pending:
            self.hub.publish(run["session_id"], "run.recovered", {"run_id": run["run_id"]})
            self._launch(run, recovering=True)
        for session_id in self.hub.log.sessions_with_decisions():
            self._resume_if_ready(session_id)

    def _inputs(
        self, session_id: str, text: str | None, results: DeferredToolResults | None = None
    ) -> dict:
        events = self.hub.log.read(session_id)
        conversation = next(
            (
                event.data.get("conversation")
                for event in reversed(events)
                if event.type in ("run.waiting", "run.completed", "run.failed")
            ),
            None,
        )
        return {
            "session_id": session_id,
            "run_id": uuid4().hex,
            "text": text,
            "trigger": "message" if results is None else "approval",
            "conversation": conversation,
            "history": json.loads(ModelMessagesTypeAdapter.dump_json(model_history(events)))
            if conversation is None
            else None,
            "results": RESULTS.dump_json(results).decode() if results is not None else None,
            "config": self.fingerprint,
            "version": self.version,
        }

    def start(self, session_id: str, text: str, message_id: str) -> Event | None:
        if self._busy(session_id):
            raise RunInProgress(session_id)
        if undecided_approvals(self.hub.log.read(session_id)):
            raise AwaitingApproval(session_id)
        run = self._inputs(session_id, text)
        event = self.hub.log.enqueue_message(
            session_id, {"text": text, "message_id": message_id}, run
        )
        if event is not None:
            self.hub.wake(session_id)
            self._launch(run)
        return event

    def decide(self, session_id: str, approval_id: str, approved: bool) -> bool:
        events = self.hub.log.read(session_id)
        if not any(
            event.type == "approval.requested" and event.data["approval_id"] == approval_id
            for event in events
        ):
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
        if not batch or undecided_approvals(events) or self._busy(session_id):
            return
        decided = decisions(events)
        results = DeferredToolResults(
            approvals={
                request["call_id"]: ToolApproved()
                if decided[request["approval_id"]]
                else ToolDenied("The user denied this action.")
                for request in batch
            },
            metadata={
                request["call_id"]: {"approval_id": request["approval_id"]} for request in batch
            },
        )
        run = self._inputs(session_id, None, results)
        self.hub.publish_once(session_id, "run.queued", run, key=f"{run['run_id']}:queued")
        self._launch(run)

    def _launch(self, run: dict, *, recovering: bool = False) -> None:
        session_id = run["session_id"]
        if self.durable:
            task = asyncio.create_task(self._wait_durable(run, recovering))
        else:
            task = asyncio.create_task(self._execute(run))
        self.tasks[session_id] = task
        self._monitors.add(task)
        task.add_done_callback(lambda done: self._finished(session_id, run["run_id"], done))

    def _handle(self, run: dict, recovering: bool):
        status = DBOS.get_workflow_status(run["run_id"]) if recovering else None
        if (
            run.get("version") != self.version
            or run["config"] != self.fingerprint
            or (status is not None and status.app_version != self.version)
        ):
            if status is not None:
                DBOS.cancel_workflow(run["run_id"])
            raise RecoveryRefused(
                "Code or run configuration changed; recovery stopped before tools."
            )
        if status is not None:
            return DBOS.retrieve_workflow(run["run_id"])
        with SetWorkflowID(run["run_id"]):
            return DBOS.start_workflow(self._workflow, run)

    async def _wait_durable(self, run: dict, recovering: bool) -> None:
        await asyncio.to_thread(self._handle, run, recovering)
        handle = await DBOS.retrieve_workflow_async(run["run_id"])
        await handle.get_result()

    def _busy(self, session_id: str) -> bool:
        if session_id not in self.tasks:
            return False
        last = next(
            (
                event
                for event in reversed(self.hub.log.read(session_id))
                if event.type
                in ("run.queued", "run.started", "run.waiting", "run.completed", "run.failed")
            ),
            None,
        )
        if last is not None and last.type in ("run.waiting", "run.completed", "run.failed"):
            self.tasks.pop(session_id)
            return False
        return True

    def _finished(self, session_id: str, run_id: str, task: asyncio.Task) -> None:
        self._monitors.discard(task)
        if self.tasks.get(session_id) is task:
            self.tasks.pop(session_id)
        if not task.cancelled() and task.exception() is not None:
            self.hub.publish_once(
                session_id,
                "run.failed",
                {
                    "run_id": run_id,
                    "reason": str(task.exception())
                    if isinstance(task.exception(), RecoveryRefused)
                    else error_summary(task.exception()),
                },
                key=f"{run_id}:terminal",
            )
        if not task.cancelled():
            self._resume_if_ready(session_id)

    async def stop(self) -> None:
        tasks = list(self._monitors)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.context.close()

    @DBOS.workflow(name="bud.run", max_recovery_attempts=5)
    async def _workflow(self, run: dict) -> None:
        await self._execute(run)

    async def _step(self, name: str, function, *args):
        if self.durable:
            return await DBOS.run_step_async(
                {"name": name, "retries_allowed": False}, function, *args
            )
        return function(*args)

    async def _execute(self, run: dict) -> None:
        token = self._current.set(run)
        session_token = request_session.set(run["session_id"])
        try:
            if run["config"] != self.fingerprint or run.get("version") != self.version:
                self._finish(
                    run,
                    "run.failed",
                    {
                        "run_id": run["run_id"],
                        "reason": "Code or configuration changed; recovery stopped before tools.",
                    },
                    [],
                )
                return
            await self._step("bud.started", self._started, run)

            conversation = (
                ConversationTypeAdapter.validate_json(run["conversation"])
                if run["conversation"]
                else None
            )
            if conversation is not None and conversation.conversation_id != run["session_id"]:
                raise RecoveryRefused("Conversation does not belong to this session.")
            limits = RUN_LIMITS
            if conversation is not None:
                limits = replace(
                    RUN_LIMITS,
                    request_limit=conversation.usage.requests + RUN_LIMITS.request_limit,
                    tool_calls_limit=conversation.usage.tool_calls + RUN_LIMITS.tool_calls_limit,
                )
            result = await self.agent.run(
                run["text"],
                conversation=conversation,
                message_history=ModelMessagesTypeAdapter.validate_python(run["history"])
                if run["history"]
                else None,
                deferred_tool_results=RESULTS.validate_json(run["results"])
                if run["results"]
                else None,
                run_id=run["run_id"],
                conversation_id=run["session_id"] if conversation is None else None,
                usage_limits=limits,
                event_stream_handler=None if self.durable else self._stream,
            )
            data = {
                "run_id": run["run_id"],
                "messages": json.loads(result.new_messages_json()),
                "conversation": ConversationTypeAdapter.dump_json(result.conversation).decode(),
            }
            requests = (
                result.output.approvals if isinstance(result.output, DeferredToolRequests) else None
            )
            approvals = [
                {
                    "run_id": run["run_id"],
                    "approval_id": uuid5(
                        NAMESPACE_URL, f"bud:{run['run_id']}:{call.tool_call_id}"
                    ).hex,
                    "call_id": call.tool_call_id,
                    "tool": call.tool_name,
                    "args": call.args_as_dict(),
                }
                for call in requests or []
            ]
            await self._step(
                "bud.finished",
                self._finish,
                run,
                "run.waiting" if requests is not None else "run.completed",
                data,
                approvals,
            )
        except asyncio.CancelledError:
            if not self.durable:
                self.hub.publish(
                    run["session_id"],
                    "run.failed",
                    {"run_id": run["run_id"], "reason": "cancelled"},
                )
            raise
        except Exception as exc:
            await self._step(
                "bud.failed",
                self._finish,
                run,
                "run.failed",
                {"run_id": run["run_id"], "reason": error_summary(exc)},
                [],
            )
        finally:
            request_session.reset(session_token)
            self._current.reset(token)

    def _started(self, run: dict) -> None:
        self.hub.publish_once(
            run["session_id"],
            "run.started",
            {
                "run_id": run["run_id"],
                "trigger": run["trigger"],
                "prompt": self.harness.prompt_version,
            },
            key=f"{run['run_id']}:started",
        )

    def _finish(self, run: dict, type: str, data: dict, approvals: list[dict]) -> None:
        entries = [
            ("approval.requested", request, f"{run['run_id']}:approval:{request['call_id']}")
            for request in approvals
        ]
        entries.append((type, data, f"{run['run_id']}:terminal"))
        self.hub.log.append_batch_once(run["session_id"], entries)
        self.hub.wake(run["session_id"])

    def _record_context(self, event: dict) -> None:
        session_id = event["session_id"]
        self.hub.publish_once(
            session_id,
            f"context.{event['stage']}",
            event,
            key=f"context:{event['id']}:{event['stage']}",
        )

    def _record_decision(self, decision: dict) -> None:
        run = self._current.get()
        self.hub.publish_once(
            run["session_id"],
            "action.decided",
            {"run_id": run["run_id"], **decision},
            key=f"{run['run_id']}:decision:{decision['call_id']}",
        )

    async def _stream(self, ctx: RunContext, events: AsyncIterable) -> None:
        run = self._current.get()
        seen: set[str] = set()
        async for event in events:
            data = {"run_id": run["run_id"]}
            request = f"{run['run_id']}:{ctx.run_step}"
            match event:
                case PartStartEvent(index=index, part=part):
                    if request not in seen:
                        self.hub.publish(
                            run["session_id"], "text.reset", {**data, "request": request}
                        )
                        seen.add(request)
                    if isinstance(part, TextPart) and part.content:
                        self.hub.publish(
                            run["session_id"],
                            "text.delta",
                            {**data, "block": f"{request}:{index}", "text": part.content},
                        )
                case PartDeltaEvent(index=index, delta=TextPartDelta(content_delta=text)) if text:
                    self.hub.publish(
                        run["session_id"],
                        "text.delta",
                        {**data, "block": f"{run['run_id']}:{ctx.run_step}:{index}", "text": text},
                    )
                case FunctionToolCallEvent(part=part):
                    self.hub.publish_once(
                        run["session_id"],
                        "tool.call",
                        {
                            **data,
                            "call_id": part.tool_call_id,
                            "tool": part.tool_name,
                            "args": part.args_as_dict(),
                        },
                        key=f"{run['run_id']}:call:{part.tool_call_id}",
                    )
                case FunctionToolResultEvent() as result:
                    self.hub.publish_once(
                        run["session_id"],
                        "tool.result",
                        {
                            **data,
                            "call_id": result.tool_call_id,
                            "result": str(result.part.content),
                        },
                        key=f"{run['run_id']}:result:{result.tool_call_id}",
                    )
