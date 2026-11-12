"""Jobs reuse ordinary sessions; the feed receives their approvals and results."""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Annotated
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, Field
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from bud.core.events import EventType
from bud.core.hub import Hub
from bud.core.projection import TaskView, queue_paused, queued_messages, reply_text, runs, snapshot
from bud.harness.actions import Policy
from bud.limits import MAX_MESSAGE_CHARS, MAX_OPEN_TASKS

CONTROL_TOOLS = {"start_task", "update_task", "inspect_task", "stop_task"}
FINISHED = {"completed", "failed", "cancelled"}


class TaskBrief(BaseModel):
    goal: str = Field(
        min_length=1,
        max_length=20_000,
        description="The person's requested outcome, exact content and constraints.",
    )
    inputs: list[Annotated[str, Field(max_length=2000)]] = Field(
        max_length=20,
        description="Relevant source IDs, file paths and supplied facts.",
    )
    expected_result: str = Field(
        min_length=1,
        max_length=4000,
        description="The requested answer or deliverable.",
    )

    def text(self) -> str:
        return (
            f"Goal: {self.goal}\nInputs:\n"
            + "\n".join(f"- {value}" for value in self.inputs)
            + f"\nExpected result: {self.expected_result}"
        )


class WorkItem(BaseModel):
    task_id: int
    feed_id: str
    session_id: str
    title: str
    brief: TaskBrief
    origin: dict | None
    skills: dict[str, str] | None


def letter(number: int) -> str:
    result = ""
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


