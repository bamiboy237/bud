"""Reactive StateStore managing session, turns, streaming text, tools, approvals, and modes.

The StateStore is the single source of truth for all TUI components. It notifies
subscribers when state updates occur, allowing declarative rendering pipelines.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from bud.tui.themes import DEFAULT_THEME_NAME, Theme, get_theme


class InputMode(StrEnum):
    PROMPT = "PROMPT"
    APPROVAL = "APPROVAL"
    QUESTION = "QUESTION"


@dataclass
class ToolState:
    call_id: str
    tool_name: str
    args: dict[str, Any]
    status: Literal["running", "completed", "failed"] = "running"
    result: str | None = None
    start_time: float = field(default_factory=time.time)
    duration_ms: float | None = None
    expanded: bool = False

    def finish(self, result: str, failed: bool = False) -> None:
        self.status = "failed" if failed else "completed"
        self.result = result
        self.duration_ms = (time.time() - self.start_time) * 1000.0


@dataclass
class ApprovalState:
    approval_id: str
    tool: str
    args: dict[str, Any]
    reference: str | None = None
    task_id: int | None = None


@dataclass
class Turn:
    role: str  # "you", "bud", "system"
    text: str
    timestamp: float = field(default_factory=time.time)


class StateStore:
    """Reactive store for the Bud TUI."""

    def __init__(self, session_id: str, model_id: str = "default", scope: str = "") -> None:
        self.session_id: str = session_id
        self.model_id: str = model_id
        self.scope: str = scope
        self.status: str = "idle"  # "idle", "thinking", "running", "paused", "waiting_approval"
        self.queue_paused: bool = False
        self.mode: InputMode = InputMode.PROMPT

        # Turns and message history
        self.turns: list[Turn] = []
        self.queued_messages: list[dict[str, Any]] = []

        # Run and live streaming state
        self.active_run_id: str | None = None
        self.streaming_text: str = ""
        self.seen_requests: set[str] = set()
        self.turn_start_time: float | None = None
        self.first_token_time: float | None = None
        self.last_latency_ms: float = 0.0
        self.input_tokens: int = 0
        self.output_tokens: int = 0

        # Tools execution
        self.tools: dict[str, ToolState] = {}
        self.tools_expanded_global: bool = False

        # Approvals
        self.pending_approvals: list[ApprovalState] = []

        # Questions / Choice Selector
        self.question_title: str = ""
        self.question_choices: list[str] = []
        self.question_selected_index: int = 0
        self.question_future: asyncio.Future[str | None] | None = None

        # Theme
        self.theme_name: str = DEFAULT_THEME_NAME
        self.theme: Theme = get_theme(DEFAULT_THEME_NAME)

        # Prompt input history
        self.prompt_history: list[str] = []
        self.history_index: int = -1

        # Subscribers
        self._listeners: list[Callable[[], None]] = []

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to state changes. Returns an unsubscribe callable."""
        self._listeners.append(callback)

        def unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsubscribe

    def notify(self) -> None:
        """Notify all subscribers that state has changed."""
        for listener in list(self._listeners):
            with contextlib.suppress(Exception):
                listener()

    def set_mode(self, mode: InputMode) -> None:
        if self.mode != mode:
            self.mode = mode
            self.notify()

    def set_theme(self, name: str) -> bool:
        new_theme = get_theme(name)
        self.theme_name = new_theme.name
        self.theme = new_theme
        self.notify()
        return True

    def set_model(self, model_id: str) -> None:
        self.model_id = model_id
        self.notify()

    def add_prompt_history(self, prompt: str) -> None:
        clean = prompt.strip()
        if clean and (not self.prompt_history or self.prompt_history[-1] != clean):
            self.prompt_history.append(clean)
        self.history_index = len(self.prompt_history)

    def recall_prev_prompt(self) -> str | None:
        """Recall previous prompt from history for Esc+Up."""
        if not self.prompt_history:
            return None
        if self.history_index > 0:
            self.history_index -= 1
        elif self.history_index == -1:
            self.history_index = max(0, len(self.prompt_history) - 1)
        return self.prompt_history[self.history_index]

    def recall_next_prompt(self) -> str | None:
        """Recall next prompt from history."""
        if not self.prompt_history:
            return None
        if self.history_index < len(self.prompt_history) - 1:
            self.history_index += 1
            return self.prompt_history[self.history_index]
        self.history_index = len(self.prompt_history)
        return ""

    def toggle_tool_disclosure(self) -> None:
        """Toggle expand/collapse of tool disclosure cards."""
        self.tools_expanded_global = not self.tools_expanded_global
        for tool in self.tools.values():
            tool.expanded = self.tools_expanded_global
        self.notify()

    def current_approval(self) -> ApprovalState | None:
        """Return the currently focused pending approval if any."""
        return self.pending_approvals[0] if self.pending_approvals else None

    # Question mode helpers
    def start_question(
        self,
        title: str,
        choices: list[str],
        default_index: int = 0,
        future: asyncio.Future[str | None] | None = None,
    ) -> None:
        self.question_title = title
        self.question_choices = choices
        self.question_selected_index = max(0, min(default_index, len(choices) - 1))
        self.question_future = future
        self.set_mode(InputMode.QUESTION)

    def select_next_choice(self) -> None:
        if self.question_choices:
            self.question_selected_index = (self.question_selected_index + 1) % len(
                self.question_choices
            )
            self.notify()

    def select_prev_choice(self) -> None:
        if self.question_choices:
            self.question_selected_index = (self.question_selected_index - 1) % len(
                self.question_choices
            )
            self.notify()

    def confirm_question(self) -> str | None:
        choice = (
            self.question_choices[self.question_selected_index] if self.question_choices else None
        )
        if self.question_future and not self.question_future.done():
            self.question_future.set_result(choice)
        self.question_future = None
        self.set_mode(InputMode.PROMPT)
        return choice

    def cancel_question(self) -> None:
        if self.question_future and not self.question_future.done():
            self.question_future.set_result(None)
        self.question_future = None
        self.set_mode(InputMode.PROMPT)

    # SSE Event Processing
    def handle_event(
        self,
        event: dict[str, Any],
        *,
        watermark: int = 0,
        pending_ids: set[str] | None = None,
    ) -> str | None:
        """Process one SSE event and update state.

        Returns an optional completed line/summary to immediately commit to scrollback.
        """
        event_type = event.get("type", "")
        data = event.get("data", {})
        seq = event.get("seq", 0)

        match event_type:
            case "user.message":
                text = data.get("text", "")
                self.turns.append(Turn(role="you", text=text))
                self.notify()
                return f"{self.theme.format_primary('you ›', bold=True)} {text}"

            case "message.queued":
                self.queued_messages.append(data)
                self.notify()
                return (
                    f"{self.theme.format_muted('◷ queued')} "
                    f"{data.get('message_id')}: {data.get('text')}"
                )

            case "message.edited":
                mid = data.get("message_id")
                for msg in self.queued_messages:
                    if msg.get("message_id") == mid:
                        msg["text"] = data.get("text")
                self.notify()
                return f"{self.theme.format_muted('✎ edited')} {mid}: {data.get('text')}"

            case "message.cancelled":
                mid = data.get("message_id")
                self.queued_messages = [
                    m for m in self.queued_messages if m.get("message_id") != mid
                ]
                self.notify()
                return f"{self.theme.format_muted('✕ cancelled message')} {mid}"

            case "queue.paused":
                self.queue_paused = True
                self.notify()
                return self.theme.format_warning("⏸ queue paused", bold=True)

            case "queue.resumed":
                self.queue_paused = False
                self.notify()
                return self.theme.format_success("▶ queue resumed", bold=True)

            case "run.started":
                self.active_run_id = data.get("run_id")
                self.status = "thinking"
                self.turn_start_time = time.time()
                self.first_token_time = None
                self.streaming_text = ""
                self.notify()
                return None

            case "run.recovered":
                self.status = "thinking"
                self.notify()
                return self.theme.format_info("↻ recovering interrupted work", bold=True)

            case "text.reset":
                req = data.get("request", "")
                if req in self.seen_requests:
                    self.streaming_text = ""
                self.seen_requests.add(req)
                self.notify()
                return None

            case "text.delta":
                chunk = data.get("text", "")
                now = time.time()
                if self.first_token_time is None and self.turn_start_time is not None:
                    self.first_token_time = now
                    self.last_latency_ms = (now - self.turn_start_time) * 1000.0
                self.streaming_text += chunk
                self.output_tokens += max(1, len(chunk.split()))
                self.status = "streaming"
                self.notify()
                return None

            case "tool.call":
                call_id = data.get("call_id") or data.get("tool", "") + f"-{len(self.tools)}"
                name = data.get("tool", "tool")
                args = data.get("args", {})
                self.tools[call_id] = ToolState(
                    call_id=call_id,
                    tool_name=name,
                    args=args,
                    status="running",
                    expanded=self.tools_expanded_global,
                )
                self.status = f"tool:{name}"
                self.notify()
                return None

            case "tool.result":
                call_id = data.get("call_id")
                # Fallback match by tool name if call_id is absent
                target: ToolState | None = None
                if call_id and call_id in self.tools:
                    target = self.tools[call_id]
                else:
                    for t in reversed(list(self.tools.values())):
                        if t.tool_name == data.get("tool") and t.status == "running":
                            target = t
                            break
                if target:
                    res = str(data.get("result", ""))
                    target.finish(res, failed=bool(data.get("failed")))
                self.status = "thinking"
                self.notify()
                return None

            case "approval.requested" | "task.approval":
                app_id = data.get("approval_id", "")
                is_historical = (
                    pending_ids is not None and seq <= watermark and app_id not in pending_ids
                )
                if is_historical:
                    return (
                        f"{self.theme.format_muted('historical approval (resolved):')} "
                        f"{app_id} ({data.get('tool')})"
                    )

                approval = ApprovalState(
                    approval_id=app_id,
                    tool=data.get("tool", ""),
                    args=data.get("args", {}),
                    reference=data.get("reference"),
                    task_id=data.get("task_id"),
                )
                self.pending_approvals.append(approval)
                self.status = "waiting_approval"
                self.set_mode(InputMode.APPROVAL)
                self.notify()
                return None

            case "approval.granted":
                app_id = data.get("approval_id", "")
                self.pending_approvals = [
                    a for a in self.pending_approvals if a.approval_id != app_id
                ]
                if not self.pending_approvals and self.mode == InputMode.APPROVAL:
                    self.set_mode(InputMode.PROMPT)
                self.status = "thinking"
                self.notify()
                return f"{self.theme.format_success('✓ approved', bold=True)} {app_id}"

            case "approval.denied":
                app_id = data.get("approval_id", "")
                self.pending_approvals = [
                    a for a in self.pending_approvals if a.approval_id != app_id
                ]
                if not self.pending_approvals and self.mode == InputMode.APPROVAL:
                    self.set_mode(InputMode.PROMPT)
                self.status = "thinking"
                self.notify()
                return f"{self.theme.format_error('✗ denied', bold=True)} {app_id}"

            case "run.stop_requested":
                self.status = "paused"
                self.notify()
                return self.theme.format_warning(
                    "■ stop requested; queue paused; authorized actions may finish", bold=True
                )

            case "run.cancelled":
                self.status = "idle"
                self.active_run_id = None
                self.streaming_text = ""
                uncertain = data.get("uncertain_tools", [])
                self.notify()
                if uncertain:
                    return self.theme.format_warning(
                        f"■ stopped; verify outcome of: {', '.join(uncertain)}", bold=True
                    )
                return self.theme.format_muted("■ run cancelled")

            case "run.failed":
                reason = data.get("reason", "unknown error")
                self.status = "error"
                self.active_run_id = None
                self.streaming_text = ""
                self.notify()
                return f"{self.theme.format_error('✗ run failed', bold=True)} {reason}"

            case "run.completed" | "run.waiting":
                # Finalize turn
                from bud.core.projection import reply_text

                full_text = reply_text(data.get("messages", [])) or self.streaming_text
                self.turns.append(Turn(role="bud", text=full_text))
                self.streaming_text = ""
                self.status = "idle"
                self.active_run_id = None
                self.notify()
                return None

        return None
