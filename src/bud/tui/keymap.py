"""Physical keyboard shortcuts and mode-specific input traps for Bud TUI.

Bindings adhere to:
  - PROMPT mode: Enter to submit, Shift+Enter / Alt+Enter / \\+Enter for newlines,
    Esc to stop active run, Esc+↑ to recall previous prompt, Tab to toggle tools,
    Ctrl+C to clear dirty buffer or interrupt, Ctrl+L to clear screen.
  - APPROVAL mode: 1-key traps [y] approve, [n] deny, [Esc] cancel (zero UUID typing).
  - QUESTION mode: ↑/↓ to navigate choices, Enter to select, Esc to cancel.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent

from bud.tui.state import InputMode, StateStore

if TYPE_CHECKING:
    from bud.tui.app import TUIApp


def build_key_bindings(store: StateStore, app: TUIApp) -> KeyBindings:
    """Create prompt-toolkit KeyBindings wired to the active reactive state."""
    kb = KeyBindings()

    is_prompt = Condition(lambda: store.mode == InputMode.PROMPT)
    is_approval = Condition(lambda: store.mode == InputMode.APPROVAL)
    is_question = Condition(lambda: store.mode == InputMode.QUESTION)

    # -------------------------------------------------------------
    # APPROVAL Mode: Single-key traps
    # -------------------------------------------------------------
    @kb.add("y", filter=is_approval)
    @kb.add("Y", filter=is_approval)
    def _approve(event: KeyPressEvent) -> None:
        asyncio.create_task(app.handle_approval_decision(approved=True))

    @kb.add("n", filter=is_approval)
    @kb.add("N", filter=is_approval)
    def _deny(event: KeyPressEvent) -> None:
        asyncio.create_task(app.handle_approval_decision(approved=False))

    @kb.add("escape", filter=is_approval)
    def _cancel_approval(event: KeyPressEvent) -> None:
        app.handle_approval_cancel()

    # -------------------------------------------------------------
    # QUESTION Mode: Arrow navigation & confirm
    # -------------------------------------------------------------
    @kb.add("up", filter=is_question)
    @kb.add("k", filter=is_question)
    def _choice_up(event: KeyPressEvent) -> None:
        store.select_prev_choice()

    @kb.add("down", filter=is_question)
    @kb.add("j", filter=is_question)
    def _choice_down(event: KeyPressEvent) -> None:
        store.select_next_choice()

    @kb.add("enter", filter=is_question)
    def _choice_select(event: KeyPressEvent) -> None:
        store.confirm_question()

    @kb.add("escape", filter=is_question)
    def _choice_cancel(event: KeyPressEvent) -> None:
        store.cancel_question()

    # -------------------------------------------------------------
    # PROMPT Mode: Submission, newlines, disclosure, interruption
    # -------------------------------------------------------------
    @kb.add("tab", filter=is_prompt)
    def _toggle_disclosure(event: KeyPressEvent) -> None:
        store.toggle_tool_disclosure()

    @kb.add("escape", "up", filter=is_prompt)
    def _recall_prev(event: KeyPressEvent) -> None:
        prev = store.recall_prev_prompt()
        if prev is not None:
            event.current_buffer.text = prev
            event.current_buffer.cursor_position = len(prev)

    @kb.add("escape", "down", filter=is_prompt)
    def _recall_next(event: KeyPressEvent) -> None:
        nxt = store.recall_next_prompt()
        if nxt is not None:
            event.current_buffer.text = nxt
            event.current_buffer.cursor_position = len(nxt)

    @kb.add("escape", filter=is_prompt)
    def _interrupt_run(event: KeyPressEvent) -> None:
        if store.active_run_id or store.status in ("thinking", "streaming"):
            asyncio.create_task(app.stop_run())

    @kb.add("c-c", filter=is_prompt)
    def _ctrl_c(event: KeyPressEvent) -> None:
        buf = event.current_buffer
        if buf.text:
            buf.text = ""
        elif store.active_run_id or store.status in ("thinking", "streaming"):
            asyncio.create_task(app.stop_run())

    @kb.add("c-d", filter=is_prompt)
    def _ctrl_d(event: KeyPressEvent) -> None:
        if not event.current_buffer.text:
            event.app.exit()

    @kb.add("c-l", filter=is_prompt)
    def _clear_screen(event: KeyPressEvent) -> None:
        app.clear_screen()

    # Newlines: Alt+Enter or Ctrl+J (Shift+Enter terminal mapping)
    @kb.add("c-j", filter=is_prompt)
    @kb.add("escape", "enter", filter=is_prompt)
    def _newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    # Enter to submit, or backslash+Enter for multiline continuation
    @kb.add("enter", filter=is_prompt)
    def _submit_or_continue(event: KeyPressEvent) -> None:
        buf = event.current_buffer
        text = buf.text
        if text.endswith("\\"):
            # Replace trailing backslash with newline
            buf.text = text[:-1] + "\n"
            buf.cursor_position = len(buf.text)
            return
        buf.validate_and_handle()

    return kb
