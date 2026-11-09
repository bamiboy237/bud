"""Agent runs with durable inputs, DBOS recovery, and application-owned approvals."""

import asyncio
import json
from collections.abc import AsyncIterable
from contextvars import Context, ContextVar
from dataclasses import replace
from hashlib import sha256
from typing import cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from dbos import DBOS, DBOSConfiguredInstance, SetWorkflowID
from pydantic import TypeAdapter
from pydantic_ai import (
    Conversation,
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
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ToolReturnPart,
)
from pydantic_ai.usage import UsageLimits

from bud.core.events import RUN_END_EVENTS, Event, EventType
from bud.core.hub import Hub
from bud.core.log import StopRequested
from bud.core.projection import (
    Snapshot,
    decisions,
    message_batch,
    model_history,
    queue_paused,
    runs,
    snapshot,
    undecided_approvals,
    waiting_batch,
)
from bud.core.runtime import replay_version
from bud.core.tasks import CONTROL_TOOLS, FINISHED, TaskFlow, WorkItems
from bud.gateway import Gateway
from bud.gateway.errors import error_summary
from bud.gateway.registry import Tier
from bud.gateway.tracked import request_session
from bud.harness.agent import Harness
from bud.harness.context import BackgroundCompaction
from bud.limits import (
    MAX_MESSAGE_CHARS,
    MAX_MODEL_REQUESTS_PER_RUN,
    MAX_TOOL_CALLS_PER_RUN,
)