class WorkItems:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.log = hub.log
        with self.log._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feed_id TEXT NOT NULL,
                    session_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    brief TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    source_id TEXT NOT NULL UNIQUE,
                    skills TEXT
                );
                CREATE INDEX IF NOT EXISTS tasks_by_feed ON tasks(feed_id);
            """)
            if "skills" not in {row[1] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN skills TEXT")

    @staticmethod
    def _item(row) -> WorkItem:
        return WorkItem(
            task_id=row[0],
            feed_id=row[1],
            session_id=row[2],
            title=row[3],
            brief=TaskBrief.model_validate_json(row[4]),
            origin=json.loads(row[5]),
            skills=json.loads(row[7]) if row[7] is not None else None,
        )

    def items(self, feed_id: str | None = None) -> list[WorkItem]:
        with self.log._connect() as db:
            rows = db.execute(
                "SELECT * FROM tasks"
                + (" WHERE feed_id=?" if feed_id else "")
                + " ORDER BY task_id",
                (feed_id,) if feed_id else (),
            ).fetchall()
        return [self._item(row) for row in rows]

    def for_session(self, session_id: str) -> WorkItem | None:
        with self.log._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE session_id=?", (session_id,)).fetchone()
        return self._item(row) if row else None

    def get(self, feed_id: str, task_id: int) -> WorkItem:
        with self.log._connect() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE feed_id=? AND task_id=?", (feed_id, task_id)
            ).fetchone()
        if row is None:
            raise ModelRetry("That task does not belong to this conversation.")
        return self._item(row)

    def _open_count(self, db) -> int:
        count = 0
        for (session,) in db.execute("SELECT session_id FROM tasks"):
            events = self.log._read(db, session)
            states = runs(events)
            if (
                not states
                or states[-1].status not in FINISHED
                or (queued_messages(events) and not queue_paused(events))
            ):
                count += 1
        return count

    def create(
        self,
        feed_id: str,
        title: str,
        brief: TaskBrief,
        source_id: str,
        origin: dict | None,
        source_run: str | None,
        skills: dict[str, str] | None = None,
    ) -> WorkItem:
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM tasks WHERE source_id=?", (source_id,)).fetchone()
            if existing:
                item = self._item(existing)
                if (item.feed_id, item.title, item.brief, item.origin, item.skills) != (
                    feed_id,
                    title,
                    brief,
                    origin,
                    skills,
                ):
                    raise ModelRetry("This task request already has different inputs.")
                return item
            if source_run is not None and self.log._stopped(db, source_run):
                raise ModelRetry("The conversation was stopped.")
            if (
                not self.log.exists(feed_id)
                or db.execute("SELECT 1 FROM tasks WHERE session_id=?", (feed_id,)).fetchone()
            ):
                raise ModelRetry("Start a task from a conversation.")
            if self._open_count(db) >= MAX_OPEN_TASKS:
                raise ModelRetry(f"There are already {MAX_OPEN_TASKS} open jobs.")
            session_id = uuid4().hex
            row = db.execute(
                "INSERT INTO tasks(feed_id,session_id,title,brief,origin,source_id,skills) "
                "VALUES(?,?,?,?,?,?,?) RETURNING task_id",
                (
                    feed_id,
                    session_id,
                    title,
                    brief.model_dump_json(),
                    json.dumps(origin),
                    source_id,
                    json.dumps(skills) if skills is not None else None,
                ),
            ).fetchone()
            task_id = row[0]
            self.log._insert(db, session_id, "session.created", {})
            self.log._accept_message(db, session_id, brief.text(), source_id, origin)
            self.log._insert(
                db,
                feed_id,
                "task.created",
                {"task_id": task_id, "title": title, "origin": origin},
            )
        self.hub.wake(feed_id)
        return self.get(feed_id, task_id)

    def update(
        self,
        item: WorkItem,
        text: str,
        source_id: str,
        origin: dict | None,
        source_run: str | None,
    ) -> None:
        if not text.strip() or len(text) > MAX_MESSAGE_CHARS:
            raise ModelRetry("Supply a task instruction within the message limit.")
        with self.log._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if source_run is not None and self.log._stopped(db, source_run):
                raise ModelRetry("The conversation was stopped.")
            event = self.log._accept_message(db, item.session_id, text, source_id, origin)
            if event is not None:
                self.log._supersede(db, item.session_id, source_id, origin)
                if queue_paused(self.log._read(db, item.session_id)):
                    self.log._insert(db, item.session_id, "queue.resumed", {})
                if self._open_count(db) > MAX_OPEN_TASKS:
                    raise ModelRetry(f"There are already {MAX_OPEN_TASKS} open jobs.")
        self.hub.wake(item.session_id)

    def references(self, item: WorkItem) -> dict[str, str]:
        requested = [
            event for event in self.log.read(item.session_id) if event.type == "approval.requested"
        ]
        return {
            event.data["approval_id"]: f"{item.task_id}-{letter(index)}"
            for index, event in enumerate(requested, 1)
        }

    def view(self, item: WorkItem) -> TaskView:
        state = snapshot(item.session_id, self.log.read(item.session_id))
        references = self.references(item)
        status = state.recent_runs[-1].status if state.recent_runs else "queued"
        if state.queued_messages and status in FINISHED:
            status = "paused" if state.queue_paused else "queued"
        if state.queue_paused and status == "queued":
            status = "paused"
        return TaskView(
            task_id=item.task_id,
            session_id=item.session_id,
            title=item.title,
            status=status,
            pending_approvals=[
                approval.model_copy(
                    update={"task_id": item.task_id, "reference": references[approval.approval_id]}
                )
                for approval in state.pending_approvals
            ],
            artifacts=[artifact for run in state.recent_runs for artifact in run.artifacts],
            result=state.transcript[-1].text
            if status == "completed"
            and state.transcript
            and state.transcript[-1].role == "assistant"
            else None,
        )

    def views(self, feed_id: str) -> list[TaskView]:
        return [self.view(item) for item in self.items(feed_id)]

    def resolve(self, feed_id: str, reference: str) -> tuple[str, str] | None:
        match = re.fullmatch(r"([1-9][0-9]*)-([A-Z]+)", reference.upper())
        candidates = self.items(feed_id)
        if match:
            candidates = [item for item in candidates if item.task_id == int(match[1])]
        for item in candidates:
            for approval_id, label in self.references(item).items():
                if reference == approval_id or reference.upper() == label:
                    return item.session_id, approval_id
        return None

    def publish(self, session_id: str) -> None:
        item = self.for_session(session_id)
        if item is None:
            return
        state = self.view(item)
        for approval in state.pending_approvals:
            self.hub.publish_once(
                item.feed_id,
                "task.approval",
                {**approval.model_dump(), "title": item.title, "origin": item.origin},
                key=f"task:{item.task_id}:approval:{approval.approval_id}",
            )
        events = self.log.read(session_id)
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.type in {"run.completed", "run.failed", "run.cancelled"}
            ),
            None,
        )
        if (
            terminal is None
            or terminal.data.get("quiet")
            or state.status not in FINISHED | {"needs_review"}
        ):
            return
        kind = terminal.type.removeprefix("run.")
        text = (
            reply_text(terminal.data.get("messages", []))
            if kind == "completed"
            else terminal.data.get("reason", kind)
        )
        notices: dict[str, EventType] = {
            "completed": "task.completed",
            "failed": "task.failed",
            "cancelled": "task.cancelled",
        }
        self.hub.publish_once(
            item.feed_id,
            notices[kind],
            {"task_id": item.task_id, "title": item.title, "text": text, "origin": item.origin},
            key=f"task:{item.task_id}:terminal:{terminal.id}",
        )


@dataclass(kw_only=True)
class TaskFlow(AbstractCapability[None]):
    store: WorkItems
    policy: Policy
    kick: Callable[[str], None]
    stop: Callable[[str], Awaitable[dict]]
    id: str | None = "bud_feed_jobs"

    @staticmethod
    def _worker(ctx: RunContext[None]) -> bool:
        metadata = ctx.metadata or {}
        return metadata.get("bud_task_id") is not None or bool(metadata.get("bud_inline_approval"))

    def get_instructions(self):
        def instructions(ctx: RunContext[None]) -> str:
            if self._worker(ctx):
                return (
                    "Complete the supplied job brief using tools. Submit requested changes "
                    "through their tools; the system collects approval. "
                    "Return the confirmed result."
                )
            return (
                "Answer quick questions here. For work that takes time, produces a deliverable "
                "or changes data, pass the person's goal and relevant inputs to start_task "
                "and reply with its task number. Use update_task for a correction to that job. "
                "Ask which job the person means when a follow-up could refer to several."
            )

        return instructions

    def get_toolset(self):
        return FunctionToolset(
            [
                self.start_task,
                self.update_task,
                self.inspect_task,
                self.stop_task,
            ]
        )

    async def prepare_tools(
        self, ctx: RunContext[None], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        if (ctx.metadata or {}).get("bud_revision_pending"):
            return []
        worker = self._worker(ctx)
        return [
            tool
            for tool in tool_defs
            if (
                tool.name not in CONTROL_TOOLS
                if worker
                else self.policy.decide(tool.name) in ("allow", "ask", "decide")
            )
        ]

    async def before_model_request(
        self, ctx: RunContext[None], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if (ctx.metadata or {}).get("bud_revision_pending"):
            return replace(
                request_context,
                model_settings=(request_context.model_settings or {}) | {"tool_choice": "none"},
            )
        if self._worker(ctx):
            return request_context
        jobs = self.store.views(self._feed(ctx))
        active = [job for job in jobs if job.status not in FINISHED]
        recent = [job for job in jobs if job.status in FINISHED][-5:]
        if not active and not recent:
            return request_context
        context = [
            {
                "task_id": job.task_id,
                "title": job.title,
                "status": job.status,
                "approvals": [a.reference for a in job.pending_approvals],
                "result": job.result[:1000] if job.result else None,
            }
            for job in [*active, *recent]
        ]
        return replace(
            request_context,
            messages=[
                *request_context.messages,
                ModelRequest(
                    parts=[
                        UserPromptPart("Current jobs in this conversation:\n" + json.dumps(context))
                    ]
                ),
            ],
        )

    @staticmethod
    def _feed(ctx: RunContext[None]) -> str:
        return (ctx.metadata or {})["bud_session_id"]

    @staticmethod
    def _source(ctx: RunContext[None]) -> str:
        return uuid5(NAMESPACE_URL, f"bud:task:{ctx.run_id}:{ctx.tool_call_id}").hex

    def start_task(
        self,
        ctx: RunContext[None],
        title: Annotated[str, Field(min_length=1, max_length=120)],
        brief: TaskBrief,
    ) -> TaskView:
        """Start an independent job from a short brief. Returns its number; work continues."""
        item = self.store.create(
            self._feed(ctx),
            title,
            brief,
            self._source(ctx),
            (ctx.metadata or {}).get("bud_origin"),
            ctx.run_id,
            skills=(ctx.metadata or {}).get("bud_skills", {}),
        )
        self.kick(item.session_id)
        return self.store.view(item)

    def update_task(
        self,
        ctx: RunContext[None],
        task_id: int,
        instruction: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_CHARS)],
    ) -> TaskView:
        """Continue or revise a named job. Its affected proposal gets a fresh approval."""
        item = self.store.get(self._feed(ctx), task_id)
        self.store.update(
            item,
            instruction,
            self._source(ctx),
            (ctx.metadata or {}).get("bud_origin"),
            ctx.run_id,
        )
        self.kick(item.session_id)
        return self.store.view(item)

    def inspect_task(self, ctx: RunContext[None], task_id: int) -> dict:
        """Read a job's brief, current status, pending decisions and saved results."""
        item = self.store.get(self._feed(ctx), task_id)
        return {"brief": item.brief.model_dump(), **self.store.view(item).model_dump()}

    async def stop_task(self, ctx: RunContext[None], task_id: int) -> dict:
        """Stop a named job and retire its pending approvals."""
        item = self.store.get(self._feed(ctx), task_id)
        return await self.stop(item.session_id)
