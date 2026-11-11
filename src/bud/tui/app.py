"""Main TUI coordinator integrating SSE events, differential painting, and prompt-toolkit input."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from prompt_toolkit import Application, print_formatted_text
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import Float, FloatContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import create_output
from prompt_toolkit.patch_stdout import patch_stdout

from bud.core.controls import session_snapshot
from bud.tui.commands import COMMANDS, handle_slash_command
from bud.tui.keymap import build_key_bindings
from bud.tui.state import InputMode, StateStore
from bud.tui.terminal import SynchronizedOutput, get_terminal_size
from bud.tui.views.approval import ApprovalView, friendly_action_title
from bud.tui.views.box import render_user_turn
from bud.tui.views.footer import FooterView
from bud.tui.views.message import MessageView
from bud.tui.views.question import QuestionView
from bud.tui.views.tool import ToolsContainerView

logger = logging.getLogger(__name__)


def _context_label() -> str:
    """Return 'name:branch' for the working directory, like 'bud:master'."""
    cwd = Path.cwd()
    git = cwd / ".git"
    head = git / "HEAD"
    branch = ""
    try:
        if git.is_file():  # linked worktree: .git is a pointer, not a directory
            head = Path(git.read_text(encoding="utf-8").strip().removeprefix("gitdir: ")) / "HEAD"
        raw = head.read_text(encoding="utf-8").strip()
        branch = raw.removeprefix("ref: refs/heads/")
    except OSError:
        branch = ""
    return f"{cwd.name}:{branch}" if branch else cwd.name


class SlashCommandCompleter(Completer):
    """Completes the first word of a slash command from the single command registry."""

    def get_completions(self, document: Any, complete_event: Any) -> Any:
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for cmd, _usage, desc in COMMANDS:
            if cmd.startswith(text):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=desc,
                )


class TUIApp:
    """Bud Terminal User Interface coordinator."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        model_id: str = "default",
        scope: str = "",
        input: Any = None,
        output: Any = None,
    ) -> None:
        self.client: httpx.AsyncClient = client
        self.session_id: str = session_id
        self.store: StateStore = StateStore(session_id, model_id=model_id, scope=scope)
        self._context_label = _context_label()
        self._custom_input: Any = input
        self._custom_output: Any = output

        # Views
        self.streaming_view: MessageView = MessageView("bud", "", self.store)
        self.tools_view: ToolsContainerView = ToolsContainerView(self.store)
        self.question_view: QuestionView = QuestionView(self.store)
        self.footer_view: FooterView = FooterView(self.store)

        # Input buffer: multiline enabled for \+Enter and Alt+Enter continuations
        self.input_buffer: Buffer = Buffer(
            multiline=True,
            accept_handler=self._on_prompt_accept,
            completer=SlashCommandCompleter(),
            complete_while_typing=True,
        )

        # Reactive subscription
        self.store.subscribe(self._on_state_change)

        # Composer containers are cached per (mode, border style); see _render_framed_composer.
        self._composer_cache: dict[tuple[InputMode, str], Any] = {}

        # Build prompt-toolkit application
        self.pt_app: Application = self._build_pt_app()

        # Background tasks
        self._event_task: asyncio.Task[None] | None = None
        self._running: bool = False
        self._background_tasks: set[asyncio.Task[Any]] = set()

        # Dynamic layout cache
        self._dynamic_dirty: bool = True
        self._cached_dynamic_cols: int = 0
        self._cached_dynamic_content: FormattedText | ANSI = FormattedText([])
        self._cached_dynamic_height: int = 0
        self._last_stream_render: float = 0.0

    def spawn_task(self, coro: Any) -> asyncio.Task[Any]:
        """Spawn a background task with strong reference tracking to prevent GC drops."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _on_state_change(self) -> None:
        """Called whenever reactive StateStore updates."""
        # Invalidate dynamic content cache
        self._dynamic_dirty = True
        event = self.store.last_event

        # Sync streaming text. Markdown re-parse of the full reply is expensive, so
        # while tokens stream in, re-render at most ~8fps; always render on other
        # events and the final text when the run ends.
        streaming_active = (
            self.store.status in ("thinking", "streaming") or self.store.streaming_text
        )
        if streaming_active:
            if not self.streaming_view._is_streaming:
                self.streaming_view.start_streaming()
                self.streaming_view.set_text(self.store.streaming_text)
                self._last_stream_render = time.monotonic()
            elif event == "text.delta":
                now = time.monotonic()
                if now - self._last_stream_render >= 0.12:
                    self._last_stream_render = now
                    self.streaming_view.set_text(self.store.streaming_text)
            else:
                self.streaming_view.set_text(self.store.streaming_text)
        elif self.streaming_view._is_streaming:
            self.streaming_view.finish_streaming(self.store.streaming_text)

        # Scope dirty-marking to the views the event can affect. The views do not
        # self-subscribe; this is the single invalidation point.
        if event.startswith("tool.") or event in (
            "run.started",
            "run.completed",
            "run.waiting",
            "run.failed",
            "run.cancelled",
            "run.recovered",
        ):
            self.tools_view.mark_dirty()
        if event in ("ui.theme", "ui.border"):
            # Restyle invalidates every cached render.
            self.tools_view.mark_dirty()
            self.question_view.mark_dirty()
            self.streaming_view.mark_dirty()
        if self.store.mode == InputMode.QUESTION:
            self.question_view.mark_dirty()
        self.footer_view.mark_dirty()

        # Invalidate prompt-toolkit layout to trigger redraw
        if self.pt_app.is_running:
            self.pt_app.invalidate()

    def _render_dynamic_content(self) -> FormattedText | ANSI:
        """Render the dynamic content window based on the active mode and status."""
        cols, _ = get_terminal_size()
        if not self._dynamic_dirty and self._cached_dynamic_cols == cols:
            return self._cached_dynamic_content

        self._cached_dynamic_cols = cols
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

            if (
                not lines
                and not self.store.turns
                and self.store.status == "idle"
                and not self.store.pending_approvals
                and not self.store.queued_messages
            ):
                lines.extend(self._render_wordmark(cols))

        if not lines:
            self._cached_dynamic_content = FormattedText([])
            self._cached_dynamic_height = 0
        else:
            self._cached_dynamic_content = ANSI("\n".join(lines))
            self._cached_dynamic_height = len(lines)

        self._dynamic_dirty = False
        return self._cached_dynamic_content

    def _render_footer(self) -> ANSI:
        """Render the one-line footer."""
        cols, _ = get_terminal_size()
        lines = self.footer_view.render(cols)
        return ANSI(lines[0] if lines else "")

    def _render_wordmark(self, cols: int) -> list[str]:
        """Idle empty-session state: centered wordmark, like modern TUI agents."""
        th = self.store.theme

        def centered(text: str) -> str:
            pad = max(0, (cols - len(text)) // 2)
            return " " * pad + text

        return [
            "",
            centered(th.format_accent("b  u  d", bold=True)),
            centered(th.format_dim("/help for commands · ask anything")),
            "",
        ]

    def _get_prompt_prefix(self, line_no: int = 0) -> FormattedText:
        """Return the per-line prompt prefix; framed modes start text cleanly."""
        if self.store.border_style == "left":
            return FormattedText([("class:input.prompt", "› " if line_no == 0 else "  ")])
        return FormattedText([])

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
            # Reuses cached height from _render_dynamic_content to eliminate duplicate rendering
            self._render_dynamic_content()
            line_count = self._cached_dynamic_height
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
            line_count = len(self.input_buffer.document.lines)
            h = max(1, min(line_count, 8))
            return Dimension(min=1, max=h, preferred=h)

        input_window = Window(
            content=input_control,
            height=get_input_height,
            dont_extend_height=True,
            get_line_prefix=lambda line_no, wrap_count: self._get_prompt_prefix(line_no),
        )

        def _render_framed_composer() -> Any:
            # The frame only depends on mode and border style; rebuild per frame
            # would churn ~16 prompt-toolkit objects at event rate for nothing.
            key = (self.store.mode, self.store.border_style)
            cached = self._composer_cache.get(key)
            if cached is not None:
                return cached

            if self.store.mode != InputMode.PROMPT:
                container: Any = Window(height=Dimension.exact(0), dont_extend_height=True)
                self._composer_cache[key] = container
                return container

            style = self.store.border_style

            def get_meta_text() -> FormattedText:
                model_name = self.store.model_id or "default"
                return FormattedText(
                    [
                        ("class:composer.badge", "bud"),
                        ("class:composer.meta", f" · {model_name} · {self._context_label}"),
                    ]
                )

            meta_control = FormattedTextControl(
                text=get_meta_text,
                focusable=False,
                show_cursor=False,
            )
            meta_window = Window(
                content=meta_control,
                height=Dimension.exact(1),
                dont_extend_height=True,
            )

            if style == "left":
                # Frameless: rail prompt prefix carries the affordance (see
                # _get_prompt_prefix), so the composer is bare text + one meta line.
                container = HSplit(
                    [
                        Window(width=2, height=1, char=" "),
                        input_window,
                        VSplit(
                            [
                                Window(width=4, height=1, char=" "),
                                meta_window,
                                Window(width=2, height=1, char=" "),
                            ],
                            height=1,
                        ),
                    ]
                )
                self._composer_cache[key] = container
                return container

            if style == "square":
                tl, tr, bl, br, h, v = "┌", "┐", "└", "┘", "─", "│"
            else:
                tl, tr, bl, br, h, v = "╭", "╮", "╰", "╯", "─", "│"

            fill = functools.partial(Window, style="class:card.border")
            top = VSplit(
                [
                    fill(width=2, height=1, char=" "),
                    fill(width=1, height=1, char=tl),
                    fill(char=h),
                    fill(width=1, height=1, char=tr),
                    fill(width=2, height=1, char=" "),
                ],
                height=1,
            )

            mid = VSplit(
                [
                    fill(width=2, char=" "),
                    fill(width=1, char=v),
                    Window(width=1, char=" "),
                    input_window,
                    Window(width=1, char=" "),
                    fill(width=1, char=v),
                    fill(width=2, char=" "),
                ]
            )

            meta_row = VSplit(
                [
                    fill(width=2, height=1, char=" "),
                    fill(width=1, height=1, char=v),
                    Window(width=1, height=1, char=" "),
                    meta_window,
                    Window(width=1, height=1, char=" "),
                    fill(width=1, height=1, char=v),
                    fill(width=2, height=1, char=" "),
                ],
                height=1,
            )

            bot = VSplit(
                [
                    fill(width=2, height=1, char=" "),
                    fill(width=1, height=1, char=bl),
                    fill(char=h),
                    fill(width=1, height=1, char=br),
                    fill(width=2, height=1, char=" "),
                ],
                height=1,
            )

            container = HSplit([top, mid, meta_row, bot])
            self._composer_cache[key] = container
            return container

        framed_composer = DynamicContainer(_render_framed_composer)
        self._render_composer = _render_framed_composer

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
            FloatContainer(
                content=HSplit(
                    [
                        dynamic_window,
                        framed_composer,
                        footer_window,
                    ]
                ),
                floats=[
                    Float(
                        xcursor=True,
                        ycursor=True,
                        content=CompletionsMenu(max_height=8, scroll_offset=1),
                    )
                ],
            ),
            focused_element=input_window,
        )

        # Use synchronized output to avoid tearing
        raw_output = self._custom_output or create_output()
        sync_output = SynchronizedOutput(raw_output)

        app_kwargs: dict[str, Any] = {
            "layout": root_layout,
            "key_bindings": kb,
            "style": self.store.theme.to_prompt_toolkit_style(),
            "output": sync_output,
            "full_screen": False,
        }
        if self._custom_input is not None:
            app_kwargs["input"] = self._custom_input

        app = Application(**app_kwargs)
        app.timeoutlen = 0.1  # Fast escape key resolution (100ms) for snappy Esc response
        return app

    def commit_scrollback(self, lines: str | list[str]) -> None:
        """Commit finalized text lines permanently into native terminal scrollback."""
        text = "\n".join(lines) if isinstance(lines, list) else lines
        if text:
            print_formatted_text(ANSI(text))

    def commit_blank_line(self) -> None:
        """Commit one blank line to scrollback (spacing between committed blocks)."""
        print_formatted_text("")

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
        self.spawn_task(self._process_user_input(text))
        return True

    async def _process_user_input(self, text: str) -> None:
        """Process submitted prompt text (slash commands or session message)."""
        # If an approval is pending but the user dismissed the card with Esc,
        # reclaim the keyboard so y/n stays reachable.
        pending = self.store.pending_approvals and self.store.mode == InputMode.PROMPT
        if pending and not self.store.represent_question():
            self.store.set_mode(InputMode.APPROVAL)
        if text.startswith("/"):
            try:
                result = await handle_slash_command(self.client, self.store, self, text)
            except httpx.HTTPStatusError as exc:
                self.commit_scrollback(
                    f"{self.store.theme.format_error('[command failed]')} "
                    f"{exc.response.status_code}: {exc.response.text[:200]}"
                )
                return
            except httpx.RequestError as exc:
                self.commit_scrollback(f"{self.store.theme.format_error('[command failed]')} {exc}")
                return
            if result == "__CLEAR__":
                self.clear_screen()
            elif result:
                self.commit_scrollback(result)
            return

        # Free-text answer to a pending question: posted to the approval, not sent as chat.
        if self.store.awaiting_free_text and self.store.awaiting_question_id:
            approval_id = self.store.awaiting_question_id
            self.store.awaiting_free_text = False
            self.store.awaiting_question_id = None
            self.store.add_prompt_history(text)
            cols, _ = get_terminal_size()
            self.commit_scrollback(render_user_turn(text, width=cols, theme=self.store.theme))
            self.commit_blank_line()
            self.spawn_task(self.submit_question_approval(approval_id, text))
            return

        # Add to prompt history
        self.store.add_prompt_history(text)

        # Commit user's message turn to scrollback immediately
        cols, _ = get_terminal_size()
        self.commit_scrollback(render_user_turn(text, width=cols, theme=self.store.theme))
        self.commit_blank_line()

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

    async def submit_question_approval(self, approval_id: str, answer: str) -> None:
        """POST the owner's answer to a pending ask_user question."""
        try:
            res = await self.client.post(
                f"/sessions/{self.session_id}/approvals/{approval_id}/answer",
                json={"text": answer, "origin": {"channel": "terminal", "client_id": "terminal"}},
            )
            res.raise_for_status()
            if not res.json().get("answered", True):
                self.commit_scrollback(self.store.theme.format_muted("[question already answered]"))
        except httpx.HTTPStatusError as exc:
            self.commit_scrollback(
                f"{self.store.theme.format_error('[answer failed]')} "
                f"{exc.response.status_code}: {exc.response.text[:200]}"
            )
        except httpx.RequestError as exc:
            self.commit_scrollback(f"{self.store.theme.format_error('[answer failed]')} {exc}")

    async def handle_approval_decision(self, approved: bool) -> None:
        """Submit 1-key approval decision (approve or deny) to server."""
        approval = self.store.current_approval()
        if not approval:
            return

        app_id = approval.approval_id
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
            action_desc = friendly_action_title(approval.tool, approval.args)
            self.commit_scrollback(f"{tag} {action_desc}")
            # Clear pending approval immediately in local state
            self.store.pending_approvals = [
                a for a in self.store.pending_approvals if a.approval_id != app_id
            ]
            if not self.store.pending_approvals and self.store.mode == InputMode.APPROVAL:
                self.store.set_mode(InputMode.PROMPT)
            self.store.notify()
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

                            # If run finished, commit tools and assistant reply to scrollback
                            if raw_event.get("type") in (
                                "run.completed",
                                "run.waiting",
                                "run.failed",
                                "run.cancelled",
                            ):
                                from bud.core.projection import reply_text

                                cols, _ = get_terminal_size()

                                # Commit completed tools summary card to scrollback before clearing
                                if self.store.tools:
                                    summary_card = self.tools_view.render(cols)
                                    if summary_card:
                                        self.commit_scrollback(summary_card)
                                    self.store.tools.clear()
                                    self.store.notify()  # drop the stale card from the live pane

                                full_reply = reply_text(
                                    raw_event.get("data", {}).get("messages", [])
                                )
                                if (
                                    not full_reply
                                    and self.store.turns
                                    and self.store.turns[-1].role == "bud"
                                ):
                                    full_reply = self.store.turns[-1].text

                                if full_reply:
                                    reply_view = MessageView("bud", full_reply, self.store)
                                    self.commit_scrollback(reply_view.render(cols))
                                    self.commit_blank_line()

                                # Turn stats under the reply, like modern agent TUIs.
                                if self.store.last_stats_line:
                                    self.commit_scrollback(
                                        self.store.theme.format_dim(self.store.last_stats_line)
                                    )
                                    self.store.last_stats_line = ""
                                    self.commit_blank_line()

                                # Clear streaming view
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
                f"{th.format_dim('·')} {th.format_dim('/help for commands · Ctrl-D to exit')}"
            )
            self.commit_scrollback(welcome)
            self.commit_blank_line()

            cols, _ = get_terminal_size()
            for turn in snapshot.get("transcript", []):
                role = {"user": "you", "assistant": "bud"}.get(turn.get("role", ""), "bud")
                text = turn.get("text", "")
                view = MessageView(role, text, self.store)
                self.commit_scrollback(view.render(cols))
                self.commit_blank_line()

            # Initial approvals if any
            for app_data in snapshot.get("pending_approvals", []):
                self.store.handle_event({"type": "approval.requested", "data": app_data, "seq": 0})

        except Exception as exc:
            self.commit_scrollback(f"[Snapshot Warning] Could not load initial state: {exc}")

        # Start SSE event follower task
        self._event_task = asyncio.create_task(self._sse_reader())

        try:
            with patch_stdout():
                await self.pt_app.run_async()
        finally:
            self._running = False
            for task in list(self._background_tasks):
                task.cancel()
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
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
