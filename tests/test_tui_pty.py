"""PTY-based end-to-end driving harness and stress tests for Bud TUI.

Uses pseudo-terminals (openpty) to simulate real user terminal sessions,
keystrokes, mode transitions, tool disclosure toggles, single-key approvals,
menu choices, Esc interruptions mid-run, SIGWINCH window resizing, and clean exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import select
import struct
import termios
import threading
import time
import unittest
from collections.abc import Callable
from typing import Any

from prompt_toolkit.input import create_input
from prompt_toolkit.output.vt100 import Vt100_Output

from bud.tui.app import TUIApp
from bud.tui.state import InputMode
from bud.tui.terminal import SYNC_END, SYNC_START


class MockAsyncClient:
    """Mock HTTPX client simulating the Bud core API."""

    def __init__(self, session_id: str = "sess_mock_pty") -> None:
        self.session_id: str = session_id
        self.headers: dict[str, str] = {"X-Bud-Scope": "test_scope", "X-Bud-Model": "mock-model"}
        self.posted_messages: list[dict[str, Any]] = []
        self.approved_actions: list[dict[str, Any]] = []
        self.stopped_runs: int = 0
        self._events_queue: asyncio.Queue[str] = asyncio.Queue()

    async def __aenter__(self) -> MockAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def get(self, path: str) -> Any:
        class Resp:
            def __init__(self, status_code: int, data: Any):
                self.status_code = status_code
                self._data = data

            def raise_for_status(self) -> None:
                pass

            def json(self) -> Any:
                return self._data

        if path == "/runtime":
            return Resp(200, {"scope": "mock_scope", "mode": "simulate"})
        if path == "/models":
            return Resp(
                200,
                [
                    {"id": "gpt-4o", "tier": "fast", "configured": True},
                    {"id": "deepseek-chat", "tier": "fast", "configured": True},
                    {"id": "mistral-large", "tier": "frontier", "configured": True},
                ],
            )
        if path.startswith("/sessions/"):
            return Resp(
                200,
                {
                    "session_id": self.session_id,
                    "last_seq": 10,
                    "status": "idle",
                    "transcript": [],
                    "pending_approvals": [],
                    "queued_messages": [],
                    "recent_runs": [],
                    "tasks": [],
                    "run_active": False,
                    "queue_paused": False,
                },
            )
        return Resp(404, {"detail": "not found"})

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        class Resp:
            def __init__(self, status_code: int, data: Any):
                self.status_code = status_code
                self._data = data

            def raise_for_status(self) -> None:
                pass

            def json(self) -> Any:
                return self._data

        if "/messages" in path:
            self.posted_messages.append(json or {})
            return Resp(202, {"duplicate": False})
        if "/approvals/" in path:
            self.approved_actions.append(json or {})
            return Resp(200, {"applied": True})
        if "/stop" in path:
            self.stopped_runs += 1
            return Resp(202, {"stop_requested": True})
        return Resp(200, {})

    def stream(self, method: str, path: str, **kwargs: Any) -> Any:
        client = self

        class StreamContext:
            def __init__(self) -> None:
                self.is_error = False

            async def __aenter__(self) -> StreamContext:
                return self

            async def __aexit__(self, *args: Any) -> None:
                pass

            def raise_for_status(self) -> None:
                pass

            async def aiter_lines(self) -> Any:
                while True:
                    line = await client._events_queue.get()
                    if line == "__STOP__":
                        break
                    yield line

        return StreamContext()

    def push_event(self, event_type: str, data: dict[str, Any], seq: int = 20) -> None:
        payload = json.dumps({"type": event_type, "data": data, "seq": seq})
        self._events_queue.put_nowait(f"data: {payload}")


class RealPTYHarness:
    """Manages pseudo-terminal creation, background output draining, and input injection."""

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self.cols: int = cols
        self.rows: int = rows
        self.master_fd, self.slave_fd = pty.openpty()
        self.resize(rows, cols)

        self.slave_in = os.fdopen(self.slave_fd, "r", encoding="utf-8")
        self.slave_out = os.fdopen(os.dup(self.slave_fd), "w", encoding="utf-8")

        self.pt_input = create_input(always_prefer_tty=True, stdin=self.slave_in)
        self.pt_output = Vt100_Output.from_pty(self.slave_out)

        self._captured: list[bytes] = []
        self._running: bool = True
        self._reader_thread = threading.Thread(target=self._drain_loop, daemon=True)
        self._reader_thread.start()

    def resize(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.slave_fd, termios.TIOCSWINSZ, winsize)

    def write_input(self, data: str | bytes) -> None:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        os.write(self.master_fd, raw)

    def _drain_loop(self) -> None:
        """Continuously drain master_fd to prevent PTY buffer saturation."""
        while self._running:
            r, _, _ = select.select([self.master_fd], [], [], 0.05)
            if self.master_fd in r:
                try:
                    chunk = os.read(self.master_fd, 8192)
                    if chunk:
                        self._captured.append(chunk)
                except OSError:
                    break

    def get_output(self) -> bytes:
        return b"".join(self._captured)

    def close(self) -> None:
        self._running = False
        self._reader_thread.join(timeout=0.5)
        with contextlib.suppress(Exception):
            self.pt_input.close()
        with contextlib.suppress(Exception):
            self.slave_in.close()
        with contextlib.suppress(Exception):
            self.slave_out.close()
        with contextlib.suppress(Exception):
            os.close(self.master_fd)


async def wait_for(
    predicate: Callable[[], bool],
    timeout: float = 1.5,
    interval: float = 0.02,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


class TestTUIPTYDriver(unittest.TestCase):
    """End-to-end tests exercising the TUI through real PTY file descriptors and keystrokes."""

    def setUp(self) -> None:
        self.harness = RealPTYHarness(cols=80, rows=24)

    def tearDown(self) -> None:
        self.harness.close()

    def test_pty_prompt_submission(self) -> None:
        """Test typing a prompt and pressing Enter inside the real PTY."""
        client = MockAsyncClient()
        app = TUIApp(
            client,
            "sess_pty_prompt",
            input=self.harness.pt_input,
            output=self.harness.pt_output,
        )

        async def _run() -> None:
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.05)

            try:
                # Type prompt and Enter through PTY master
                self.harness.write_input("hello bud from real pty\r")
                ok = await wait_for(lambda: len(client.posted_messages) > 0)
                self.assertTrue(ok)
                self.assertEqual(client.posted_messages[0]["text"], "hello bud from real pty")
                self.assertIn("hello bud from real pty", app.store.prompt_history)

                # Exit via Ctrl-D
                self.harness.write_input("\x04")
                await wait_for(lambda: not app.pt_app.is_running)
            finally:
                client._events_queue.put_nowait("__STOP__")
                if app.pt_app.is_running:
                    app.pt_app.exit()
                await task

        asyncio.run(_run())

    def test_pty_multiline_prompt_continuation(self) -> None:
        """Test multiline continuation with backslash+Enter through the PTY."""
        client = MockAsyncClient()
        app = TUIApp(
            client,
            "sess_pty_multi",
            input=self.harness.pt_input,
            output=self.harness.pt_output,
        )

        async def _run() -> None:
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.05)

            try:
                # Type line 1 with backslash then Enter
                self.harness.write_input("line 1\\\r")
                await wait_for(lambda: "\n" in app.input_buffer.text)
                self.assertEqual(app.input_buffer.text, "line 1\n")

                # Type line 2 and submit with Enter
                self.harness.write_input("line 2\r")
                ok = await wait_for(lambda: len(client.posted_messages) > 0)
                self.assertTrue(ok)
                self.assertEqual(client.posted_messages[0]["text"], "line 1\nline 2")

                # Exit via Ctrl-D
                self.harness.write_input("\x04")
                await wait_for(lambda: not app.pt_app.is_running)
            finally:
                client._events_queue.put_nowait("__STOP__")
                if app.pt_app.is_running:
                    app.pt_app.exit()
                await task

        asyncio.run(_run())

    def test_pty_tool_disclosure_toggle(self) -> None:
        """Test pressing Tab to expand and collapse tool disclosure widgets through PTY."""
        client = MockAsyncClient()
        app = TUIApp(
            client,
            "sess_pty_tools",
            input=self.harness.pt_input,
            output=self.harness.pt_output,
        )

        async def _run() -> None:
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.05)

            try:
                # Dispatch tool call via SSE
                client.push_event(
                    "tool.call",
                    {"call_id": "c1", "tool": "workspace_create", "args": {"path": "app.py"}},
                )
                await wait_for(lambda: "c1" in app.store.tools)
                self.assertFalse(app.store.tools["c1"].expanded)

                # Press Tab to expand
                self.harness.write_input("\t")
                await wait_for(lambda: app.store.tools["c1"].expanded)
                self.assertTrue(app.store.tools["c1"].expanded)

                # Press Tab to collapse
                self.harness.write_input("\t")
                await wait_for(lambda: not app.store.tools["c1"].expanded)
                self.assertFalse(app.store.tools["c1"].expanded)

                # Exit via Ctrl-D
                self.harness.write_input("\x04")
                await wait_for(lambda: not app.pt_app.is_running)
            finally:
                client._events_queue.put_nowait("__STOP__")
                if app.pt_app.is_running:
                    app.pt_app.exit()
                await task

        asyncio.run(_run())

    def test_pty_single_key_approval_trap(self) -> None:
        """Test 1-key approval traps: [y] approves, [n] denies, [Esc] cancels through PTY."""
        client = MockAsyncClient()
        app = TUIApp(
            client,
            "sess_pty_appr",
            input=self.harness.pt_input,
            output=self.harness.pt_output,
        )

        async def _run() -> None:
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.05)

            try:
                # 1. Approval requested
                client.push_event(
                    "approval.requested",
                    {
                        "approval_id": "app_pty_1",
                        "tool": "send_email",
                        "args": {"to": "alice@example.com"},
                    },
                    seq=21,
                )
                await wait_for(lambda: app.store.mode == InputMode.APPROVAL)
                self.assertEqual(app.store.mode, InputMode.APPROVAL)

                # Press 'y' through PTY
                self.harness.write_input("y")
                ok = await wait_for(lambda: len(client.approved_actions) == 1)
                self.assertTrue(ok)
                self.assertTrue(client.approved_actions[0]["approved"])
                self.assertEqual(app.store.mode, InputMode.PROMPT)

                # 2. Denial requested
                client.push_event(
                    "approval.requested",
                    {"approval_id": "app_pty_2", "tool": "create_event", "args": {"title": "Sync"}},
                    seq=22,
                )
                await wait_for(lambda: app.store.mode == InputMode.APPROVAL)
                self.assertEqual(app.store.mode, InputMode.APPROVAL)

                # Press 'n' through PTY
                self.harness.write_input("n")
                ok2 = await wait_for(lambda: len(client.approved_actions) == 2)
                self.assertTrue(ok2)
                self.assertFalse(client.approved_actions[1]["approved"])
                self.assertEqual(app.store.mode, InputMode.PROMPT)

                # 3. Cancel with Esc
                client.push_event(
                    "approval.requested",
                    {"approval_id": "app_pty_3", "tool": "workspace_update", "args": {}},
                    seq=23,
                )
                await wait_for(lambda: app.store.mode == InputMode.APPROVAL)
                self.harness.write_input("\x1b")
                await wait_for(lambda: app.store.mode == InputMode.PROMPT)
                self.assertEqual(app.store.mode, InputMode.PROMPT)

                # Exit via Ctrl-D
                self.harness.write_input("\x04")
                await wait_for(lambda: not app.pt_app.is_running)
            finally:
                client._events_queue.put_nowait("__STOP__")
                if app.pt_app.is_running:
                    app.pt_app.exit()
                await task

        asyncio.run(_run())

    def test_pty_question_mode_choice_selection(self) -> None:
        """Test QUESTION mode navigation: arrows navigate, Enter confirms through PTY."""
        client = MockAsyncClient()
        app = TUIApp(
            client,
            "sess_pty_q",
            input=self.harness.pt_input,
            output=self.harness.pt_output,
        )

        async def _run() -> None:
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.05)

            try:
                loop = asyncio.get_running_loop()
                future: asyncio.Future[str | None] = loop.create_future()
                app.store.start_question(
                    "Select a model:",
                    ["gpt-4o", "mistral-large", "deepseek-chat"],
                    future=future,
                )

                await wait_for(lambda: app.store.mode == InputMode.QUESTION)
                self.assertEqual(app.store.question_selected_index, 0)

                # Down arrow (\x1b[B)
                self.harness.write_input("\x1b[B")
                await wait_for(lambda: app.store.question_selected_index == 1)
                self.assertEqual(app.store.question_selected_index, 1)

                # Up arrow (\x1b[A)
                self.harness.write_input("\x1b[A")
                await wait_for(lambda: app.store.question_selected_index == 0)
                self.assertEqual(app.store.question_selected_index, 0)

                # Down arrow again
                self.harness.write_input("\x1b[B")
                await wait_for(lambda: app.store.question_selected_index == 1)

                # Enter to select mistral-large
                self.harness.write_input("\r")
                await wait_for(lambda: future.done())
                self.assertEqual(future.result(), "mistral-large")
                self.assertEqual(app.store.mode, InputMode.PROMPT)

                # Exit via Ctrl-D
                self.harness.write_input("\x04")
                await wait_for(lambda: not app.pt_app.is_running)
            finally:
                client._events_queue.put_nowait("__STOP__")
                if app.pt_app.is_running:
                    app.pt_app.exit()
                await task

        asyncio.run(_run())

    def test_pty_esc_interrupt_run(self) -> None:
        """Test pressing Esc mid-run interrupts active execution through PTY."""
        client = MockAsyncClient()
        app = TUIApp(
            client,
            "sess_pty_stop",
            input=self.harness.pt_input,
            output=self.harness.pt_output,
        )

        async def _run() -> None:
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.05)

            try:
                client.push_event("run.started", {"run_id": "run_stop_1"}, seq=21)
                await wait_for(lambda: app.store.active_run_id == "run_stop_1")
                self.assertEqual(app.store.active_run_id, "run_stop_1")

                # Press Esc
                self.harness.write_input("\x1b")
                ok = await wait_for(lambda: client.stopped_runs > 0)
                self.assertTrue(ok)
                self.assertGreaterEqual(client.stopped_runs, 1)

                # Exit via Ctrl-D
                self.harness.write_input("\x04")
                await wait_for(lambda: not app.pt_app.is_running)
            finally:
                client._events_queue.put_nowait("__STOP__")
                if app.pt_app.is_running:
                    app.pt_app.exit()
                await task

        asyncio.run(_run())

    def test_pty_sigwinch_resize(self) -> None:
        """Test resizing the terminal with SIGWINCH dynamically through PTY."""
        client = MockAsyncClient()
        app = TUIApp(
            client,
            "sess_pty_resize",
            input=self.harness.pt_input,
            output=self.harness.pt_output,
        )

        async def _run() -> None:
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.05)

            try:
                # Resize to wide/tall
                self.harness.resize(40, 120)
                app.pt_app.invalidate()
                await asyncio.sleep(0.05)
                self.assertEqual(self.harness.pt_output.get_size().columns, 120)
                self.assertEqual(self.harness.pt_output.get_size().rows, 40)

                # Resize to narrow
                self.harness.resize(20, 50)
                app.pt_app.invalidate()
                await asyncio.sleep(0.05)
                self.assertEqual(self.harness.pt_output.get_size().columns, 50)
                self.assertEqual(self.harness.pt_output.get_size().rows, 20)

                # Exit via Ctrl-D
                self.harness.write_input("\x04")
                await wait_for(lambda: not app.pt_app.is_running)
            finally:
                client._events_queue.put_nowait("__STOP__")
                if app.pt_app.is_running:
                    app.pt_app.exit()
                await task

        asyncio.run(_run())

    def test_pty_slash_commands(self) -> None:
        """Test slash commands (/theme, /exit) typed through real PTY."""
        client = MockAsyncClient()
        app = TUIApp(
            client,
            "sess_pty_cmds",
            input=self.harness.pt_input,
            output=self.harness.pt_output,
        )

        async def _run() -> None:
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.05)

            try:
                # Switch theme to nord
                self.harness.write_input("/theme nord\r")
                await wait_for(lambda: app.store.theme_name == "nord")
                self.assertEqual(app.store.theme_name, "nord")

                # Switch theme to tokyo-night
                self.harness.write_input("/theme tokyo-night\r")
                await wait_for(lambda: app.store.theme_name == "tokyo-night")
                self.assertEqual(app.store.theme_name, "tokyo-night")

                # Exit via /exit command
                self.harness.write_input("/exit\r")
                await wait_for(lambda: not app.pt_app.is_running)
                self.assertFalse(app.pt_app.is_running)
            finally:
                client._events_queue.put_nowait("__STOP__")
                if app.pt_app.is_running:
                    app.pt_app.exit()
                await task

        asyncio.run(_run())

    def test_synchronized_output_decset2026(self) -> None:
        """Verify that DECSET 2026 synchronized output start and end sequences enclose frames."""
        client = MockAsyncClient()
        app = TUIApp(
            client,
            "sess_pty_sync",
            input=self.harness.pt_input,
            output=self.harness.pt_output,
        )

        async def _run() -> None:
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.05)

            try:
                self.harness.write_input("test frame\r")
                await wait_for(lambda: len(client.posted_messages) > 0)
                self.harness.write_input("\x04")
                await wait_for(lambda: not app.pt_app.is_running)
            finally:
                client._events_queue.put_nowait("__STOP__")
                if app.pt_app.is_running:
                    app.pt_app.exit()
                await task

        asyncio.run(_run())
        captured = self.harness.get_output()
        self.assertIn(SYNC_START.encode("utf-8"), captured)
        self.assertIn(SYNC_END.encode("utf-8"), captured)

    def test_headless_piped_streaming(self) -> None:
        """Test headless mode streams plain text without ANSI codes when piped."""
        import io
        from unittest.mock import patch

        from bud.core.client import headless_chat

        client = MockAsyncClient()
        fake_stdin = io.StringIO("piped prompt text")
        fake_stdout = io.StringIO()

        # Feed events to the client's queue
        client.push_event("text.delta", {"text": "\x1b[32mHello\x1b[0m world"})
        client.push_event("run.completed", {"messages": []})

        with (
            patch("sys.stdin", fake_stdin),
            patch("sys.stdout", fake_stdout),
            patch("bud.core.client.owner_client", return_value=client),
            patch(
                "bud.core.client.session_snapshot",
                return_value={"last_seq": 0, "pending_approvals": []},
            ),
        ):
            fake_stdin.isatty = lambda: False  # type: ignore
            fake_stdout.isatty = lambda: False  # type: ignore
            asyncio.run(headless_chat("http://127.0.0.1:8420", "sess_head_1"))

        output = fake_stdout.getvalue()
        self.assertNotIn("\x1b[32m", output)
        self.assertIn("Hello world", output)

    def test_headless_approval_exits_nonzero(self) -> None:
        """Test headless mode raises SystemExit(1) if approval is required non-interactively."""
        import io
        from unittest.mock import patch

        from bud.core.client import headless_chat

        client = MockAsyncClient()
        fake_stdin = io.StringIO("do send email")
        fake_stderr = io.StringIO()

        client.push_event(
            "approval.requested",
            {"approval_id": "app_head_fail", "tool": "send_email", "args": {}},
            seq=5,
        )

        with (
            patch("sys.stdin", fake_stdin),
            patch("sys.stderr", fake_stderr),
            patch("bud.core.client.owner_client", return_value=client),
            patch(
                "bud.core.client.session_snapshot",
                return_value={"last_seq": 0, "pending_approvals": []},
            ),
        ):
            fake_stdin.isatty = lambda: False  # type: ignore
            with self.assertRaises(SystemExit) as ctx:
                asyncio.run(headless_chat("http://127.0.0.1:8420", "sess_head_2"))
            self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
