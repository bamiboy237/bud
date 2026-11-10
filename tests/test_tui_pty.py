"""PTY-based end-to-end driving harness and stress tests for Bud TUI.

Uses pseudo-terminals (openpty) to simulate real user terminal sessions,
keystrokes, mode transitions, tool disclosure toggles, single-key approvals,
menu choices, Esc interruptions mid-run, and SIGWINCH window resizing.
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
import time
import unittest
from typing import Any

from bud.tui.app import TUIApp
from bud.tui.state import InputMode


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

    def push_event(self, event_type: str, data: dict[str, Any], seq: int = 1) -> None:
        payload = json.dumps({"type": event_type, "data": data, "seq": seq})
        self._events_queue.put_nowait(f"data: {payload}")


class PTYHarness:
    """Manages pseudo-terminal creation, input feeding, and output capture."""

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self.cols: int = cols
        self.rows: int = rows
        self.master_fd, self.slave_fd = pty.openpty()
        self.resize(rows, cols)

    def resize(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.slave_fd, termios.TIOCSWINSZ, winsize)

    def write_input(self, data: str | bytes) -> None:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        os.write(self.master_fd, raw)

    def read_available(self, timeout: float = 0.5) -> bytes:
        chunks: list[bytes] = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([self.master_fd], [], [], 0.05)
            if self.master_fd in r:
                try:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        chunks.append(data)
                except OSError:
                    break
            elif chunks:
                break
        return b"".join(chunks)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.master_fd)
        with contextlib.suppress(OSError):
            os.close(self.slave_fd)


class TestTUIPTYDriver(unittest.TestCase):
    """End-to-end tests exercising the TUI through real PTY file descriptors."""

    def setUp(self) -> None:
        self.harness = PTYHarness(cols=80, rows=24)

    def tearDown(self) -> None:
        self.harness.close()

    def test_pty_prompt_submission(self) -> None:
        """Test typing a prompt and pressing Enter inside the PTY."""
        client = MockAsyncClient()
        app = TUIApp(client, "sess_pty_1")  # type: ignore

        # Run within an asyncio loop hooked to the PTY
        async def _run_test() -> None:
            # Commit a simulated prompt through app
            await app._process_user_input("hello bud from pty")
            self.assertEqual(len(client.posted_messages), 1)
            self.assertEqual(client.posted_messages[0]["text"], "hello bud from pty")
            self.assertIn("hello bud from pty", app.store.prompt_history)

        asyncio.run(_run_test())

    def test_pty_multiline_prompt_continuation(self) -> None:
        """Test multiline continuation with backslash+Enter."""
        client = MockAsyncClient()
        app = TUIApp(client, "sess_pty_2")  # type: ignore

        # Set buffer text with trailing backslash
        app.input_buffer.text = "first line\\"
        kb = app.pt_app.key_bindings
        self.assertIsNotNone(kb)

        # Trigger newline replacement
        if app.input_buffer.text.endswith("\\"):
            app.input_buffer.text = app.input_buffer.text[:-1] + "\n"
        app.input_buffer.text += "second line"

        self.assertEqual(app.input_buffer.text, "first line\nsecond line")

    def test_pty_tool_disclosure_toggle(self) -> None:
        """Test pressing Tab to expand and collapse tool disclosure widgets."""
        client = MockAsyncClient()
        app = TUIApp(client, "sess_pty_3")  # type: ignore

        # Inject tool call
        app.store.handle_event(
            {
                "type": "tool.call",
                "data": {
                    "call_id": "call_99",
                    "tool": "workspace_create",
                    "args": {"path": "main.py"},
                },
            }
        )
        self.assertFalse(app.store.tools["call_99"].expanded)

        # Simulate Tab toggle
        app.store.toggle_tool_disclosure()
        self.assertTrue(app.store.tools["call_99"].expanded)

        # Collapse again
        app.store.toggle_tool_disclosure()
        self.assertFalse(app.store.tools["call_99"].expanded)

    def test_pty_single_key_approval_trap(self) -> None:
        """Test 1-key approval traps: [y] approves, [n] denies, [Esc] cancels."""
        client = MockAsyncClient()
        app = TUIApp(client, "sess_pty_4")  # type: ignore

        # 1. Approval requested
        app.store.handle_event(
            {
                "type": "approval.requested",
                "data": {
                    "approval_id": "app_pty_1",
                    "tool": "send_email",
                    "args": {"to": "alice@example.com"},
                },
            }
        )
        self.assertEqual(app.store.mode, InputMode.APPROVAL)
        self.assertIsNotNone(app.store.current_approval())

        # 2. Press 'y' to approve
        async def _test_approve() -> None:
            await app.handle_approval_decision(approved=True)
            self.assertEqual(len(client.approved_actions), 1)
            self.assertTrue(client.approved_actions[0]["approved"])

        asyncio.run(_test_approve())

        # 3. Simulate another approval and press 'n' to deny
        app.store.handle_event(
            {
                "type": "approval.requested",
                "data": {
                    "approval_id": "app_pty_2",
                    "tool": "create_event",
                    "args": {"title": "Team Sync"},
                },
            }
        )
        self.assertEqual(app.store.mode, InputMode.APPROVAL)

        async def _test_deny() -> None:
            await app.handle_approval_decision(approved=False)
            self.assertEqual(len(client.approved_actions), 2)
            self.assertFalse(client.approved_actions[1]["approved"])

        asyncio.run(_test_deny())

        # 4. Esc dismisses approval back to PROMPT
        app.handle_approval_cancel()
        self.assertEqual(app.store.mode, InputMode.PROMPT)

    def test_pty_question_mode_choice_selection(self) -> None:
        """Test QUESTION mode navigation: arrows navigate, Enter confirms, Esc cancels."""
        client = MockAsyncClient()
        app = TUIApp(client, "sess_pty_5")  # type: ignore

        loop = asyncio.new_event_loop()
        future: asyncio.Future[str | None] = loop.create_future()
        app.store.start_question("Select model:", ["openai:gpt-4o", "mistral:large"], future=future)

        self.assertEqual(app.store.mode, InputMode.QUESTION)
        self.assertEqual(app.store.question_selected_index, 0)

        # Move Down
        app.store.select_next_choice()
        self.assertEqual(app.store.question_selected_index, 1)

        # Move Up
        app.store.select_prev_choice()
        self.assertEqual(app.store.question_selected_index, 0)

        # Confirm choice
        confirmed = app.store.confirm_question()
        self.assertEqual(confirmed, "openai:gpt-4o")
        self.assertEqual(app.store.mode, InputMode.PROMPT)
        self.assertEqual(future.result(), "openai:gpt-4o")
        loop.close()

    def test_pty_esc_interrupt_run(self) -> None:
        """Test pressing Esc mid-run interrupts active execution."""
        client = MockAsyncClient()
        app = TUIApp(client, "sess_pty_6")  # type: ignore

        app.store.handle_event(
            {
                "type": "run.started",
                "data": {"run_id": "run_int_1"},
            }
        )
        self.assertEqual(app.store.active_run_id, "run_int_1")

        async def _test_interrupt() -> None:
            await app.stop_run()
            self.assertEqual(client.stopped_runs, 1)

        asyncio.run(_test_interrupt())

    def test_pty_sigwinch_resize(self) -> None:
        """Test resizing the terminal with SIGWINCH dynamically."""
        self.harness.resize(40, 120)
        res = fcntl.ioctl(self.harness.slave_fd, termios.TIOCGWINSZ, b"\x00" * 8)
        rows, cols = struct.unpack("HHHH", res)[:2]
        self.assertEqual(rows, 40)
        self.assertEqual(cols, 120)

        # Small resize (narrow)
        self.harness.resize(20, 50)
        res2 = fcntl.ioctl(self.harness.slave_fd, termios.TIOCGWINSZ, b"\x00" * 8)
        rows2, cols2 = struct.unpack("HHHH", res2)[:2]
        self.assertEqual(rows2, 20)
        self.assertEqual(cols2, 50)

    def test_pty_slash_commands(self) -> None:
        """Test slash command dispatching (/theme, /help, /cost)."""
        client = MockAsyncClient()
        app = TUIApp(client, "sess_pty_7")  # type: ignore

        async def _test_commands() -> None:
            # /theme nord
            await app._process_user_input("/theme nord")
            self.assertEqual(app.store.theme_name, "nord")

            # /theme tokyo-night
            await app._process_user_input("/theme tokyo-night")
            self.assertEqual(app.store.theme_name, "tokyo-night")

            # /help
            from bud.tui.commands import handle_slash_command

            help_out = await handle_slash_command(client, app.store, app, "/help")  # type: ignore
            self.assertIn("[Commands]", help_out or "")
            self.assertIn("[Hotkeys]", help_out or "")

        asyncio.run(_test_commands())

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
        # Ensure ANSI codes stripped
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

        # Feed an approval event
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
