"""Durable reminders, one-shot follow-ups, and long shell commands.

A reminder posts directly to chat. A follow-up queues a model run in the owner's chat.
Timers survive a restart and fire late if Bud was down. Commands use the run_command
sandbox. A restart reports an interrupted command without restarting it.

Starting work requires an owner request and goes through the normal approval policy.
"""

import asyncio
import contextlib
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field
from pydantic_ai import RunContext, Tool

from bud.core.events import EventType

logger = logging.getLogger(__name__)

STARTS = frozenset({"remind_me", "schedule_follow_up", "run_in_background"})
TOOLS = STARTS | {"list_background", "cancel_background"}
GUIDANCE = """remind_me posts a reminder in this chat at a set time, with no job and no cost
while it waits. Use it for "remind me", never start_task or a sleep. run_in_background runs a
long shell command, such as a build or a download, while the chat goes on; its result appears
in the chat when it ends. Use schedule_follow_up when the owner asks Bud to check a specific
thread or fact later. list_background and cancel_background manage these items.
"""
LONGEST_WAIT = timedelta(days=30)
MAX_RUNNING = 3
TAIL_CHARS = 600
LATE = timedelta(minutes=1)
# A sleeping Mac pauses the event loop's clock, so waits re-check the wall clock this often.
CHECK_SECONDS = 30.0
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

# Runs one sandboxed command: (command, timeout seconds, owner request) -> run() result.
RunShell = Callable[[str, float, str], Awaitable[dict[str, Any]]]


class Publisher(Protocol):
    def publish_once(
        self, session_id: str, type: EventType, data: dict | None = None, *, key: str | None = None
    ) -> Any: ...


@dataclass(frozen=True)
class Item:
    id: int
    session_id: str
    kind: str
    title: str
    status: str  # waiting, running, done, failed, cancelled, interrupted
    due_at: datetime | None
    command: str | None
    timeout: float | None
    request: str
    prompt: str | None = None
    source_ref: str | None = None

    def view(self) -> dict[str, Any]:
        shown: dict[str, Any] = {"id": self.id, "kind": self.kind, "title": self.title}
        shown["status"] = self.status
        if self.due_at is not None:
            shown["due"] = self.due_at.isoformat(timespec="minutes")
        return shown


_COLUMNS = (
    "id, session_id, kind, title, status, due_at, command, timeout, request, prompt, source_ref"
)


