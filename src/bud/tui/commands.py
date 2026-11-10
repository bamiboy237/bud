"""Lean slash commands handler for Bud TUI.

Supported commands:
  /help               Show command and hotkey guide
  /model [model_id]   Switch model (interactive menu if no model specified)
  /theme [theme_name] Switch visual theme (default, nord, catppuccin, gruvbox, tokyo-night)
  /cost               Display session tokens and cost summary
  /connect <app>      Connect gmail or calendar via Composio
  /clear              Clear screen history
  /status             Show detailed server and queue status
  /pause, /resume     Pause or resume message queue
  /stop               Interrupt active execution
  /cancel <msg_id>    Cancel queued message
  /edit <id> <text>   Edit queued message
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx

from bud.config import get_settings
from bud.core.controls import submit as submit_control
from bud.gateway import Ledger
from bud.tui.state import StateStore
from bud.tui.themes import list_theme_names

if TYPE_CHECKING:
    from bud.tui.app import TUIApp

HELP_TEXT = """\
[Commands]
  /model [id]          Switch model (opens choice selector if omitted)
  /theme [name]        Switch palette (default, nord, catppuccin, gruvbox, tokyo-night)
  /cost                Print token spend and request ledger
  /connect <gmail|cal> Connect account through Composio
  /clear               Clear screen scrollback
  /status              View queue, runs, and pending actions
  /pause, /resume      Pause or resume message queue
  /stop                Interrupt current run
  /cancel <msg_id>     Cancel queued message
  /edit <id> <text>    Edit queued message
  /help                Show this command guide

[Hotkeys]
  Enter                Submit prompt
  Shift+Enter          Insert newline (also Alt+Enter or \\+Enter)
  Esc                  Stop active run
  Esc + ↑              Recall previous prompt from history
  Tab                  Toggle expand/collapse on tool disclosure cards
  Ctrl+C               Clear buffer or stop active run
  Ctrl+L               Clear screen
  [y] / [n]            Single-key 1-touch approval / denial (when approval active)
  ↑ / ↓                Navigate choices (when menu active)"""


async def handle_slash_command(
    client: httpx.AsyncClient,
    store: StateStore,
    app: TUIApp,
    text: str,
) -> str | None:
    """Execute a slash command. Returns result text to display, or None."""
    parts = text.strip().split(maxsplit=2)
    command = parts[0].lower()

    if command in ("/help", "/?"):
        return HELP_TEXT

    if command == "/clear":
        return "__CLEAR__"

    if command == "/theme":
        if len(parts) > 1:
            name = parts[1].lower()
            store.set_theme(name)
            return f"[theme] Switched to {store.theme.display_name} ({store.theme.name})"
        names = ", ".join(list_theme_names())
        return (
            f"[themes] Available: {names}\nCurrent: {store.theme.display_name} ({store.theme.name})"
        )

    if command == "/cost":
        ledger_path = get_settings().data_dir / "bud.db"
        rows = Ledger(ledger_path).costs_by_day_and_model() if ledger_path.exists() else []
        session_tokens = store.input_tokens + store.output_tokens
        tokens_msg = (
            f"[Session Tokens] in: {store.input_tokens:,} | "
            f"out: {store.output_tokens:,} | total: {session_tokens:,}"
        )
        lines = [tokens_msg]
        if rows:
            lines.append("[Spend History]")
            total_usd = sum(r.cost_usd for r in rows)
            for r in rows[-5:]:
                lines.append(f"  {r.day} {r.model[:24]}: ${r.cost_usd:.4f} ({r.requests} reqs)")
            lines.append(f"  Total API spend: ${total_usd:.4f}")
        else:
            lines.append("[Spend History] No ledger requests recorded yet.")
        return "\n".join(lines)

    if command == "/model":
        if len(parts) > 1:
            target_model = parts[1]
            store.set_model(target_model)
            return f"[model] Pinned model to: {target_model}"

        # Fetch models from server and launch interactive QUESTION picker
        try:
            res = await client.get("/models")
            res.raise_for_status()
            models_data = res.json()
            if not models_data:
                return "[model] No models configured on server."

            configured = [m["id"] for m in models_data if m.get("configured")]
            choices = configured or [m["id"] for m in models_data]

            # Start interactive choice picker in QUESTION mode
            loop = asyncio.get_running_loop()
            future: asyncio.Future[str | None] = loop.create_future()
            store.start_question("Select a model:", choices, future=future)

            # Await choice in background
            async def _await_model_selection() -> None:
                selected = await future
                if selected:
                    store.set_model(selected)
                    app.commit_scrollback(f"[model] Switched to: {selected}")
                else:
                    app.commit_scrollback("[model] Selection cancelled.")

            asyncio.create_task(_await_model_selection())
            return None
        except Exception as exc:
            return f"[model] Failed to retrieve models: {exc}"

    # Passthrough to core server controls
    result = await submit_control(client, store.session_id, text)
    return result
