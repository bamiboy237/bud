"""Agent runs with durable inputs, DBOS recovery, and application-owned approvals."""

import asyncio
import json
import logging
from collections.abc import AsyncIterable
from contextvars import Context, ContextVar
from datetime import datetime
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
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from bud.core.events import RUN_END_EVENTS, Event, EventType
from bud.core.hub import Hub
from bud.core.log import StopRequested
from bud.core.projection import (
    JobReport,
    Snapshot,
    decisions,
    message_batch,
    model_history,
    queue_paused,
    reply_text,
    runs,
    snapshot,
    undecided_approvals,
    waiting_batch,
)
from bud.core.runtime import replay_version
from bud.core.tasks import CONTROL_TOOLS, FINISHED, JOB_OUTPUT, TaskFlow, WorkItems
from bud.gateway import Gateway
from bud.gateway.errors import NEXT_STEP, error_summary, failure
from bud.gateway.registry import Tier
from bud.gateway.router import NoModelAvailable
from bud.gateway.tracked import TrackedModel, request_session
from bud.harness.agent import Harness
from bud.harness.context import BackgroundCompaction
from bud.harness.learning import Learner
from bud.harness.recall import Recall, render
from bud.harness.spiral import paused_in
from bud.limits import MAX_MESSAGE_CHARS

RESULTS = TypeAdapter(DeferredToolResults)
logger = logging.getLogger(__name__)
RECENT_LESSONS = 5


class UnknownApproval(Exception):
    pass


