"""Background work the server runs without the model: reminders and long shell commands.

A reminder is a stored due time; when it comes, its text goes straight into the chat, with
no model turn. A command runs in the same sandbox as run_command but without its time limit,
and its exit status and last lines go into the chat when it ends. Both are kept in SQLite, so
a reminder survives a restart and fires late if Bud was down. A command a restart cut short
is reported as interrupted, never run again.
"""

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Protocol

from dbos import DBOS
from pydantic import Field
from pydantic_ai import RunContext, Tool

from bud.core.events import EventType

logger = logging.getLogger(__name__)

TOOLS = {"remind_me", "run_in_background", "list_background", "cancel_background"}
GUIDANCE = """remind_me posts a reminder in this chat at a set time, with no job and no cost
while it waits. Use it for "remind me", never start_task or a sleep. run_in_background runs a
long shell command, such as a build or a download, while the chat goes on; its result appears
in the chat when it ends. list_background and cancel_background manage both.
"""
LONGEST_WAIT = timedelta(days=30)
LONGEST_COMMAND = timedelta(hours=6)
MAX_RUNNING = 3
TAIL_CHARS = 600
LATE = timedelta(minutes=1)

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
    kind: str  # "reminder" or "command"
    title: str
    status: str  # waiting, running, done, failed, cancelled, interrupted
    due_at: datetime | None
    command: str | None
    timeout: float | None
    request: str

    def view(self) -> dict[str, Any]:
        shown: dict[str, Any] = {"id": self.id, "kind": self.kind, "title": self.title}
        shown["status"] = self.status
        if self.due_at is not None:
            shown["due"] = self.due_at.isoformat(timespec="minutes")
        return shown


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
        self._tasks: dict[int, asyncio.Task] = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, "
                "status TEXT NOT NULL, due_at TEXT, command TEXT, timeout REAL, "
                "request TEXT NOT NULL DEFAULT '')"
            )
        path.chmod(0o600)

    @contextlib.contextmanager
    def _db(self) -> Generator[sqlite3.Connection]:
        with contextlib.closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    def bind(self, hub: Publisher) -> None:
        self.hub = hub

    # The store

    def _row(self, row: tuple) -> Item:
        id, session_id, kind, title, status, due_at, command, timeout, request = row
        due = datetime.fromisoformat(due_at) if due_at else None
        return Item(id, session_id, kind, title, status, due, command, timeout, request)

    def _get(self, id: int) -> Item | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM items WHERE id = ?", (id,)).fetchone()
        return self._row(row) if row else None

    def items(self, session_id: str) -> list[Item]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM items WHERE session_id = ? AND status IN ('waiting', 'running') "
                "ORDER BY id",
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

    # Starting work

    def remind(self, session_id: str, text: str, due: datetime) -> Item:
        with self._db() as db:
            cursor = db.execute(
                "INSERT INTO items (session_id, kind, title, status, due_at) "
                "VALUES (?, 'reminder', ?, 'waiting', ?)",
                (session_id, text, due.isoformat()),
            )
        item = self._get(cursor.lastrowid or 0)
        assert item is not None
        self._watch(item)
        return item

    def start(
        self, session_id: str, title: str, command: str, timeout: float, request: str
    ) -> Item:
        if self.run_shell is None:
            raise ValueError("Shell commands are not available on this computer.")
        with self._db() as db:
            running = db.execute(
                "SELECT count(*) FROM items WHERE kind = 'command' AND status = 'running'"
            ).fetchone()[0]
            if running >= MAX_RUNNING:
                raise ValueError(
                    f"{MAX_RUNNING} commands are already running. Wait for one to end, or "
                    "cancel one."
                )
            cursor = db.execute(
                "INSERT INTO items (session_id, kind, title, status, command, timeout, request) "
                "VALUES (?, 'command', ?, 'running', ?, ?, ?)",
                (session_id, title, command, timeout, request),
            )
        item = self._get(cursor.lastrowid or 0)
        assert item is not None
        self._watch(item)
        return item

    def cancel(self, session_id: str, id: int) -> bool:
        item = self._get(id)
        if item is None or item.session_id != session_id:
            return False
        if not (self._claim(id, "waiting", "cancelled") or self._claim(id, "running", "cancelled")):
            return False
        task = self._tasks.pop(id, None)
        if task is not None:
            task.cancel()  # a command's process group is killed by shell.run
        return True

    def _watch(self, item: Item) -> None:
        task = asyncio.get_running_loop().create_task(self._follow(item))
        self._tasks[item.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(item.id, None))

    async def _follow(self, item: Item) -> None:
        try:
            if item.kind == "reminder":
                await self._fire(item)
            else:
                await self._run(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background item %d failed", item.id)

    async def _fire(self, item: Item) -> None:
        assert item.due_at is not None
        wait = (item.due_at - self.now()).total_seconds()
        if wait > 0:
            await asyncio.sleep(wait)
        if not self._claim(item.id, "waiting", "done"):
            return  # cancelled while it waited
        late = self.now() - item.due_at
        text = f"Reminder: {item.title}"
        if late > LATE:
            text += f" (late: Bud was not running at {_clock(item.due_at)})"
        self._publish(item, "done", text)

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
        folder = self.workspace / "background"
        try:
            folder.mkdir(exist_ok=True)
            path = folder / f"{item.id}.log"
            path.write_text(
                f"$ {item.command}\nexit code: {result.get('exit_code')}\n\n"
                f"--- stdout ---\n{result.get('stdout') or ''}\n"
                f"--- stderr ---\n{result.get('stderr') or ''}\n"
            )
            return path
        except OSError as exc:
            logger.warning("Could not save background log %d: %s", item.id, exc)
            return None

    def _publish(self, item: Item, status: str, text: str, log: Path | None = None) -> None:
        if self.hub is None:
            logger.warning("Background item %d finished before the hub was bound", item.id)
            return
        data: dict[str, Any] = {"id": item.id, "kind": item.kind, "title": item.title}
        data |= {"status": status, "text": text}
        if log is not None:
            data["report"] = f"background/{log.name}"
            data["report_path"] = str(log)
        self.hub.publish_once(item.session_id, "background.finished", data, key=f"bg:{item.id}")

    # Server lifetime

    async def resume(self) -> None:
        """At startup: wait again for every reminder, and report commands a restart cut off."""
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM items WHERE status IN ('waiting', 'running')"
            ).fetchall()
        for item in map(self._row, rows):
            if item.kind == "reminder":
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

        async def once(name: str, step: Callable[[], Any]) -> Any:
            if DBOS.workflow_id is not None and DBOS.step_id is None:
                # One recorded step: crash recovery returns this result instead of
                # scheduling the same work twice.
                async def run() -> Any:
                    return step()

                return await DBOS.run_step_async({"name": name, "retries_allowed": False}, run)
            return step()

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
            if (minutes_from_now is None) == (at is None):
                return {"error": "Give exactly one of minutes_from_now or at."}
            now = background.now()
            if at is not None:
                try:
                    due = datetime.fromisoformat(at)
                except ValueError:
                    return {"error": "Give `at` as an ISO time with its offset."}
                if due.tzinfo is None:
                    return {"error": "Give `at` with its UTC offset, such as -06:00."}
            else:
                due = now + timedelta(minutes=minutes_from_now or 0)
            if due <= now:
                return {"error": "That time has already passed."}
            if due - now > LONGEST_WAIT:
                return {"error": "Reminders can be at most 30 days away."}
            scheduled = await once("bud.remind", lambda: background.remind(where, text, due).view())
            return {"scheduled": scheduled}

        async def run_in_background(
            ctx: RunContext[None],
            title: Annotated[str, Field(min_length=1, max_length=80)],
            command: Annotated[str, Field(min_length=1)],
            timeout_minutes: Annotated[int, Field(ge=1, le=360)] = 60,
        ) -> dict[str, Any]:
            """Run a long shell command (zsh) in your workspace, sandboxed like run_command,
            while the chat goes on. Its exit status and last lines appear in the chat when it
            ends, and its full output is saved to background/<id>.log in the workspace. Use
            run_command for anything that ends within a few minutes.

            Args:
                title: A few words the person will recognize, such as "Build the report".
                command: The command line, run from the workspace folder.
                timeout_minutes: Minutes before the command is stopped, from 1 to 360.
            """
            where = session(ctx)
            if where is None:
                return {"error": "Start background commands from the main conversation."}
            found = (ctx.metadata or {}).get("bud_request")
            request = found if isinstance(found, str) else ""

            def start() -> dict[str, Any]:
                try:
                    item = background.start(where, title, command, timeout_minutes * 60, request)
                except ValueError as exc:
                    return {"error": str(exc)}
                return {"started": item.view()}

            return await once("bud.background", start)

        def list_background(ctx: RunContext[None]) -> dict[str, Any]:
            """List this chat's waiting reminders and running background commands."""
            where = session(ctx)
            return {"items": [item.view() for item in background.items(where)] if where else []}

        def cancel_background(ctx: RunContext[None], id: int) -> dict[str, Any]:
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
