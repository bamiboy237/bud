"""Unit tests for Bud TUI views, themes, terminal renderer, and state store."""

from __future__ import annotations

import io
import unittest

from bud.tui.state import ApprovalState, StateStore, ToolState
from bud.tui.terminal import (
    SYNC_END,
    SYNC_START,
    DifferentialRenderer,
    SynchronizedOutput,
    strip_ansi,
    truncate_visible,
    visible_width,
)
from bud.tui.themes import (
    DEFAULT_THEME_NAME,
    get_theme,
    hex_to_rgb,
    list_theme_names,
)
from bud.tui.views.approval import ApprovalView, friendly_action_title
from bud.tui.views.footer import FooterView
from bud.tui.views.message import MessageView
from bud.tui.views.question import QuestionView
from bud.tui.views.tool import ToolView


class TestThemes(unittest.TestCase):
    def test_themes_exist(self) -> None:
        names = list_theme_names()
        self.assertIn("default", names)
        self.assertIn("groknight", names)
        self.assertIn("nord", names)
        self.assertIn("catppuccin", names)
        self.assertIn("gruvbox", names)
        self.assertIn("tokyo-night", names)

    def test_grok_theme_alias(self) -> None:
        theme = get_theme("grok")
        self.assertEqual(theme.name, "groknight")
        self.assertEqual(theme.display_name, "GrokNight")

    def test_theme_colors(self) -> None:
        nord = get_theme("nord")
        self.assertEqual(nord.name, "nord")
        self.assertEqual(hex_to_rgb("#88C0D0"), (136, 192, 208))

        formatted = nord.format_primary("test")
        self.assertIn("\x1b[38;2;236;239;244m", formatted)
        self.assertIn("test", formatted)

    def test_theme_fallback(self) -> None:
        unknown = get_theme("nonexistent")
        self.assertEqual(unknown.name, DEFAULT_THEME_NAME)

    def test_prompt_toolkit_style(self) -> None:
        theme = get_theme("catppuccin")
        style = theme.to_prompt_toolkit_style()
        self.assertIsNotNone(style)


class TestTerminal(unittest.TestCase):
    def test_strip_ansi(self) -> None:
        colored = "\x1b[38;2;255;0;0mHello\x1b[0m \x1b[1mWorld\x1b[0m"
        self.assertEqual(strip_ansi(colored), "Hello World")

    def test_visible_width(self) -> None:
        self.assertEqual(visible_width("hello"), 5)
        colored = "\x1b[31mhello\x1b[0m"
        self.assertEqual(visible_width(colored), 5)
        # Unicode wide character
        self.assertEqual(visible_width("你好"), 4)

    def test_truncate_visible(self) -> None:
        text = "Hello world, this is a long text string."
        truncated = truncate_visible(text, 10, tail="…")
        self.assertLessEqual(visible_width(truncated), 10)
        self.assertTrue(truncated.endswith("…\x1b[0m"))

    def test_differential_renderer(self) -> None:
        stream = io.StringIO()
        renderer = DifferentialRenderer(stream)

        # Render first frame
        renderer.render_frame(["Line 1", "Line 2"])
        output = stream.getvalue()
        self.assertIn(SYNC_START, output)
        self.assertIn(SYNC_END, output)
        self.assertIn("Line 1", output)
        self.assertIn("Line 2", output)
        self.assertEqual(len(renderer.prev_lines), 2)

        # Commit lines
        stream.seek(0)
        stream.truncate(0)
        renderer.commit_lines(["Committed line"])
        committed_output = stream.getvalue()
        self.assertIn("Committed line\n", committed_output)
        self.assertEqual(renderer.prev_lines, [])

    def test_synchronized_output_encloses_frame(self) -> None:
        from prompt_toolkit.output.vt100 import Vt100_Output

        stream = io.StringIO()
        raw = Vt100_Output(stream, lambda: (80, 24))
        sync = SynchronizedOutput(raw)

        raw.write("FRAME_CONTENT")
        sync.flush()

        val = stream.getvalue()
        self.assertTrue(val.startswith(SYNC_START))
        self.assertTrue(val.endswith(SYNC_END))
        self.assertIn("FRAME_CONTENT", val)
        # Empty flush should produce no output
        stream.seek(0)
        stream.truncate(0)
        sync.flush()
        self.assertEqual(stream.getvalue(), "")