class DecisionRejected(Exception):
    """The approval exists but expects the other interaction (answer vs decision)."""

    def __init__(self, approval_id: str, expects_answer: bool) -> None:
        self.approval_id = approval_id
        self.expects_answer = expects_answer
        verb = "an answer" if expects_answer else "an approve/deny decision"
        super().__init__(f"approval {approval_id} expects {verb}")


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
        learner: Learner | None = None,
        recall: Recall | None = None,
    ) -> None:
        self.hub = hub
        self.gateway = gateway
        self.harness = harness
        self.tier: Tier = tier
        self.durable = durable
        self.tasks: dict[str, asyncio.Task] = {}
        self._monitors: set[asyncio.Task] = set()
        self._executing: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bursts: dict[str, asyncio.TimerHandle] = {}
        self._closing = False
        self._current: ContextVar[dict] = ContextVar("bud_run")
        self.session_models: dict[str, str] = {}
        self.work_items = WorkItems(hub) if jobs else None
        self.learner = learner
        self.recall = recall
        self._learning: set[asyncio.Task] = set()
        if learner is not None:
            harness.live_instructions.append(self._recent_lessons)
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
        # A Model instance cannot cross the DBOS step boundary, so durable runners
        # register every buildable registry model up front — ledger-tracked, like
        # the agent's default — and agent.run() receives the registered key, not an
        # instance. Built only for durable runners: building also acquires the
        # cross-process ChatGPT lock, which non-durable runners must not take.
        runtime_models: dict[str, Model] = {}
        if durable:
            for spec in self.gateway.registry.models:
                built = self.gateway.build_model(spec)
                if built is not None:
                    runtime_models[spec.id] = TrackedModel(built, spec, self.gateway.ledger)
        self.runtime_model_ids = frozenset(runtime_models)
        durability = (
            DBOSDurability(
                name="bud",
                models=runtime_models,
                event_stream_handler=self._stream,
                parallel_execution_mode="sequential",
                model_step_config={"retries_allowed": False},
                mcp_step_config={"retries_allowed": False},
            )
            if durable
            else None
        )
        self.context = BackgroundCompaction(record=self._record_context)
        self.agent = harness.agent(
            gateway.model(tier, require_tools=True),
            self._record_decision,
            context=self.context,
            loop_report=self._loop_detected,
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
            # What the owner asked for, kept for the approval-triage model: the message text,
            # or (set in _resume_if_ready) the original message of a resumed run. A job gets
            # the owner's words that started or steered it, never its brief: bud writes the
            # brief, possibly after reading untrusted mail.
            "request": text if item is None else item.owner_request,
            # The owner's clock when this run was queued (the simulated clock in sim mode).
            "clock": self.harness.clock().isoformat(),
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

    def set_model(self, session_id: str, model_id: str | None) -> None:
        if model_id is None:
            self.session_models.pop(session_id, None)
            return
        if not any(spec.id == model_id for spec in self.gateway.registry.models):
            raise ValueError(f"Unknown model: {model_id}.")
        self.session_models[session_id] = model_id

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
            if session_id in self.tasks:
                self.tasks[session_id].cancel()
            executing = self._executing.get(run_id)
            if executing is not None:
                executing.cancel()
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
        batch = waiting_batch(events)
        request = next((r for r in batch if r["approval_id"] == approval_id), None)
        if request is None:
            raise UnknownApproval(approval_id)
        if request.get("tool") == "ask_user":
            # Questions carry information; a decision cannot. Send the answer instead.
            raise DecisionRejected(approval_id, expects_answer=True)
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
        events = self.hub.log.read(session_id)
        request = next(
            (r for r in undecided_approvals(events) if r["approval_id"] == approval_id), None
        )
        if request is not None and request.get("tool") == "ask_user":
            # A question is replaced by its answer, not by a correction.
            raise DecisionRejected(approval_id, expects_answer=True)
        event = self.hub.log.revise_approval(session_id, approval_id, text, message_id, origin)
        self.hub.wake(session_id)
        self._drain(session_id)
        return event is not None

    def answer(
        self, session_id: str, approval_id: str, text: str, origin: dict | None = None
    ) -> bool:
        """Record the owner's answer to an ask_user question and resume the run."""
        if self.work_items is not None:
            target = self.work_items.resolve(session_id, approval_id)
            if target is not None:
                return self.answer(target[0], target[1], text, origin)
        self._validate_text(text)
        events = self.hub.log.read(session_id)
        if approval_id in decisions(events):
            return False
        batch = waiting_batch(events)
        request = next((r for r in batch if r["approval_id"] == approval_id), None)
        if request is None:
            raise UnknownApproval(approval_id)
        if request.get("tool") != "ask_user":
            # Action approvals are approved or denied; answers must not grant them.
            raise DecisionRejected(approval_id, expects_answer=False)
        event = self.hub.publish_once(
            session_id,
            "question.answered",
            {"approval_id": approval_id, "text": text, "origin": origin},
            key=f"{session_id}:answer:{approval_id}",
        )
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
        revised = {
            event.data["approval_id"]
            for event in events
            if event.type == "approval.denied" and event.data.get("revision_message_id")
        }
        answers = {
            event.data["approval_id"]: event.data["text"]
            for event in events
            if event.type == "question.answered"
        }
        results = DeferredToolResults(
            approvals={
                request["call_id"]: ToolApproved()
                if decided[request["approval_id"]]
                else ToolDenied(
                    "The owner sent another message before answering this. If it corrects or "
                    "replaces this request, follow it. If it is about something else, handle it, "
                    "then return to this request: it is still open, so ask again."
                    if request["approval_id"] in revised
                    else _denial(request)
                )
                for request in batch
            },
            metadata={
                request["call_id"]: (
                    {
                        "approval_id": request["approval_id"],
                        "question_answer": answers[request["approval_id"]],
                    }
                    if request["approval_id"] in answers
                    else {
                        "approval_id": request["approval_id"],
                        **_review_of(request.get("review")),
                    }
                )
                for request in batch
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
            (
                event.data.get("skills", {})
                for event in reversed(events)
                if event.type == "run.queued" and event.data["run_id"] == batch[0]["run_id"]
            ),
            run["skills"],
        )
        run["request"] = next(
            (
                event.data.get("request")
                for event in reversed(events)
                if event.type == "run.queued" and event.data["run_id"] == batch[0]["run_id"]
            ),
            None,
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
                {"run_id": run_id, **_failure(error)},
                [],
            )
            self.hub.wake(session_id)
        if not task.cancelled():
            if error is None and not stopped:
                self._maybe_learn(session_id, run_id)
            self._publish_task(session_id)
            self._drain(session_id)

    def _maybe_learn(self, session_id: str, run_id: str) -> None:
        """Reflect on a completed turn in the background; the reply is already out."""
        if (self.learner is None and self.recall is None) or self._closing:
            return
        if self.work_items is not None and self.work_items.for_session(session_id) is not None:
            return  # a job's messages are briefs written by bud, not the owner's words
        events = self.hub.log.read(session_id)
        end = next(
            (
                e
                for e in reversed(events)
                if e.type == "run.completed" and e.data["run_id"] == run_id
            ),
            None,
        )
        if end is None:
            return
        start = max(
            (e.seq for e in events if e.type == "run.completed" and e.seq < end.seq), default=0
        )
        turn = [e for e in events if start < e.seq <= end.seq]
        owner = "\n\n".join(e.data["text"] for e in turn if e.type == "user.message")
        requests = {e.data["approval_id"]: e.data for e in turn if e.type == "approval.requested"}
        signals = [
            f"The owner denied {request['tool']} {json.dumps(request['args'])[:300]}"
            for e in turn
            if e.type == "approval.denied"
            and not e.data.get("revision_message_id")
            and (request := requests.get(e.data["approval_id"])) is not None
        ]
        reply = reply_text(end.data.get("messages", []))
        jobs = []
        if self.learner is not None:
            jobs.append(self._learn(session_id, run_id, owner, reply, signals))
        if self.recall is not None:
            try:
                self.recall.add_turn(session_id, run_id, owner, reply)
            except Exception as exc:
                logger.warning("recall indexing skipped for run %s: %s", run_id, exc)
            jobs.append(self._embed_pending(run_id))
        for job in jobs:
            task = asyncio.create_task(job)
            self._learning.add(task)
            task.add_done_callback(self._learning.discard)

    async def _embed_pending(self, run_id: str) -> None:
        assert self.recall is not None
        try:
            await self.recall.embed_pending()
        except Exception as exc:  # recall is best effort; the turn already succeeded
            logger.warning("recall embedding skipped for run %s: %s", run_id, error_summary(exc))

    async def _recalled(self, run: dict) -> str:
        """Background for a new message: memory lines and earlier turns that bear on it."""
        if self.recall is None or not run.get("text") or run["trigger"] != "message":
            return ""
        if self.work_items is not None and self.work_items.for_session(run["session_id"]):
            return ""  # a job brief is bud's own words
        try:
            await self.recall.index_memory()
            hits = await self.recall.search(run["text"], exclude_session=run["session_id"])
        except Exception as exc:
            logger.warning("recall skipped for run %s: %s", run["run_id"], error_summary(exc))
            return ""
        return render(hits)

    async def _learn(
        self, session_id: str, run_id: str, owner: str, reply: str, signals: list[str]
    ) -> None:
        assert self.learner is not None
        try:
            applied = await self.learner.learn(session_id, owner, reply, signals)
        except Exception as exc:  # learning is best effort; the turn already succeeded
            logger.warning("learning skipped for run %s: %s", run_id, error_summary(exc))
            return
        if applied:
            self.hub.publish_once(
                session_id,
                "memory.learned",
                {"run_id": run_id, "lessons": applied},
                key=f"{run_id}:learned",
            )

    async def settle_learning(self) -> None:
        await asyncio.gather(*self._learning, return_exceptions=True)

    def _recent_lessons(self, ctx: RunContext) -> str:
        session_id = (ctx.metadata or {}).get("bud_session_id")
        if not session_id:
            return ""
        lessons = [
            lesson["fact"]
            for event in self.hub.log.read(session_id)
            if event.type == "memory.learned"
            for lesson in event.data["lessons"]
        ][-RECENT_LESSONS:]
        if not lessons:
            return ""
        listed = "\n".join(f"- {fact}" for fact in lessons)
        return (
            "\nbud saved these to memory in this conversation and told the owner:\n"
            f"{listed}\nIf the owner says to forget or undo one, remove that MEMORY.md line "
            "with write_memory (old_text) and confirm."
        )

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
        await self.settle_learning()
        await self.context.close()
        if self.harness.reviewer is not None:
            await self.harness.reviewer.close()

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
                        "kind": "recovery",
                        "next": NEXT_STEP["recovery"],
                    },
                    [],
                )
                return
            await self._step("bud.started", self._started, run)

            conversation, history = self._history(run)
            if conversation is not None and conversation.conversation_id != run["session_id"]:
                raise RecoveryRefused("Conversation does not belong to this session.")
            model_override = self.session_models.get(run["session_id"])
            if (
                model_override is not None
                and self.durable
                and model_override not in self.runtime_model_ids
            ):
                # Fail fast with bud's own error instead of pydantic-ai's
                # infer_model fallback, which would use a different credential path.
                raise NoModelAvailable(
                    f"No configured model for {model_override}. Check its API key in .env "
                    "or `bud login chatgpt`, then pick again."
                )
            # Appended to the newest message, after the cached prefix, so prompt caching holds.
            recalled = (
                await DBOS.run_step_async(
                    {"name": "bud.recall", "retries_allowed": False}, self._recalled, run
                )
                if self.durable
                else await self._recalled(run)
            )
            # A conversation picked up on a later day must not reuse "today"/"tomorrow".
            history_events = self.hub.log.read(run["session_id"])
            gap = time_gap_note(history_events, run)
            still_open = superseded_note(history_events, run)
            result = await self.agent.run(
                (run["text"] + recalled + gap + still_open) if run["text"] else run["text"],
                model=(
                    # Durable runs resolve pre-registered keys; local runs take the instance.
                    model_override
                    if self.durable
                    else self.gateway.model(
                        self.tier,
                        session_id=run["session_id"],
                        require_tools=True,
                        selected=model_override,
                    )
                    if model_override
                    else None
                ),
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
                    "bud_request": run.get("request"),
                    "bud_revision_pending": bool(run.get("quiet")),
                    "bud_inline_approval": run.get("task_id") is None
                    and run["trigger"] == "approval",
                },
                # No step cap: SpiralGuard ends runaway loops. Explicit, because pydantic-ai
                # otherwise applies request_limit=50 to the whole conversation's running
                # total, which fails every message once a long-lived thread passes 50.
                usage_limits=UsageLimits(request_limit=None),
                # A job ends only by filing a typed report; chat ends in text.
                output_type=JOB_OUTPUT if run.get("task_id") is not None else None,
                event_stream_handler=None if self.durable else self._stream,
            )
            data = {
                "run_id": run["run_id"],
                "messages": json.loads(result.new_messages_json()),
                "conversation": ConversationTypeAdapter.dump_json(result.conversation).decode(),
            }
            if isinstance(result.output, JobReport):
                data["report"] = result.output.model_dump()
            deferred = result.output if isinstance(result.output, DeferredToolRequests) else None
            requests = deferred.approvals if deferred is not None else None
            approvals = [
                {
                    "run_id": run["run_id"],
                    "approval_id": uuid5(
                        NAMESPACE_URL, f"bud:{run['run_id']}:{call.tool_call_id}"
                    ).hex,
                    "call_id": call.tool_call_id,
                    "tool": call.tool_name,
                    "args": call.args_as_dict(),
                    # Why the reviewer held this call; absent when a rule asked, not it.
                    **_review_of(deferred.metadata.get(call.tool_call_id) if deferred else None),
                }
                for call in requests or []
            ]
            if run.get("task_id") is not None and paused_in(result.new_messages()) is not None:
                # The job wrote its handoff and stopped; it stays paused until the owner replies.
                self.hub.log.pause_queue(run["session_id"], "looping")
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
                {"run_id": run["run_id"], **_failure(exc)},
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

    def _loop_detected(self, detection: dict) -> None:
        """A run is going in circles: tell the clients. This is a pause, not a failure."""
        run = self._current.get()
        self.hub.publish_once(
            run["session_id"],
            "run.loop_detected",
            {**detection, "run_id": run["run_id"]},
            key=f"{run['run_id']}:loop_detected",
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


def _failure(error: BaseException) -> dict[str, str]:
    if isinstance(error, RecoveryRefused):
        return {"reason": str(error), "kind": "recovery", "next": NEXT_STEP["recovery"]}
    return failure(error)


def superseded_note(events: list[Event], run: dict) -> str:
    """What this message interrupted: proposals or questions it superseded before the owner
    answered. A burst of messages must not silently drop the earlier request."""
    if run.get("trigger") != "message":
        return ""
    messages = set(run.get("message_ids") or [run.get("message_id")])
    requested = {e.data["approval_id"]: e.data for e in events if e.type == "approval.requested"}
    asks = {
        e.data["run_id"]: e.data.get("request") or e.data.get("text")
        for e in events
        if e.type == "run.queued"
    }
    lines = []
    for event in events:
        if event.type != "approval.denied" or event.data.get("revision_message_id") not in messages:
            continue
        pending = requested.get(event.data["approval_id"])
        if pending is None:
            continue
        what = (
            f"your question: {str(pending['args'].get('question', ''))[:200]}"
            if pending["tool"] == "ask_user"
            else f"approval to run {pending['tool']} {json.dumps(pending['args'])[:200]}"
        )
        asked = asks.get(pending["run_id"])
        lines.append(f"- The owner had asked: {str(asked)[:300]!r}. You were waiting on {what}.")
    if not lines:
        return ""
    return (
        "\n\n<still_open>This message arrived before the owner answered:\n"
        + "\n".join(lines)
        + "\nIf this message replaces or cancels that, follow it. Otherwise handle this message "
        "and then return to the earlier request in the same reply.</still_open>"
    )


def time_gap_note(events: list[Event], run: dict) -> str:
    """A note when this message arrives on a later date than the conversation's last one.

    Built only from the log and the run's own recorded clock, so a replay agrees.
    """
    if run.get("trigger") != "message" or not run.get("text") or not run.get("clock"):
        return ""
    earlier = next(
        (
            event.data["clock"]
            for event in reversed(events)
            if event.type == "run.queued"
            and event.data.get("run_id") != run["run_id"]
            and event.data.get("text")
            and event.data.get("clock")
        ),
        None,
    )
    if earlier is None:
        return ""
    now = datetime.fromisoformat(run["clock"])
    then = datetime.fromisoformat(earlier)
    if now.date() <= then.date():
        return ""
    return (
        f"\n\n<time>It is now {now:%A, %d %B %Y}. This conversation's previous message was "
        f"on {then:%A, %d %B %Y}. Answers given earlier about today, tomorrow, or this week "
        "may be out of date: check again before relying on them.</time>"
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


def _review_of(review: object) -> dict[str, dict]:
    """The reviewer's record of a held call, as an extra key on an approval or its metadata."""
    return {"review": review} if isinstance(review, dict) and review else {}


def _denial(request: dict) -> str:
    """What the agent hears when the owner denies a call. A held call carries its reason."""
    review = request.get("review")
    reason = review.get("reason") if isinstance(review, dict) else None
    if not isinstance(reason, str) or not reason:
        return (
            "The owner declined this action; nothing failed. Say plainly that you won't do it, "
            "and don't retry it unless they ask."
        )
    return (
        f"The owner declined this action; nothing failed. Reviewer's concern: {reason} "
        "If a safer way to do what "
        "the user asked exists, use it. Otherwise tell the user what you wanted to do and why."
    )