class Background:
    def __init__(
        self,
        path: Path,
        workspace: Path,
        run_shell: RunShell | None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.path = path
        self.workspace = workspace
        self.run_shell = run_shell
        self.now = now
        self.hub: Publisher | None = None
        self.wake_agent: Callable[[Item], bool] | None = None
        self._tasks: dict[int, asyncio.Task] = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, "
                "status TEXT NOT NULL, due_at TEXT, command TEXT, timeout REAL, "
                "request TEXT NOT NULL DEFAULT '', source TEXT UNIQUE)"
            )
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
            for name in ("prompt", "source_ref"):
                if name not in columns:
                    db.execute(f"ALTER TABLE items ADD COLUMN {name} TEXT")
        path.chmod(0o600)

    @contextlib.contextmanager
    def _db(self) -> Generator[sqlite3.Connection]:
        with contextlib.closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    def bind(self, hub: Publisher, wake_agent: Callable[[Item], bool] | None = None) -> None:
        self.hub = hub
        self.wake_agent = wake_agent

    # The store

    def _row(self, row: tuple) -> Item:
        id, session_id, kind, title, status, due_at, command, timeout, request, prompt, ref = row
        due = datetime.fromisoformat(due_at) if due_at else None
        return Item(
            id, session_id, kind, title, status, due, command, timeout, request, prompt, ref
        )

    def _get(self, id: int) -> Item | None:
        with self._db() as db:
            row = db.execute(f"SELECT {_COLUMNS} FROM items WHERE id = ?", (id,)).fetchone()
        return self._row(row) if row else None

    def items(self, session_id: str) -> list[Item]:
        with self._db() as db:
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM items WHERE session_id = ? "
                "AND status IN ('waiting', 'running') ORDER BY id",
                (session_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def _claim(self, id: int, before: str, after: str) -> bool:
        """Move an item from one status to the next; False when something else got there."""
        with self._db() as db:
            changed = db.execute(
                "UPDATE items SET status = ? WHERE id = ? AND status = ?", (after, id, before)
            ).rowcount
        return changed == 1

    def _insert(self, source: str | None, values: dict[str, Any], cap: bool) -> tuple[Item, bool]:
        """Insert once per source. Returns the item and whether this call created it, so a
        replayed tool call finds its first item instead of starting the work again."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if source is not None:
                found = db.execute(
                    f"SELECT {_COLUMNS} FROM items WHERE source = ?", (source,)
                ).fetchone()
                if found:
                    return self._row(found), False
            if cap:
                running = db.execute(
                    "SELECT count(*) FROM items WHERE kind = 'command' AND status = 'running'"
                ).fetchone()[0]
                if running >= MAX_RUNNING:
                    raise ValueError(
                        f"{MAX_RUNNING} commands are already running. Wait for one to end, "
                        "or cancel one."
                    )
            names = ", ".join(values) + ", source"
            marks = ", ".join("?" for _ in values) + ", ?"
            cursor = db.execute(
                f"INSERT INTO items ({names}) VALUES ({marks})", (*values.values(), source)
            )
            row = db.execute(
                f"SELECT {_COLUMNS} FROM items WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._row(row), True

    # Starting work

    def remind(self, session_id: str, text: str, due: datetime, source: str | None = None) -> Item:
        item, created = self._insert(
            source,
            {
                "session_id": session_id,
                "kind": "reminder",
                "title": text,
                "status": "waiting",
                "due_at": due.isoformat(),
            },
            cap=False,
        )
        if created:
            self._watch(item)
        return item

    def follow_up(
        self,
        session_id: str,
        prompt: str,
        source_ref: str,
        due: datetime,
        source: str | None = None,
    ) -> Item:
        item, created = self._insert(
            source,
            {
                "session_id": session_id,
                "kind": "follow_up",
                "title": prompt[:80],
                "status": "waiting",
                "due_at": due.isoformat(),
                "prompt": prompt,
                "source_ref": source_ref,
            },
            cap=False,
        )
        if created:
            self._watch(item)
        return item

    def start(
        self,
        session_id: str,
        title: str,
        command: str,
        timeout: float,
        request: str,
        source: str | None = None,
    ) -> Item:
        if self.run_shell is None:
            raise ValueError("Shell commands are not available on this computer.")
        item, created = self._insert(
            source,
            {
                "session_id": session_id,
                "kind": "command",
                "title": title,
                "status": "running",
                "command": command,
                "timeout": timeout,
                "request": request,
            },
            cap=True,
        )
        if created:
            self._watch(item)
        return item

    def cancel(self, session_id: str, id: int) -> bool:
        """Call on the event loop: it cancels the task that follows the item."""
        item = self._get(id)
        if item is None or item.session_id != session_id:
            return False
        if not (self._claim(id, "waiting", "cancelled") or self._claim(id, "running", "cancelled")):
            return False
        task = self._tasks.pop(id, None)
        if task is not None:
            task.cancel()  # a command's process group is killed and reaped by shell.run
        return True

    def stop_commands(self, session_id: str) -> int:
        """Stop this chat's running commands, as /stop does; reminders keep waiting."""
        stopped = 0
        for item in self.items(session_id):
            if item.kind == "command" and self.cancel(session_id, item.id):
                stopped += 1
        return stopped

    def _watch(self, item: Item) -> None:
        if item.id in self._tasks:
            return
        task = asyncio.get_running_loop().create_task(self._follow(item))
        self._tasks[item.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(item.id, None))

    async def _follow(self, item: Item) -> None:
        try:
            if item.kind == "command":
                await self._run(item)
            else:
                await self._fire(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background item %d failed", item.id)
            if item.kind == "command" and self._claim(item.id, "running", "failed"):
                self._publish(item, "failed", f"“{item.title}” could not run.")

    async def _fire(self, item: Item) -> None:
        assert item.due_at is not None
        # Re-check the wall clock often: a sleeping Mac pauses the loop's own clock.
        while (wait := (item.due_at - self.now()).total_seconds()) > 0:
            await asyncio.sleep(min(wait, CHECK_SECONDS))
        if item.status == "waiting" and not self._claim(item.id, "waiting", "running"):
            return
        while True:
            try:
                if item.kind == "follow_up":
                    accepted = self.wake_agent is not None and self.wake_agent(item)
                else:
                    late = self.now() - item.due_at
                    text = f"Reminder: {item.title}"
                    if late > LATE:
                        text += f" (late: Bud was not running at {_clock(item.due_at)})"
                    accepted = self._publish(item, "done", text)
                if accepted:
                    self._claim(item.id, "running", "done")
                    return
            except Exception:
                logger.exception("Timer %d could not deliver. It will retry.", item.id)
            await asyncio.sleep(CHECK_SECONDS)

    async def _run(self, item: Item) -> None:
        assert self.run_shell is not None and item.command is not None
        result = await self.run_shell(item.command, item.timeout or 0, item.request)
        log = self._save_log(item, result)
        code = result.get("exit_code")
        if result.get("timed_out"):
            status, head = "failed", f"“{item.title}” was stopped: {result['timed_out']}"
        elif code == 0:
            status, head = "done", f"Finished “{item.title}”."
        else:
            status, head = "failed", f"“{item.title}” failed with exit code {code}."
        if not self._claim(item.id, "running", status):
            return  # cancelled
        tail = _tail((result.get("stdout") or "") + (result.get("stderr") or ""))
        text = head + (f"\n{tail}" if tail else "")
        if result.get("blocked_sites"):
            text += "\nBlocked sites: " + ", ".join(result["blocked_sites"])
        self._publish(item, status, text, log)

    def _save_log(self, item: Item, result: dict[str, Any]) -> Path | None:
        """Write background/<id>.log in the workspace, never through a link.

        Sandboxed commands can write the workspace, so they could plant a link where the log
        goes. This server writes outside the sandbox, so every folder is opened without
        following links and the file must not exist yet.
        """
        body = (
            f"$ {item.command}\nexit code: {result.get('exit_code')}\n\n"
            f"--- stdout ---\n{result.get('stdout') or ''}\n"
            f"--- stderr ---\n{result.get('stderr') or ''}\n"
        ).encode()
        name = f"{item.id}.log"
        try:
            root = os.open("/", _DIR_FLAGS)
            try:
                for part in self.workspace.absolute().parts[1:]:
                    child = os.open(part, _DIR_FLAGS, dir_fd=root)
                    os.close(root)
                    root = child
                with contextlib.suppress(FileExistsError):
                    os.mkdir("background", 0o700, dir_fd=root)
                folder = os.open("background", _DIR_FLAGS, dir_fd=root)
            finally:
                os.close(root)
            try:
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=folder,
                )
            finally:
                os.close(folder)
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            return self.workspace / "background" / name
        except OSError as exc:
            logger.warning("Did not save background log %d: %s", item.id, exc)
            return None

    def _publish(self, item: Item, status: str, text: str, log: Path | None = None) -> bool:
        if self.hub is None:
            logger.warning("Background item %d finished before the hub was bound", item.id)
            return False
        data: dict[str, Any] = {"id": item.id, "kind": item.kind, "title": item.title}
        data |= {"status": status, "text": text}
        if log is not None:
            data["report"] = f"background/{log.name}"
            data["report_path"] = str(log)
        self.hub.publish_once(item.session_id, "background.finished", data, key=f"bg:{item.id}")
        return True

    # Server lifetime

    async def resume(self) -> None:
        """Restart timer watchers and report interrupted commands."""
        with self._db() as db:
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM items WHERE status IN ('waiting', 'running')"
            ).fetchall()
        for item in map(self._row, rows):
            if item.kind != "command":
                self._watch(item)
            elif self._claim(item.id, "running", "interrupted"):
                self._publish(
                    item,
                    "interrupted",
                    f"“{item.title}” stopped when Bud restarted, before it finished. It was "
                    "not run again.",
                )

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # The model's tools

    def tools(self) -> list[Tool[None]]:
        background = self

        def session(ctx: RunContext[None]) -> str | None:
            metadata = ctx.metadata or {}
            if metadata.get("bud_task_id") is not None:
                return None  # a job's own session is not where the owner reads
            found = metadata.get("bud_session_id")
            return found if isinstance(found, str) and found else None

        def owner_words(ctx: RunContext[None]) -> str | None:
            """The owner's own request for this turn. A job report or other turn the owner
            did not write has none, and cannot start background work."""
            found = (ctx.metadata or {}).get("bud_request")
            return found if isinstance(found, str) and found.strip() else None

        def source(ctx: RunContext[None]) -> str:
            # The same tool call replayed after a crash maps to the same item.
            return str(uuid5(NAMESPACE_URL, f"bud:background:{ctx.run_id}:{ctx.tool_call_id}"))

        async def remind_me(
            ctx: RunContext[None],
            text: Annotated[str, Field(min_length=1, max_length=300)],
            minutes_from_now: Annotated[float | None, Field(gt=0)] = None,
            at: str | None = None,
        ) -> dict[str, Any]:
            """Post a reminder in this chat at a set time. Nothing runs while it waits.

            Args:
                text: What to remind the person of, in a few words, such as "stretch".
                minutes_from_now: Minutes until the reminder. Give this or `at`.
                at: The time, with its UTC offset, such as 2026-11-21T09:00:00-06:00.
            """
            where = session(ctx)
            if where is None:
                return {"error": "Set reminders from the main conversation, not from a job."}
            if owner_words(ctx) is None:
                return {"error": "Only the person can ask for a reminder."}
            due = due_time(minutes_from_now, at)
            if isinstance(due, str):
                return {"error": due}
            item = background.remind(where, text, due, source(ctx))
            return {"scheduled": item.view()}

        async def schedule_follow_up(
            ctx: RunContext[None],
            prompt: Annotated[str, Field(min_length=1, max_length=1000)],
            source_ref: Annotated[str, Field(min_length=1, max_length=1000)],
            minutes_from_now: Annotated[float | None, Field(gt=0)] = None,
            at: str | None = None,
        ) -> dict[str, Any]:
            """Wake Bud once to check a specific thread or fact in the owner's chat.

            Args:
                prompt: What to check and what finding warrants a reply.
                source_ref: The thread ID, URL, or other stable reference to check.
                minutes_from_now: Minutes until the check. Give this or `at`.
                at: ISO time with a UTC offset.
            """
            where = session(ctx)
            if (
                where is None
                or owner_words(ctx) is None
                or (ctx.metadata or {}).get("bud_occurrence_id") is not None
            ):
                return {"error": "Only the owner can ask for a follow-up in their chat."}
            due = due_time(minutes_from_now, at)
            if isinstance(due, str):
                return {"error": due}
            item = background.follow_up(where, prompt, source_ref, due, source(ctx))
            return {"scheduled": item.view()}

        def due_time(minutes_from_now: float | None, at: str | None) -> datetime | str:
            if (minutes_from_now is None) == (at is None):
                return "Give exactly one of minutes_from_now or at."
            now = background.now()
            if at is not None:
                try:
                    due = datetime.fromisoformat(at)
                except ValueError:
                    return "Give `at` as an ISO time with its offset."
                if due.tzinfo is None:
                    return "Give `at` with its UTC offset, such as -06:00."
            else:
                due = now + timedelta(minutes=minutes_from_now or 0)
            if due <= now:
                return "That time has already passed."
            if due - now > LONGEST_WAIT:
                return "Timers can be at most 30 days away."
            return due

        async def run_in_background(
            ctx: RunContext[None],
            title: Annotated[str, Field(min_length=1, max_length=80)],
            command: Annotated[str, Field(min_length=1)],
            timeout_minutes: Annotated[int, Field(ge=1, le=360)] = 60,
        ) -> dict[str, Any]:
            """Run a long shell command (zsh) in your workspace, sandboxed like run_command,
            while the chat goes on. Its exit status and last lines appear in the chat when it
            ends, and its full output is saved to background/<id>.log in the workspace. It may
            reach package sites and sites the person allowed; any other site is blocked and
            listed. Use run_command for anything that ends within a few minutes.

            Args:
                title: A few words the person will recognize, such as "Build the report".
                command: The command line, run from the workspace folder.
                timeout_minutes: Minutes before the command is stopped, from 1 to 360.
            """
            where = session(ctx)
            if where is None:
                return {"error": "Start background commands from the main conversation."}
            request = owner_words(ctx)
            if request is None:
                return {"error": "Only the person can ask for a background command."}
            try:
                item = background.start(
                    where, title, command, timeout_minutes * 60, request, source(ctx)
                )
            except ValueError as exc:
                return {"error": str(exc)}
            return {"started": item.view()}

        async def list_background(ctx: RunContext[None]) -> dict[str, Any]:
            """List this chat's reminders, follow-ups, and running background commands."""
            where = session(ctx)
            return {"items": [item.view() for item in background.items(where)] if where else []}

        async def cancel_background(ctx: RunContext[None], id: int) -> dict[str, Any]:
            """Cancel a waiting reminder or stop a running background command.

            Args:
                id: The id from remind_me, run_in_background, or list_background.
            """
            where = session(ctx)
            if where is None or not background.cancel(where, id):
                return {"error": f"Nothing waiting or running has id {id} in this chat."}
            return {"cancelled": id}

        return [
            Tool(remind_me),
            Tool(schedule_follow_up),
            Tool(run_in_background),
            Tool(list_background),
            Tool(cancel_background),
        ]


def _clock(when: datetime) -> str:
    return when.astimezone().strftime("%-I:%M %p").lower()


def _tail(output: str) -> str:
    lines = output.strip().splitlines()
    shown = "\n".join(lines[-8:])
    return shown[-TAIL_CHARS:]