class TestMessageView(unittest.TestCase):
    def test_user_message_render(self) -> None:
        view = MessageView("you", "hello bud")
        lines = view.render(80)
        self.assertTrue(len(lines) >= 1)
        plain = strip_ansi("\n".join(lines))
        self.assertIn("you › hello bud", plain)

    def test_assistant_message_markdown(self) -> None:
        view = MessageView("bud", "# Heading\n**bold text** and `code`")
        lines = view.render(80)
        plain = strip_ansi("\n".join(lines))
        self.assertIn("bud ›", plain)
        self.assertIn("bold text", plain)

    def test_token_buffering_debounce(self) -> None:
        view = MessageView("bud")
        view.start_streaming()
        # Immediate push within debounce window buffers without flush
        view.push_token("Hello")
        view.push_token(" World")
        self.assertEqual(view.text, "Hello World")
        view.finish_streaming()
        self.assertEqual(view.text, "Hello World")
        self.assertFalse(view._is_streaming)


class TestToolView(unittest.TestCase):
    def test_tool_collapsed_and_expanded(self) -> None:
        tool = ToolState(
            call_id="call_1",
            tool_name="workspace_create",
            args={"path": "test.txt", "content": "hi"},
            status="completed",
            result="File created successfully.",
            expanded=False,
        )
        tool.finish("File created successfully.")

        # Collapsed render (1 line, natural human summary)
        widget = ToolView(tool)
        collapsed_lines = widget.render(80)
        self.assertEqual(len(collapsed_lines), 1)
        plain_c = strip_ansi(collapsed_lines[0])
        self.assertIn("Saved test.txt", plain_c)
        self.assertIn("done", plain_c)

        # Expanded render (shows clean params & clean result, zero curly braces)
        tool.expanded = True
        widget.mark_dirty()
        expanded_lines = widget.render(80)
        self.assertGreater(len(expanded_lines), 1)
        plain_e = strip_ansi("\n".join(expanded_lines))
        self.assertIn("Saved test.txt", plain_e)
        self.assertIn("Path: test.txt", plain_e)
        self.assertIn("File created successfully", plain_e)
        # Ensure zero curly braces or JSON brackets
        self.assertNotIn("{", plain_e)
        self.assertNotIn("}", plain_e)

    def test_tool_python_repr_entries_parsing(self) -> None:
        """Verify python-repr dict outputs from runner.py strip braces and surface entries."""
        tool = ToolState(
            call_id="call_list",
            tool_name="workspace_list",
            args={"prefix": "data/"},
            status="completed",
            result=(
                "{'ok': True, 'entries': [{'path': 'data/bud.db', 'kind': 'file'}], "
                "'truncated': False}"
            ),
            expanded=True,
        )
        widget = ToolView(tool)
        lines = widget.render(80)
        plain = strip_ansi("\n".join(lines))
        self.assertIn("data/bud.db", plain)
        self.assertNotIn("{", plain)
        self.assertNotIn("}", plain)
        self.assertNotIn("Ok: True", plain)

    def test_tool_python_repr_text_parsing(self) -> None:
        """Verify workspace_read text preview surfaces content without internal hashes/flags."""
        tool = ToolState(
            call_id="call_read",
            tool_name="workspace_read",
            args={"path": "src/bud/config.py"},
            status="completed",
            result="{'ok': True, 'text': 'import sys\\nimport os', 'revision': 'hash123'}",
            expanded=True,
        )
        widget = ToolView(tool)
        lines = widget.render(80)
        plain = strip_ansi("\n".join(lines))
        self.assertIn("import sys", plain)
        self.assertIn("import os", plain)
        self.assertNotIn("{", plain)
        self.assertNotIn("}", plain)
        self.assertNotIn("hash123", plain)
        self.assertNotIn("Ok: True", plain)

    def test_tools_container_activity_summary(self) -> None:
        """Verify ToolsContainerView groups completed operations into a Workspace Activity card."""
        from bud.tui.views.tool import ToolsContainerView

        store = StateStore("sess_tools")
        t1 = ToolState(
            "c1", "workspace_read", {"path": "packages/console/routes.tsx"}, status="completed"
        )
        t2 = ToolState(
            "c2", "workspace_read", {"path": "packages/console/header.tsx"}, status="completed"
        )
        t3 = ToolState(
            "c3",
            "workspace_list",
            {"pattern": "*.tsx", "prefix": "packages/console"},
            status="completed",
        )

        store.tools = {"c1": t1, "c2": t2, "c3": t3}
        container = ToolsContainerView(store)

        lines = container.render(80)
        plain = strip_ansi("\n".join(lines))

        self.assertIn("Workspace Activity", plain)
        self.assertIn("Checked 2 workspace files", plain)
        self.assertIn("routes.tsx", plain)
        self.assertIn("header.tsx", plain)
        self.assertIn("Searched for '*.tsx' in packages/console", plain)
        # Ensure framed in subtle box
        self.assertIn("╭", plain)
        self.assertIn("╰", plain)


