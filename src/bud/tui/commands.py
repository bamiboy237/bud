"""Lean slash commands handler for Bud TUI.

Supported commands:
  /help               Show command and hotkey guide
  /model [model_id]   Switch model (interactive menu if no model specified)
  /theme [theme_name] Switch palette (aura, opencode, vesper, kanagawa, rosepine, etc.)
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

# Single source of truth for slash commands: the autocomplete menu and /help
# are both generated from this registry, so they can never drift apart.
COMMANDS: list[tuple[str, str, str]] = [
    # (command, usage, description)
    ("/model", "[id]", "Switch model (opens choice selector if omitted)"),
    ("/theme", "[name]", "Switch palette (aura, opencode, vesper, kanagawa, ...)"),
    ("/border", "[round|sq|left]", "Switch card styling (frame, square frame, left rail)"),
    ("/cost", "", "Print token spend and request ledger"),
    ("/connect", "<gmail|cal>", "Connect account through Composio"),
    ("/clear", "", "Clear screen scrollback"),
    ("/status", "", "View queue, runs, and pending actions"),
    ("/pause", "", "Pause message queue"),
    ("/resume", "", "Resume message queue"),
    ("/stop", "", "Interrupt current run"),
    ("/cancel", "<msg_id>", "Cancel queued message"),
    ("/edit", "<id> <text>", "Edit queued message"),
    ("/exit", "", "Exit the terminal application"),
    ("/quit", "", "Exit the terminal application"),
    ("/help", "", "Show this command guide"),
]

_MAX_LABEL = max(len(f"{cmd} {usage}".strip()) for cmd, usage, _ in COMMANDS)

HOTKEYS_TEXT = """\
[Hotkeys]
  Enter                Submit prompt (accepts menu selection when open)
  Shift+Enter          Insert newline (also Alt+Enter or \\\\+Enter)
  Up / Down            Navigate slash menu (when open) or choices
  Esc                  Close menu (when open); stop active run
  Esc + Up             Recall previous prompt from history
  Tab                  Toggle expand/collapse on tool disclosure cards
  Ctrl+C               Clear buffer or stop active run
  Ctrl+D               Exit application (when input buffer is empty)
  Ctrl+L               Clear screen
  [y] / [n]            Single-key 1-touch approval / denial (when approval active)"""

HELP_TEXT = "\n".join(
    ["[Commands]"]
    + [f"  {f'{cmd} {usage}'.strip():<{_MAX_LABEL + 2}}{desc}" for cmd, usage, desc in COMMANDS]
    + ["", HOTKEYS_TEXT]
)


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

    if command in ("/exit", "/quit", "/q"):
        app.pt_app.exit()
        return None

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

    if command == "/border":
        if len(parts) > 1:
            raw = parts[1].lower().strip()
            if raw in ("round", "rounded"):
                store.set_border_style("rounded")
                return "[border] Switched border style to: rounded"
            if raw in ("sq", "square"):
                store.set_border_style("square")
                return "[border] Switched border style to: square"
            if raw in ("l", "left"):
                store.set_border_style("left")
                return "[border] Switched border style to: left rail (frameless composer)"
        usage = "[border] Current: {} (usage: /border rounded | /border square | /border left)"
        return usage.format(store.border_style)

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

    async def _switch_model(target_model: str) -> str:
        """Ask the server to pin this session's model, then reflect it locally."""
        res = await client.post(f"/sessions/{store.session_id}/model", json={"model": target_model})
        if res.status_code == 422:
            return f"[model] rejected: {res.json().get('detail', 'unknown model')}"
        res.raise_for_status()
        store.set_model(target_model)
        return f"[model] Session pinned to: {target_model}"

    if command == "/model":
        if len(parts) > 1:
            return await _switch_model(parts[1])

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
                if not selected:
                    app.commit_scrollback("[model] Selection cancelled.")
                    return
                try:
                    app.commit_scrollback(await _switch_model(selected))
                except httpx.HTTPStatusError as exc:
                    detail = exc.response.text[:200]
                    app.commit_scrollback(f"[model] failed: {exc.response.status_code}: {detail}")
                except httpx.RequestError as exc:
                    app.commit_scrollback(f"[model] failed: {exc}")

            app.spawn_task(_await_model_selection())
            return None
        except Exception as exc:
            return f"[model] Failed to retrieve models: {exc}"

    # Passthrough to core server controls
    result = await submit_control(client, store.session_id, text)
    return result
