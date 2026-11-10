"""Main TUI coordinator integrating SSE events, differential painting, and prompt-toolkit input."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from uuid import uuid4

import httpx
from prompt_toolkit import Application, print_formatted_text
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output import create_output

from bud.core.controls import session_snapshot
from bud.tui.commands import handle_slash_command
from bud.tui.keymap import build_key_bindings
from bud.tui.state import InputMode, StateStore
from bud.tui.terminal import SynchronizedOutput, get_terminal_size
from bud.tui.views.approval import ApprovalView
from bud.tui.views.footer import FooterView
from bud.tui.views.message import MessageView
from bud.tui.views.question import QuestionView
from bud.tui.views.tool import ToolsContainerView

logger = logging.getLogger(__name__)


class TUIApp:
    """Bud Terminal User Interface coordinator."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        model_id: str = "default",
        scope: str = "",
    ) -> None:
        self.client: httpx.AsyncClient = client
        self.session_id: str = session_id
        self.store: StateStore = StateStore(session_id, model_id=model_id, scope=scope)

        # Views
        self.streaming_view: MessageView = MessageView("bud", "", self.store)
        self.tools_view: ToolsContainerView = ToolsContainerView(self.store)
        self.question_view: QuestionView = QuestionView(self.store)
        self.footer_view: FooterView = FooterView(self.store)

        # Input buffer
        self.input_buffer: Buffer = Buffer(
            multiline=False,
            accept_handler=self._on_prompt_accept,
        )

        # Reactive subscription
        self.store.subscribe(self._on_state_change)

        # Build prompt-toolkit application
        self.pt_app: Application = self._build_pt_app()

        # Background tasks
        self._event_task: asyncio.Task[None] | None = None
        self._running: bool = False

    def _on_state_change(self) -> None:
        """Called whenever reactive StateStore updates."""
        # Sync streaming text
        if self.store.status in ("thinking", "streaming") or self.store.streaming_text:
            if not self.streaming_view._is_streaming:
                self.streaming_view.start_streaming()
            self.streaming_view.set_text(self.store.streaming_text)
        else:
            if self.streaming_view._is_streaming:
                self.streaming_view.finish_streaming(self.store.streaming_text)

        self.streaming_view.mark_dirty()
        self.tools_view.mark_dirty()
        self.question_view.mark_dirty()
        self.footer_view.mark_dirty()

        # Invalidate prompt-toolkit layout to trigger redraw
        if self.pt_app.is_running:
            self.pt_app.invalidate()

    def _render_dynamic_content(self) -> FormattedText | ANSI:
        """Render the dynamic content window based on the active mode and status."""
        cols, _ = get_terminal_size()
        lines: list[str] = []

        if self.store.mode == InputMode.APPROVAL:
            approval = self.store.current_approval()
            if approval:
                view = ApprovalView(approval, self.store)
                lines.extend(view.render(cols))

        elif self.store.mode == InputMode.QUESTION:
            lines.extend(self.question_view.render(cols))

        else:
            # PROMPT mode: show active tool cards and streaming assistant response
            tool_lines = self.tools_view.render(cols)
            if tool_lines:
                lines.extend(tool_lines)

            if self.store.status in ("thinking", "streaming") or self.store.streaming_text:
                stream_lines = self.streaming_view.render(cols)
                if stream_lines:
                    lines.extend(stream_lines)

        if not lines:
            return FormattedText([])
        return ANSI("\n".join(lines))

    def _render_footer(self) -> ANSI:
        """Render the one-line footer."""
        cols, _ = get_terminal_size()
        lines = self.footer_view.render(cols)
        return ANSI(lines[0] if lines else "")

    def _get_prompt_prefix(self) -> FormattedText:
        """Return the prompt symbol prefix."""
        if self.store.mode != InputMode.PROMPT:
            return FormattedText([])
        return FormattedText([("class:prompt", "you › ")])

    def _build_pt_app(self) -> Application:
        """Assemble the prompt-toolkit inline Application with SynchronizedOutput."""
        kb = build_key_bindings(self.store, self)

        # Dynamic view control (tools, streaming, approvals, questions)
        dynamic_control = FormattedTextControl(
            text=self._render_dynamic_content,
            focusable=False,
            show_cursor=False,
        )

        def get_dynamic_height() -> Dimension:
            # Measure dynamic lines count
            content = self._render_dynamic_content()
            raw_text = (
                content.value if isinstance(content, ANSI) else "".join(t[1] for t in content)
            )
            line_count = len(raw_text.splitlines()) if raw_text else 0
            if line_count == 0:
                return Dimension(min=0, max=0, preferred=0)
            return Dimension(min=1, max=line_count, preferred=line_count)

        dynamic_window = Window(
            content=dynamic_control,
            height=get_dynamic_height,
            dont_extend_height=True,
        )

        # Input buffer control
        input_control = BufferControl(
            buffer=self.input_buffer,
            key_bindings=kb,
            focusable=True,
        )

        def get_input_height() -> Dimension:
            if self.store.mode != InputMode.PROMPT:
                return Dimension(min=0, max=0, preferred=0)
            return Dimension(min=1, max=1, preferred=1)

        input_window = Window(
            content=input_control,
            height=get_input_height,
            dont_extend_height=True,
            get_line_prefix=lambda line_no, wrap_count: self._get_prompt_prefix(),
        )

        # Footer window
        footer_control = FormattedTextControl(
            text=self._render_footer,
            focusable=False,
            show_cursor=False,
        )
        footer_window = Window(
            content=footer_control,
            height=Dimension.exact(1),
            dont_extend_height=True,
        )

        root_layout = Layout(
            HSplit(
                [
                    dynamic_window,
                    input_window,
                    footer_window,
                ]
            ),
            focused_element=input_window,
        )

        # Use synchronized output to avoid tearing
        raw_output = create_output()
        sync_output = SynchronizedOutput(raw_output)

        return Application(
            layout=root_layout,
            key_bindings=kb,
            style=self.store.theme.to_prompt_toolkit_style(),
            output=sync_output,
            full_screen=False,
            refresh_interval=0.05,  # 50ms refresh for smooth spinners and streaming
        )

    def commit_scrollback(self, lines: str | list[str]) -> None:
        """Commit finalized text lines permanently into native terminal scrollback."""
        if isinstance(lines, str):
            lines = lines.splitlines()
        for line in lines:
            print_formatted_text(ANSI(line), output=self.pt_app.output)

    def clear_screen(self) -> None:
        """Clear screen scrollback."""
        if self.pt_app.output:
            self.pt_app.output.erase_screen()
            self.pt_app.output.cursor_goto(0, 0)
        self.pt_app.invalidate()

    def _on_prompt_accept(self, buffer: Buffer) -> bool:
        """Handle user prompt submission."""
        text = buffer.text.strip()
        if not text:
            return False

        # Reset buffer immediately
        buffer.text = ""
        asyncio.create_task(self._process_user_input(text))
        return True

    async def _process_user_input(self, text: str) -> None:
        """Process submitted prompt text (slash commands or session message)."""
        if text.startswith("/"):
            result = await handle_slash_command(self.client, self.store, self, text)
            if result == "__CLEAR__":
                self.clear_screen()
            elif result:
                self.commit_scrollback(result)
            return

        # Add to prompt history
        self.store.add_prompt_history(text)

        # Commit user's message line to scrollback immediately
        user_line = f"{self.store.theme.format_primary('you ›', bold=True)} {text}"
        self.commit_scrollback(user_line)

        # Send to server
        try:
            source = {"channel": "terminal", "client_id": "terminal"}
            res = await self.client.post(
                f"/sessions/{self.session_id}/messages",
                json={"text": text, "message_id": uuid4().hex, "origin": source},
            )
            res.raise_for_status()
            if res.json().get("duplicate"):
                self.commit_scrollback(self.store.theme.format_muted("[duplicate message]"))
        except httpx.HTTPStatusError as exc:
            err_tag = self.store.theme.format_error("[HTTP Error]")
            self.commit_scrollback(f"{err_tag} {exc.response.status_code}: {exc.response.text}")
        except Exception as exc:
            self.commit_scrollback(f"{self.store.theme.format_error('[Request Failed]')} {exc}")

    async def handle_approval_decision(self, approved: bool) -> None:
        """Submit 1-key approval decision (approve or deny) to server."""
        approval = self.store.current_approval()
        if not approval:
            return

        app_id = approval.reference or approval.approval_id
        source = {"channel": "terminal", "client_id": "terminal"}

        try:
            res = await self.client.post(
                f"/sessions/{self.session_id}/approvals/{app_id}",
                json={"approved": approved, "origin": source},
            )
            res.raise_for_status()
            label = "approved" if approved else "denied"
            tag = (
                self.store.theme.format_success(f"[{label}]", bold=True)
                if approved
                else self.store.theme.format_error(f"[{label}]", bold=True)
            )
            self.commit_scrollback(f"{tag} {app_id} ({approval.tool})")
        except Exception as exc:
            self.commit_scrollback(f"{self.store.theme.format_error('[Approval Failed]')} {exc}")

    def handle_approval_cancel(self) -> None:
        """Dismiss active approval prompt temporarily back to PROMPT mode."""
        self.store.set_mode(InputMode.PROMPT)

    async def stop_run(self) -> None:
        """Interrupt current active run on server."""
        try:
            res = await self.client.post(f"/sessions/{self.session_id}/stop")
            res.raise_for_status()
            data = res.json()
            if data.get("stop_requested"):
                self.commit_scrollback(
                    self.store.theme.format_warning(
                        "■ stop requested; queue paused; authorized actions may finish",
                        bold=True,
                    )
                )
            else:
                self.commit_scrollback(self.store.theme.format_muted("■ no active run to stop"))
        except Exception as exc:
            self.commit_scrollback(f"{self.store.theme.format_error('[Stop Failed]')} {exc}")

    async def _sse_reader(self) -> None:
        """Background task following SSE events from the Bud server."""
        last_seq = 0
        while self._running:
            try:
                state = await session_snapshot(self.client, self.session_id)
                watermark = state["last_seq"]
                pending_ids = {item["approval_id"] for item in state["pending_approvals"]}

                async with self.client.stream(
                    "GET",
                    f"/sessions/{self.session_id}/events",
                    headers={"Last-Event-ID": str(last_seq)},
                    timeout=httpx.Timeout(5.0, read=None),
                ) as response:
                    if response.is_error:
                        await response.aread()
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if not self._running:
                            break
                        if line.startswith("data: "):
                            raw_event = json.loads(line.removeprefix("data: "))
                            last_seq = raw_event.get("seq", last_seq)

                            # Handle event in state store
                            committed = self.store.handle_event(
                                raw_event,
                                watermark=watermark,
                                pending_ids=pending_ids,
                            )
                            if committed:
                                self.commit_scrollback(committed)

                            # If run finished, commit the completed assistant reply to scrollback
                            if raw_event.get("type") in ("run.completed", "run.waiting"):
                                from bud.core.projection import reply_text

                                full_reply = reply_text(
                                    raw_event.get("data", {}).get("messages", [])
                                )
                                if full_reply:
                                    reply_view = MessageView("bud", full_reply, self.store)
                                    cols, _ = get_terminal_size()
                                    self.commit_scrollback(reply_view.render(cols))

                                # Clear tool disclosures and streaming view
                                self.store.tools.clear()
                                self.streaming_view.set_text("")
                                self.pt_app.invalidate()

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 404):
                    err_tag = self.store.theme.format_error("[Session Error]")
                    self.commit_scrollback(f"{err_tag} HTTP {exc.response.status_code}")
                    return
                await asyncio.sleep(1.0)
            except (TimeoutError, httpx.RequestError):
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("SSE reader exception: %s", exc)
                await asyncio.sleep(1.0)

    async def run(self) -> None:
        """Run the TUI event loop."""
        self._running = True

        # Load initial session snapshot
        try:
            snapshot = await session_snapshot(self.client, self.session_id)
            self.store.model_id = self.client.headers.get("X-Bud-Model", self.store.model_id)

            # Render initial welcome & historical transcript
            th = self.store.theme
            welcome = (
                f"{th.format_accent('bud', bold=True)} "
                f"{th.format_dim('session:')} {th.format_secondary(self.session_id)} "
                f"{th.format_dim('•')} {th.format_dim('/help for commands • Ctrl-D to exit')}"
            )
            self.commit_scrollback(welcome)

            for turn in snapshot.get("transcript", []):
                role = turn.get("role", "bud")
                text = turn.get("text", "")
                view = MessageView(role, text, self.store)
                cols, _ = get_terminal_size()
                self.commit_scrollback(view.render(cols))

            # Initial approvals if any
            for app_data in snapshot.get("pending_approvals", []):
                self.store.handle_event({"type": "approval.requested", "data": app_data, "seq": 0})

        except Exception as exc:
            self.commit_scrollback(f"[Snapshot Warning] Could not load initial state: {exc}")

        # Start SSE event follower task
        self._event_task = asyncio.create_task(self._sse_reader())

        try:
            await self.pt_app.run_async()
        finally:
            self._running = False
            if self._event_task:
                self._event_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._event_task


async def run_tui(base_url: str, session_id: str | None, *, new: bool = False) -> None:
    """Entry point for running the Bud TUI."""
    from bud.core.client import owner_client, select_model

    async with owner_client(base_url) as client:
        # Runtime handshake
        res = await client.get("/runtime")
        res.raise_for_status()
        scope = res.json().get("scope", "")
        client.headers["X-Bud-Scope"] = scope

        chosen_model = "default"
        if session_id is None:
            # Interactive model picker on start if new session
            chosen_model = await select_model(client) or "default"
            if new:
                res = await client.post(
                    "/sessions", json={"session_id": uuid4().hex, "model": chosen_model}
                )
            else:
                res = await client.post("/sessions/primary", json={"model": chosen_model})
            res.raise_for_status()
            session_id = res.json()["session_id"]
        else:
            await session_snapshot(client, session_id)

        assert session_id is not None
        client.headers["X-Bud-Model"] = chosen_model

        app = TUIApp(client, session_id, model_id=chosen_model, scope=scope)
        await app.run()