class TestApprovalView(unittest.TestCase):
    def test_approval_card_render(self) -> None:
        store = StateStore("sess_test")
        approval = ApprovalState(
            approval_id="app_123",
            tool="send_email",
            args={"to": "alice@example.com", "subject": "Test", "body": "Line 1\nLine 2"},
            reference="ref_1",
        )
        # Rounded borders (default)
        store.border_style = "rounded"
        view = ApprovalView(approval, store)
        lines = view.render(80)
        plain = strip_ansi("\n".join(lines))

        self.assertIn("Action Required: Send email to alice@example.com", plain)
        self.assertIn("alice@example.com", plain)
        self.assertIn("[y] Approve", plain)
        self.assertIn("[n] Deny", plain)
        self.assertIn("[esc] Cancel", plain)
        self.assertIn("╭", plain)
        self.assertIn("╰", plain)
        self.assertNotIn("{", plain)
        self.assertNotIn("}", plain)

        # Square borders
        store.border_style = "square"
        view.mark_dirty()
        sq_lines = view.render(80)
        sq_plain = strip_ansi("\n".join(sq_lines))
        self.assertIn("┌", sq_plain)
        self.assertIn("└", sq_plain)
        self.assertNotIn("╭", sq_plain)

    def test_approval_list_args(self) -> None:
        """Verify list arguments like multiple recipients are formatted cleanly."""
        store = StateStore("sess_test")
        approval = ApprovalState(
            approval_id="app_list",
            tool="send_email",
            args={"recipients": ["alice@example.com", "bob@example.com"]},
            reference="ref_2",
        )
        view = ApprovalView(approval, store)
        lines = view.render(80)
        plain = strip_ansi("\n".join(lines))
        self.assertIn("alice@example.com, bob@example.com", plain)
        self.assertNotIn("[alice", plain)

    def test_friendly_action_title_disambiguation(self) -> None:
        """Verify integrations and actions are mapped to executive titles."""
        t1 = friendly_action_title("COMPOSIO_GITHUB_CREATE_ISSUE", {"title": "Fix login bug"})
        self.assertEqual(t1, 'Create issue: "Fix login bug"')

        t2 = friendly_action_title("COMPOSIO_SLACK_SEND_MESSAGE", {"channel": "#general"})
        self.assertEqual(t2, "Send Slack message to #general")

        t3 = friendly_action_title("workspace_delete", {"path": "data/temp.db"})
        self.assertEqual(t3, "Delete data/temp.db")