RESULTS = TypeAdapter(DeferredToolResults)


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
        jobs: bool = False,
    ) -> None:
        self.hub = hub
        self.gateway = gateway
        self.harness = harness
        self.tier = tier
        self.durable = durable
        self.tasks: dict[str, asyncio.Task] = {}
        self._monitors: set[asyncio.Task] = set()
        self._executing: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bursts: dict[str, asyncio.TimerHandle] = {}
        self._closing = False
        self._current: ContextVar[dict] = ContextVar("bud_run")
        self.work_items = WorkItems(hub) if jobs else None
        if jobs:
            harness.policy.tools.update(dict.fromkeys(CONTROL_TOOLS, "allow"))
        self.version = replay_version()
        self.fingerprint = sha256(
            (
                harness.prompt_version
                + gateway.registry.model_dump_json()
                + json.dumps(harness.policy.tools, sort_keys=True)
                + str(gateway.selected_model)
                + str(gateway.fallback)
                + str(jobs)
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
            gateway.model(tier, require_tools=True),
            self._record_decision,
            context=self.context,
            durability=durability,
            capabilities=(
                TaskFlow(
                    store=self.work_items,
                    policy=harness.policy,
                    kick=self._kick,
                    stop=self._stop_task,
                ),
            )
            if self.work_items is not None
            else (),
        )

    def snapshot(self, session_id: str) -> Snapshot:
        state = snapshot(session_id, self.hub.log.read(session_id))
        if self.work_items is not None and self.work_items.for_session(session_id) is None:
            state.tasks = self.work_items.views(session_id)
            state.pending_approvals.extend(
                approval for task in state.tasks for approval in task.pending_approvals
            )
        return state

    def _kick(self, session_id: str) -> None:
        self.hub.wake(session_id)
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._drain, session_id, context=Context())

    async def _stop_task(self, session_id: str) -> dict:
        if self._loop is asyncio.get_running_loop():
            return await self.stop_session(session_id)
        assert self._loop is not None
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(self.stop_session(session_id), self._loop)
        )

    def _publish_task(self, session_id: str) -> None:
        if self.work_items is not None:
            self.work_items.publish(session_id)

    async def recover(self) -> None:
        self._loop = asyncio.get_running_loop()
        pending = self.hub.log.pending_runs()
        known = {run["run_id"] for run in pending}
        # Old runs have no immutable inputs or journal and cannot be replayed safely.
        for session_id, run_id in self.hub.log.unfinished_runs():
            if run_id not in known:
                self.hub.log.finish_run(
                    session_id,
                    run_id,
                    "run.failed",
                    {
                        "run_id": run_id,
                        "reason": "interrupted before durable execution was enabled",
                    },
                    [],
                )
        for run in pending:
            if self.hub.log.stop_requested(run["run_id"]):
                if self.durable:
                    await DBOS.cancel_workflow_async(run["run_id"])
                if run["run_id"] not in self._executing:
                    self.hub.log.acknowledge_stop(run["session_id"], run["run_id"])
                continue
            self.hub.publish(run["session_id"], "run.recovered", {"run_id": run["run_id"]})
            self._launch(run, recovering=True)
        for session_id in set(
            self.hub.log.sessions_with_decisions() + self.hub.log.queue_sessions()
        ):
            self._drain(session_id)
        if self.work_items is not None:
            for item in self.work_items.items():
                self.work_items.publish(item.session_id)

    def _inputs(
        self,
        session_id: str,
        text: str | None,
        results: DeferredToolResults | None = None,
        *,
        origin: dict | None = None,
    ) -> dict:
        events = self.hub.log.read(session_id)
        last = next((event for event in reversed(events) if event.type in RUN_END_EVENTS), None)
        saved = last is not None and bool(
            last.data.get("conversation_saved") or last.data.get("conversation")
        )
        item = self.work_items.for_session(session_id) if self.work_items is not None else None
        skills = self.harness.skills.snapshot()
        if item is not None:
            skills = (
                item.skills
                if item.skills is not None
                else next(
                    (
                        event.data["skills"]
                        for event in events
                        if event.type == "run.queued" and "skills" in event.data
                    ),
                    skills,
                )
            )
        # Inputs name the saved snapshot or a log position instead of copying history, so
        # each queued run stays small. Both references are immutable once written.
        return {
            "session_id": session_id,
            "run_id": uuid4().hex,
            "text": text,
            "trigger": "message" if results is None else "approval",
            "origin": origin,
            "task_id": item.task_id if item is not None else None,
            "conversation_run": last.data["run_id"] if saved and last is not None else None,
            "history_seq": None if saved else (events[-1].seq if events else 0),
            "results": RESULTS.dump_json(results).decode() if results is not None else None,
            "skills": skills,
            "config": self.fingerprint,
            "version": self.version,
        }

    def start(
        self, session_id: str, text: str, message_id: str, origin: dict | None = None
    ) -> Event | None:
        self._loop = asyncio.get_running_loop()
        self._validate_text(text)
        event = self.hub.log.accept_message(session_id, text, message_id, origin)
        if event is not None:
            self.hub.wake(session_id)
        if (
            event is not None
            and origin
            and origin.get("channel") == "imessage"
            and not self._busy(session_id)
        ):
            if session_id not in self._bursts:
                # A fixed window, not an indefinitely reset timer. Accepted text
                # is already durable; a restart can drain it without this timer.
                self._bursts[session_id] = self._loop.call_later(1, self._flush_burst, session_id)
        else:
            self._drain(session_id)
        return event

    def _flush_burst(self, session_id: str) -> None:
        self._bursts.pop(session_id, None)
        self._drain(session_id)

    @staticmethod
    def _validate_text(text: str) -> None:
        if not text.strip() or len(text) > MAX_MESSAGE_CHARS:
            raise ValueError(
                f"Message must contain text and be at most {MAX_MESSAGE_CHARS:,} characters"
            )

    def edit_message(self, session_id: str, message_id: str, text: str) -> None:
        self._validate_text(text)
        self.hub.log.edit_message(session_id, message_id, text)
        self.hub.wake(session_id)

    def cancel_message(self, session_id: str, message_id: str) -> bool:
        changed = self.hub.log.cancel_message(session_id, message_id)
        self.hub.wake(session_id)
        return changed

    def pause_queue(self, session_id: str, paused: bool) -> bool:
        changed = self.hub.log.set_queue_paused(session_id, paused)
        self.hub.wake(session_id)
        if not paused:
            self._drain(session_id)
        return changed

    def _drain(self, session_id: str) -> None:
        if self._closing or session_id in self._bursts or self._busy(session_id):
            return
        events = self.hub.log.read(session_id)
        if waiting_batch(events):
            if self.hub.log.supersede_updates(session_id):
                self.hub.wake(session_id)
            self._resume_if_ready(session_id)
            return
        pending = message_batch(events)
        if queue_paused(events) or not pending:
            return
        run = self._inputs(
            session_id,
            "\n\n".join(item.text for item in pending),
            origin=pending[0].origin.model_dump() if pending[0].origin else None,
        )
        run["message_id"] = pending[0].message_id
        run["message_ids"] = [item.message_id for item in pending]
        if self.hub.log.dispatch_message(session_id, pending[0].message_id, run):
            self.hub.wake(session_id)
            self._launch(run)

    async def stop_session(self, session_id: str) -> dict:
        timer = self._bursts.pop(session_id, None)
        if timer is not None:
            timer.cancel()
        request = self.hub.log.request_stop(session_id)
        self.hub.wake(session_id)
        child_stopped = False
        if self.work_items is not None:
            for item in self.work_items.items(session_id):
                if self.work_items.view(item).status not in FINISHED:
                    result = await self.stop_session(item.session_id)
                    child_stopped |= result["stop_requested"]
        await self.context.discard(session_id)
        if request is not None:
            run_id = request["run_id"]
            if self.durable:
                await DBOS.cancel_workflow_async(run_id)
            elif session_id in self.tasks:
                self.tasks[session_id].cancel()
            if run_id not in self._executing:
                self.hub.log.acknowledge_stop(session_id, run_id)
            self.hub.wake(session_id)
        self._publish_task(session_id)
        return {"stop_requested": request is not None or child_stopped, "queue_paused": True}

    def decide(
        self, session_id: str, approval_id: str, approved: bool, origin: dict | None = None
    ) -> bool:
        if self.work_items is not None:
            target = self.work_items.resolve(session_id, approval_id)
            if target is not None:
                return self.decide(target[0], target[1], approved, origin)
        events = self.hub.log.read(session_id)
        if approval_id in decisions(events):
            return False
        if not any(request["approval_id"] == approval_id for request in waiting_batch(events)):
            raise UnknownApproval(approval_id)
        event_type = "approval.granted" if approved else "approval.denied"
        event = self.hub.publish_once(
            session_id, event_type, {"approval_id": approval_id, "origin": origin}
        )
        if event is None:
            return False
        self._resume_if_ready(session_id)
        return True

    def revise(
        self, session_id: str, approval_id: str, text: str, message_id: str, origin: dict
    ) -> bool:
        if self.work_items is not None:
            target = self.work_items.resolve(session_id, approval_id)
            if target is not None:
                return self.revise(target[0], target[1], text, message_id, origin)
        self._validate_text(text)
        event = self.hub.log.revise_approval(session_id, approval_id, text, message_id, origin)
        self.hub.wake(session_id)
        self._drain(session_id)
        return event is not None

    def _resume_if_ready(self, session_id: str) -> None:
        events = self.hub.log.read(session_id)
        batch = waiting_batch(events)
        if not batch or undecided_approvals(events) or self._busy(session_id):
            return
        decided = decisions(events)
        revised = {
            event.data["approval_id"]
            for event in events
            if event.type == "approval.denied" and event.data.get("revision_message_id")
        }
        results = DeferredToolResults(
            approvals={
                request["call_id"]: ToolApproved()
                if decided[request["approval_id"]]
                else ToolDenied(
                    "The user sent new input. Stop this proposal; the updated request will follow."
                    if request["approval_id"] in revised
                    else "The user denied this action."
                )
                for request in batch
            },
            metadata={
                request["call_id"]: {"approval_id": request["approval_id"]} for request in batch
            },
        )
        ids = {request["approval_id"] for request in batch}
        origin = next(
            (
                event.data.get("origin")
                for event in reversed(events)
                if event.type in ("approval.granted", "approval.denied")
                and event.data["approval_id"] in ids
                and event.data.get("origin") is not None
            ),
            None,
        )
        if origin is None:
            origin = next(
                (
                    event.data.get("origin")
                    for event in reversed(events)
                    if event.type == "run.queued" and event.data["run_id"] == batch[0]["run_id"]
                ),
                None,
            )
        run = self._inputs(session_id, None, results, origin=origin)
        run["skills"] = next(
            event.data.get("skills", {})
            for event in reversed(events)
            if event.type == "run.queued" and event.data["run_id"] == batch[0]["run_id"]
        )
        run["quiet"] = any(request["approval_id"] in revised for request in batch) and not any(
            decided[request["approval_id"]] for request in batch
        )
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
        if self.hub.log.stop_requested(run["run_id"]):
            raise StopRequested("Stopped before workflow launch")
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
        states = runs(self.hub.log.read(session_id))
        return bool(states and states[-1].status in ("queued", "running", "stopping"))

    def _finished(self, session_id: str, run_id: str, task: asyncio.Task) -> None:
        self._monitors.discard(task)
        if self.tasks.get(session_id) is task:
            self.tasks.pop(session_id)
        error = None if task.cancelled() else task.exception()
        stopped = self.hub.log.stop_requested(run_id)
        if stopped:
            if run_id not in self._executing:
                self.hub.log.acknowledge_stop(session_id, run_id)
                self.hub.wake(session_id)
        elif error is not None:
            self.hub.log.finish_run(
                session_id,
                run_id,
                "run.failed",
                {
                    "run_id": run_id,
                    "reason": str(error)
                    if isinstance(error, RecoveryRefused)
                    else error_summary(error),
                },
                [],
            )
            self.hub.wake(session_id)
        if not task.cancelled():
            self._publish_task(session_id)
            self._drain(session_id)

    async def stop(self) -> None:
        self._closing = True
        for timer in self._bursts.values():
            timer.cancel()
        self._bursts.clear()
        if not self.durable:
            for task in self._monitors:
                task.cancel()
        # Keep ownership until launch submissions and actual executions have exited.
        await asyncio.gather(*self._monitors, return_exceptions=True)
        await asyncio.gather(*self._executing.values(), return_exceptions=True)
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
        task = asyncio.current_task()
        assert task is not None
        self._executing[run["run_id"]] = task
        try:
            if self.hub.log.stop_requested(run["run_id"]):
                return
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

            conversation, history = self._history(run)
            if conversation is not None and conversation.conversation_id != run["session_id"]:
                raise RecoveryRefused("Conversation does not belong to this session.")
            limits = UsageLimits(
                request_limit=MAX_MODEL_REQUESTS_PER_RUN,
                tool_calls_limit=MAX_TOOL_CALLS_PER_RUN,
            )
            if conversation is not None:
                limits = replace(
                    limits,
                    request_limit=conversation.usage.requests + MAX_MODEL_REQUESTS_PER_RUN,
                    tool_calls_limit=conversation.usage.tool_calls + MAX_TOOL_CALLS_PER_RUN,
                )
            result = await self.agent.run(
                run["text"],
                conversation=conversation,
                message_history=history or None,
                deferred_tool_results=RESULTS.validate_json(run["results"])
                if run["results"]
                else None,
                run_id=run["run_id"],
                conversation_id=run["session_id"] if conversation is None else None,
                metadata={
                    "bud_skills": run.get("skills", {}),
                    "bud_session_id": run["session_id"],
                    "bud_task_id": run.get("task_id"),
                    "bud_origin": run.get("origin"),
                    "bud_revision_pending": bool(run.get("quiet")),
                    "bud_inline_approval": run.get("task_id") is None
                    and run["trigger"] == "approval",
                },
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
            if not self.durable and not self.hub.log.stop_requested(run["run_id"]):
                self._finish(
                    run,
                    "run.failed",
                    {"run_id": run["run_id"], "reason": "cancelled"},
                    [],
                )
            raise
        except Exception as exc:
            if self.hub.log.stop_requested(run["run_id"]):
                return
            await self._step(
                "bud.failed",
                self._finish,
                run,
                "run.failed",
                {"run_id": run["run_id"], "reason": error_summary(exc)},
                [],
            )
        finally:
            if self.hub.log.stop_requested(run["run_id"]):
                self.hub.log.acknowledge_stop(run["session_id"], run["run_id"])
                self.hub.wake(run["session_id"])
                self._publish_task(run["session_id"])
                if self._loop is not None and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._drain, run["session_id"])
            self._executing.pop(run["run_id"], None)
            request_session.reset(session_token)
            self._current.reset(token)

    def _history(self, run: dict) -> tuple[Conversation | None, list[ModelMessage]]:
        if run.get("conversation_run"):
            saved = self.hub.log.conversation(run["session_id"], run["conversation_run"])
            if saved is None:
                raise RecoveryRefused("The saved conversation for this run is unavailable.")
            return ConversationTypeAdapter.validate_json(saved), []
        if run.get("history_seq") is None:
            return None, []
        events = self.hub.log.read(run["session_id"])
        return None, model_history([event for event in events if event.seq <= run["history_seq"]])

    def _started(self, run: dict) -> None:
        self.hub.publish_once(
            run["session_id"],
            "run.started",
            {
                "run_id": run["run_id"],
                "trigger": run["trigger"],
                "prompt": self.harness.prompt_version,
                "skills": run.get("skills", {}),
            },
            key=f"{run['run_id']}:started",
        )

    def _finish(self, run: dict, type: EventType, data: dict, approvals: list[dict]) -> None:
        self.hub.log.finish_run(run["session_id"], run["run_id"], type, data, approvals)
        self.hub.wake(run["session_id"])
        self._publish_task(run["session_id"])

    def _record_context(self, event: dict) -> None:
        session_id = event["session_id"]
        self.hub.publish_once(
            session_id,
            cast(EventType, f"context.{event['stage']}"),
            event,
            key=f"context:{event['id']}:{event['stage']}",
        )

    def _record_decision(self, decision: dict) -> None:
        run = self._current.get()
        self.hub.log.record_action(run["session_id"], run["run_id"], decision)
        self.hub.wake(run["session_id"])

    async def _stream(self, ctx: RunContext, events: AsyncIterable) -> None:
        run = self._current.get()
        seen: set[str] = set()
        async for event in events:
            data = {"run_id": run["run_id"]}
            request = f"{run['run_id']}:{ctx.run_step}"
            match event:
                case PartStartEvent(index=index, part=part):
                    # Streamed text is live-only; run.completed stores the finished reply.
                    if request not in seen:
                        self.hub.broadcast(
                            run["session_id"], "text.reset", {**data, "request": request}
                        )
                        seen.add(request)
                    if isinstance(part, TextPart) and part.content:
                        self.hub.broadcast(
                            run["session_id"],
                            "text.delta",
                            {**data, "block": f"{request}:{index}", "text": part.content},
                        )
                case PartDeltaEvent(index=index, delta=TextPartDelta(content_delta=text)) if text:
                    self.hub.broadcast(
                        run["session_id"],
                        "text.delta",
                        {**data, "block": f"{request}:{index}", "text": text},
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
                    part = result.part
                    outcome = part.outcome if isinstance(part, ToolReturnPart) else "retried"
                    artifact = _artifact(part) if outcome == "success" else None
                    self.hub.publish_once(
                        run["session_id"],
                        "tool.result",
                        {
                            **data,
                            "call_id": result.tool_call_id,
                            "tool": part.tool_name,
                            "outcome": outcome,
                            "result": str(part.content),
                            **({"artifact": artifact} if artifact else {}),
                        },
                        key=f"{run['run_id']}:result:{result.tool_call_id}",
                    )


def _artifact(part: ToolReturnPart | RetryPromptPart) -> dict[str, str] | None:
    """A tool marks a saved deliverable in its return metadata; the model never sees it."""
    metadata = getattr(part, "metadata", None)
    artifact = metadata.get("artifact") if isinstance(metadata, dict) else None
    if not isinstance(artifact, dict):
        return None
    fields: dict[str, str] = {}
    for key in ("path", "revision", "operation"):
        value = artifact.get(key)
        if not isinstance(value, str):
            return None
        fields[key] = value
    return fields
