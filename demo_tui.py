"""Standalone interactive playground to test Bud TUI features offline.

Run with:
    cd /Users/king/Desktop/bud/.worktrees/feat-tui
    uv run python demo_tui.py

Features you can test:
- Interactive framed composer with rounded hairline cards
- Live theme switching (/theme aura, /theme opencode, /theme vesper, /theme kanagawa)
- Live border switching (/border round, /border sq)
- Interactive model selector (/model)
- Executive approval modal (/approval)
- Simulated streaming response & Workspace Activity card (any text prompt)
- Exit with Ctrl-D or /exit
"""

from __future__ import annotations

import asyncio
import contextlib

from prompt_toolkit.patch_stdout import patch_stdout

from bud.tui.app import TUIApp
from bud.tui.state import ApprovalState, InputMode, ToolState
from bud.tui.views.message import MessageView


class StandaloneTUIApp(TUIApp):
    """Standalone TUI application for testing interactions without a running backend."""

    def __init__(self) -> None:
        import httpx

        # Dummy client not used for network in standalone mode
        client = httpx.AsyncClient()
        super().__init__(client, "sess_demo", model_id="claude-3-5-sonnet")

    async def _process_user_input(self, text: str) -> None:
        trimmed = text.strip()

        # Custom demo triggers
        if trimmed in ("/approval", "/test approval"):
            appr = ApprovalState(
                approval_id="app_send_01",
                tool="COMPOSIO_GMAIL_SEND_EMAIL",
                args={
                    "to": "alice@example.com",
                    "subject": "Sign-off: Database Retention Verification",
                    "body": "Hi Alice,\n\nDatabase retention rules are verified.\n\nBest,\nBud",
                },
                reference="ref_email_01",
            )
            self.store.pending_approvals = [appr]
            self.store.set_mode(InputMode.APPROVAL)
            self.commit_scrollback(
                "[demo] Executive approval modal active: [y] approve, [n] deny, [Esc] cancel."
            )
            return

        if trimmed in ("/help", "/?"):
            from bud.tui.commands import HELP_TEXT

            self.commit_scrollback(HELP_TEXT)
            self.commit_scrollback(
                "\n[Demo Shortcuts]\n"
                "  /approval            Trigger sample executive action approval card\n"
                "  /theme <name>        Switch palette (aura, opencode, vesper, etc.)\n"
                "  /border [round|sq]   Toggle rounded vs square hairline borders\n"
                "  /model               Open interactive model picker\n"
            )
            return

        # Handle standard slash commands (/theme, /border, /model, etc.)
        if trimmed.startswith("/"):
            await super()._process_user_input(trimmed)
            return

        # Regular prompt: simulate an AI turn with tools and streaming
        self.store.add_prompt_history(trimmed)
        user_line = f"  {self.store.theme.format_primary('you ›', bold=True)} {trimmed}"
        self.commit_scrollback(user_line)

        # 1. Simulate thinking & tools
        self.store.status = "thinking"
        t1 = ToolState("c1", "workspace_read", {"path": "src/bud/config.py"}, status="completed")
        t2 = ToolState(
            "c2", "workspace_read", {"path": "src/bud/gateway/ledger.py"}, status="completed"
        )
        t3 = ToolState(
            "c3", "workspace_list", {"prefix": "src/bud", "pattern": "*.py"}, status="completed"
        )
        self.store.tools = {"c1": t1, "c2": t2, "c3": t3}
        self.store.notify()
        await asyncio.sleep(0.4)

        # 2. Simulate streaming reply
        self.store.status = "streaming"
        reply = (
            f'I reviewed your request: "{trimmed}".\n\n'
            "- **Verified**: Analyzed `src/bud/config.py` and active ledger definitions.\n"
            "- **Theme**: Currently running under **" + self.store.theme.display_name + "**.\n"
            "- **Borders**: **"
            + self.store.border_style.capitalize()
            + "** hairline box containers.\n\n"
            "Try `/theme opencode`, `/theme vesper`, or `/border sq` to explore styles live!"
        )

        words = reply.split(" ")
        accumulated = ""
        for word in words:
            accumulated += word + " "
            self.store.streaming_text = accumulated
            self.store.notify()
            await asyncio.sleep(0.02)

        # 3. Commit turn outcome to scrollback
        cols, _ = (80, 24)
        summary_card = self.tools_view.render(cols)
        if summary_card:
            self.commit_scrollback(summary_card)
        self.store.tools.clear()

        reply_view = MessageView("bud", reply, self.store)
        self.commit_scrollback(reply_view.render(cols))

        self.store.status = "idle"
        self.store.streaming_text = ""
        self.store.notify()

    async def run(self) -> None:
        """Run standalone demo without SSE connection."""
        self._running = True

        welcome = (
            f"  {self.store.theme.format_accent('bud', bold=True)} "
            f"{self.store.theme.format_dim('standalone demo ·')} "
            f"{self.store.theme.format_secondary('OpenCode Aura')} "
            f"{self.store.theme.format_dim('· Type /help for commands · Ctrl-D to exit')}"
        )
        self.commit_scrollback(welcome)
        self.commit_scrollback("")

        try:
            with patch_stdout():
                await self.pt_app.run_async()
        finally:
            self._running = False
            for task in list(self._background_tasks):
                task.cancel()


if __name__ == "__main__":
    app = StandaloneTUIApp()
    with contextlib.suppress(KeyboardInterrupt, EOFError):
        asyncio.run(app.run())