class TestQuestionView(unittest.TestCase):
    def test_question_choice_navigation(self) -> None:
        store = StateStore("sess_test")
        choices = ["gpt-4o", "mistral-small", "deepseek-chat"]
        store.start_question("Choose a model:", choices, default_index=0)

        view = QuestionView(store)
        lines = view.render(80)
        plain = strip_ansi("\n".join(lines))

        self.assertIn("Choose a model:", plain)
        self.assertIn("› gpt-4o", plain)
        self.assertIn("mistral-small", plain)
        self.assertNotIn("●", plain)
        self.assertNotIn("○", plain)

        # Move down
        store.select_next_choice()
        self.assertEqual(store.question_selected_index, 1)

        lines2 = view.render(80)
        plain2 = strip_ansi("\n".join(lines2))
        self.assertIn("› mistral-small", plain2)
        self.assertNotIn("●", plain2)
        self.assertNotIn("○", plain2)


class TestFooterView(unittest.TestCase):
    def test_footer_responsive_render(self) -> None:
        store = StateStore("sess_test", model_id="gpt-4o-mini")
        store.input_tokens = 500
        store.output_tokens = 800
        store.last_latency_ms = 185.0

        footer = FooterView(store)
        lines = footer.render(80)
        self.assertEqual(len(lines), 1)
        plain = strip_ansi(lines[0])
        self.assertIn("idle", plain)
        self.assertIn("gpt-4o-mini", plain)
        self.assertIn("1.3k tokens", plain)
        self.assertIn("185ms", plain)
        self.assertNotIn("│", plain)


class TestStateStore(unittest.TestCase):
    def test_event_lifecycle(self) -> None:
        store = StateStore("sess_test")
        notifications = 0

        def _on_update() -> None:
            nonlocal notifications
            notifications += 1

        store.subscribe(_on_update)

        # 1. User message
        committed = store.handle_event({"type": "user.message", "data": {"text": "Hello"}})
        self.assertIn("Hello", committed or "")
        self.assertEqual(len(store.turns), 1)

        # 2. Run started
        store.handle_event({"type": "run.started", "data": {"run_id": "r1"}})
        self.assertEqual(store.status, "thinking")
        self.assertEqual(store.active_run_id, "r1")

        # 3. Text delta
        store.handle_event({"type": "text.delta", "data": {"text": "World"}})
        self.assertEqual(store.streaming_text, "World")

        # 4. Tool call
        store.handle_event(
            {
                "type": "tool.call",
                "data": {"call_id": "c1", "tool": "search", "args": {"q": "bud"}},
            }
        )
        self.assertIn("c1", store.tools)
        self.assertEqual(store.tools["c1"].status, "running")

        # 5. Tool result
        store.handle_event(
            {
                "type": "tool.result",
                "data": {"call_id": "c1", "tool": "search", "result": "found"},
            }
        )
        self.assertEqual(store.tools["c1"].status, "completed")

        # 6. Run completed
        store.handle_event(
            {
                "type": "run.completed",
                "data": {"run_id": "r1", "messages": []},
            }
        )
        self.assertEqual(store.status, "idle")
        self.assertGreater(notifications, 4)

    def test_border_style_toggle(self) -> None:
        store = StateStore("sess_test")
        self.assertTrue(store.set_border_style("square"))
        self.assertEqual(store.border_style, "square")
        self.assertTrue(store.set_border_style("round"))
        self.assertEqual(store.border_style, "rounded")
        self.assertTrue(store.set_border_style("sq"))
        self.assertEqual(store.border_style, "square")
        self.assertTrue(store.set_border_style("rounded"))
        self.assertEqual(store.border_style, "rounded")
        self.assertFalse(store.set_border_style("invalid"))

    def test_history_recall(self) -> None:
        store = StateStore("sess_test")
        store.add_prompt_history("prompt 1")
        store.add_prompt_history("prompt 2")

        self.assertEqual(store.recall_prev_prompt(), "prompt 2")
        self.assertEqual(store.recall_prev_prompt(), "prompt 1")
        self.assertEqual(store.recall_next_prompt(), "prompt 2")


if __name__ == "__main__":
    unittest.main()
