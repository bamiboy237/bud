"""Original-mail previews and request grouping."""

import base64
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from pydantic_ai import Agent, DeferredToolRequests, Tool
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from bud.composio.binding import AccountBinding
from bud.composio.connection import Connections
from bud.composio.tools import ComposioTools
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.projection import request_groups, snapshot
from bud.core.runner import _review_of
from bud.core.tasks import TaskBrief, WorkItems
from bud.harness.actions import ActionGateway, Policy
from bud.harness.email_preview import email_preview
from bud.harness.review import held
from bud.world.store import World
from bud.world.tools import build_tools


def gmail_message(body: str, mime: str = "text/plain") -> dict:
    raw = body.encode()
    return {
        "id": "original-message",
        "threadId": "original-thread",
        "payload": {
            "mimeType": mime,
            "headers": [
                {"name": "Subject", "value": "Original subject"},
                {"name": "From", "value": "Sender <sender@example.com>"},
                {"name": "To", "value": "owner@example.com"},
                {"name": "Cc", "value": "cc@example.com"},
                {"name": "Content-Type", "value": f"{mime}; charset=utf-8"},
            ],
            "body": {"data": base64.urlsafe_b64encode(raw).decode(), "size": len(raw)},
        },
    }


async def held_call(toolset, name: str, args: dict, policy: dict, *, reviewer=None):
    def respond(messages, _info):
        if any(isinstance(part, ToolReturnPart) for part in messages[-1].parts):
            return ModelResponse(parts=[TextPart("Done")])
        return ModelResponse(parts=[ToolCallPart(name, args, "unchanged-call-id")])

    gateway = ActionGateway(
        toolset, policy=Policy(tools=policy), record=lambda _: None, reviewer=reviewer
    )
    agent = Agent(
        FunctionModel(respond),
        toolsets=[gateway],
        output_type=[str, DeferredToolRequests],
    )
    result = await agent.run("Archive the original email", metadata={"bud_request": "Archive it"})
    assert isinstance(result.output, DeferredToolRequests)
    return result.output


class EmailPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_account_wrapper_checks_binding_and_only_dispatches_read(self):
        reads = []

        async def fetch(thread_id: str, user_id: str):
            reads.append((thread_id, user_id))
            return {
                "successful": True,
                "data": {"messages": [gmail_message("Full original\n\nThank you.")]},
            }

        async def create(thread_id: str, body: str):
            self.fail("No real-account write may run while held")

        with tempfile.TemporaryDirectory() as folder:
            binding = AccountBinding("owner", 1, (("gmail", "account"),))
            connections = Mock(spec=Connections)
            path = Path(folder) / "dispatches.db"
            toolset = ComposioTools(
                FunctionToolset(
                    [
                        Tool(fetch, name="GMAIL_FETCH_MESSAGE_BY_THREAD_ID"),
                        Tool(create, name="GMAIL_CREATE_EMAIL_DRAFT"),
                    ]
                ),
                dispatch_path=path,
                allowed_tools=frozenset(
                    {
                        "GMAIL_FETCH_MESSAGE_BY_THREAD_ID",
                        "GMAIL_CREATE_EMAIL_DRAFT",
                    }
                ),
                connections=connections,
                binding=binding,
            )
            args = {"thread_id": "original-thread", "body": "Proposed reply"}
            requests = await held_call(
                toolset,
                "GMAIL_CREATE_EMAIL_DRAFT",
                args,
                {
                    "GMAIL_FETCH_MESSAGE_BY_THREAD_ID": "allow",
                    "GMAIL_CREATE_EMAIL_DRAFT": "ask",
                },
            )
            self.assertEqual(reads, [("original-thread", "me")])
            connections.assert_current_binding.assert_called_with(binding, "gmail")
            self.assertFalse(path.exists())
            self.assertEqual(requests.approvals[0].args_as_dict(), args)
            metadata = _review_of(requests.metadata["unchanged-call-id"])
            self.assertEqual(
                metadata["preview"]["messages"][0]["body"], "Full original\n\nThank you."
            )

    async def test_archive_full_original_store_to_event_snapshot_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            world = World(Path(folder) / "world.db")
            world.reset()
            thread_id = world.snapshot()["threads"][0]["id"]
            original = world.read_thread(thread_id)
            before = world.snapshot()
            args = {"thread_id": thread_id}
            requests = await held_call(
                FunctionToolset(build_tools(world)),
                "archive_email",
                args,
                {"archive_email": "ask", "read_thread": "allow"},
            )
            self.assertEqual(world.snapshot(), before)
            call = requests.approvals[0]
            self.assertEqual(call.args_as_dict(), args)
            self.assertEqual(call.tool_call_id, "unchanged-call-id")
            metadata = _review_of(requests.metadata[call.tool_call_id])
            self.assertNotIn("review", metadata)
            preview = metadata["preview"]
            self.assertEqual(preview["subject"], original["subject"])
            self.assertEqual(preview["status"], "available")
            self.assertEqual(
                [message["body"] for message in preview["messages"]],
                [message["body"] for message in original["messages"]],
            )
            self.assertTrue(all(message["to"] for message in preview["messages"]))
            log = EventLog(Path(folder) / "events.db")
            log.append("session", "run.queued", {"run_id": "run"})
            request = {
                "approval_id": "unchanged-approval-id",
                "run_id": "run",
                "tool": call.tool_name,
                "call_id": call.tool_call_id,
                "args": call.args_as_dict(),
                **metadata,
            }
            log.finish_run("session", "run", "run.waiting", {"run_id": "run"}, [request])
            pending = snapshot("session", log.read("session")).pending_approvals[0]
            self.assertEqual(pending.approval_id, "unchanged-approval-id")
            self.assertEqual(pending.args, args)
            self.assertEqual(pending.run_id, "run")
            self.assertEqual(pending.request_id, "run")
            assert pending.preview is not None
            self.assertEqual(pending.preview.model_dump(), preview)
            log.append("session", "approval.granted", {"approval_id": pending.approval_id})
            self.assertEqual(snapshot("session", log.read("session")).pending_approvals, [])

    async def test_missing_original_keeps_exact_request_and_no_connector_error(self):
        async def read_thread(thread_id: str):
            raise RuntimeError("private connector details")

        async def archive_email(thread_id: str):
            self.fail("No write may run during preview")

        requests = await held_call(
            FunctionToolset([Tool(read_thread), Tool(archive_email)]),
            "archive_email",
            {"thread_id": "missing"},
            {"archive_email": "ask", "read_thread": "allow"},
        )
        preview = _review_of(requests.metadata["unchanged-call-id"])["preview"]
        self.assertEqual(preview["status"], "unavailable")
        self.assertNotIn("private", str(preview))
        self.assertEqual(requests.approvals[0].args_as_dict(), {"thread_id": "missing"})

    async def test_preview_respects_read_policy(self):
        async def read_thread(thread_id: str):
            self.fail("A denied read must not run for presentation")

        async def archive_email(thread_id: str):
            self.fail("No write may run during preview")

        requests = await held_call(
            FunctionToolset([Tool(read_thread), Tool(archive_email)]),
            "archive_email",
            {"thread_id": "target"},
            {"archive_email": "ask", "read_thread": "deny"},
        )
        preview = _review_of(requests.metadata["unchanged-call-id"])["preview"]
        self.assertEqual(preview["status"], "unavailable")
        self.assertEqual(requests.approvals[0].args_as_dict(), {"thread_id": "target"})

    async def test_original_read_failure_preserves_safe_proposed_html_text(self):
        async def fetch(thread_id: str, user_id: str):
            raise RuntimeError("private connector failure")

        async def send(thread_id: str, body: str, is_html: bool):
            self.fail("A preview must not send email")

        requests = await held_call(
            FunctionToolset(
                [
                    Tool(fetch, name="GMAIL_FETCH_MESSAGE_BY_THREAD_ID"),
                    Tool(send, name="GMAIL_SEND_EMAIL"),
                ]
            ),
            "GMAIL_SEND_EMAIL",
            {"thread_id": "target", "body": "<p>Proposed reply</p>", "is_html": True},
            {"GMAIL_SEND_EMAIL": "ask", "GMAIL_FETCH_MESSAGE_BY_THREAD_ID": "allow"},
        )
        preview = _review_of(requests.metadata["unchanged-call-id"])["preview"]
        self.assertEqual(preview["status"], "unavailable")
        self.assertEqual(preview["proposed_body_text"], "Proposed reply")
        self.assertNotIn("private", str(preview))

    async def test_mark_read_and_draft_reply_resolve_original_not_proposed_text(self):
        original = {
            "subject": "Original",
            "messages": [
                {
                    "id": "m1",
                    "sender": "sender@example.com",
                    "recipients": '["owner@example.com"]',
                    "sent_at": "2026-11-15",
                    "body": "Full original\n\nQuoted history\n> previous mail",
                }
            ],
        }
        for name in ("mark_read", "draft_reply"):
            with self.subTest(name=name):
                read = AsyncMock(return_value=original)
                preview = await email_preview(
                    name, {"thread_id": "target", "body": "Proposed only"}, read
                )
                assert preview is not None
                self.assertEqual(preview.messages[0].body, original["messages"][0]["body"])
                self.assertNotIn("Proposed only", str(preview.messages))
                read.assert_awaited_once_with("read_thread", {"thread_id": "target"})

    async def test_held_review_keeps_review_separate(self):
        async def read_thread(thread_id: str):
            return {"subject": "Original", "messages": []}

        async def archive_email(thread_id: str):
            self.fail("No write may run during preview")

        reviewer = AsyncMock()
        reviewer.review.return_value = held("Owner should decide")
        requests = await held_call(
            FunctionToolset([Tool(read_thread), Tool(archive_email)]),
            "archive_email",
            {"thread_id": "missing"},
            {"archive_email": "review", "read_thread": "allow"},
            reviewer=reviewer,
        )
        metadata = _review_of(requests.metadata["unchanged-call-id"])
        self.assertEqual(metadata["review"]["reason"], "Owner should decide")
        self.assertNotIn("email_preview", metadata["review"])
        self.assertEqual(metadata["preview"]["status"], "unavailable")

    async def test_gmail_original_is_complete_not_snippet_or_proposed_body(self):
        body = "Original\n\n" + ("Long body with ünicode.\n" * 1000)
        message = gmail_message(body)
        message["snippet"] = "not a full body"
        read = AsyncMock(return_value={"successful": True, "data": {"messages": [message]}})
        args = {"thread_id": "original-thread", "body": "Proposed reply", "is_html": False}
        before = copy.deepcopy(args)
        preview = await email_preview("GMAIL_CREATE_EMAIL_DRAFT", args, read)
        assert preview is not None
        self.assertEqual(preview.messages[0].body, body)
        self.assertEqual(preview.messages[0].body_text, body)
        self.assertEqual(preview.messages[0].sender, "Sender <sender@example.com>")
        self.assertEqual(preview.messages[0].cc, ["cc@example.com"])
        self.assertEqual(preview.proposed_body_text, "Proposed reply")
        self.assertEqual(preview.status, "available")
        self.assertEqual(args, before)
        read.assert_awaited_once_with(
            "GMAIL_FETCH_MESSAGE_BY_THREAD_ID",
            {"thread_id": "original-thread", "user_id": "me"},
        )

    async def test_html_original_and_outgoing_display_text_never_load_resources(self):
        html = (
            "<html><head><style>hidden</style></head><body>"
            "<p>Hello &amp; welcome</p><ul><li>One</li><li>Two</li></ul>"
            "<blockquote>Quoted original</blockquote><script>hidden()</script>"
            '<img src="https://example.invalid/tracker"></body></html>'
        )
        read = AsyncMock(
            return_value={
                "successful": True,
                "data": {"messages": [gmail_message(html, "text/html")]},
            }
        )
        preview = await email_preview(
            "GMAIL_SEND_EMAIL",
            {"thread_id": "original-thread", "body": "<p>New reply</p>", "is_html": True},
            read,
        )
        assert preview is not None
        message = preview.messages[0]
        assert message.body_text is not None
        self.assertEqual(message.body, html)
        self.assertEqual(message.body_format, "html")
        self.assertIn("Hello & welcome\n", message.body_text)
        self.assertIn("• One\n", message.body_text)
        self.assertIn("\nQuoted original", message.body_text)
        self.assertNotIn("hidden", message.body_text)
        self.assertNotIn("tracker", message.body_text)
        self.assertEqual(preview.proposed_body_text, "New reply")
        read.reset_mock()
        unthreaded = await email_preview(
            "GMAIL_SEND_EMAIL", {"body": "<p>Send</p>", "is_html": True}, read
        )
        assert unthreaded is not None
        self.assertEqual(unthreaded.proposed_body_text, "Send")
        self.assertEqual(unthreaded.messages, [])
        self.assertEqual(unthreaded.status, "available")
        self.assertIsNone(unthreaded.unavailable_reason)
        read.assert_not_awaited()

    async def test_gmail_missing_body_pagination_mismatch_and_failure_are_explicit(self):
        for case in ("snippet", "pagination", "mismatch", "failed", "size"):
            with self.subTest(case=case):
                message = gmail_message("Original")
                data = {"messages": [message]}
                result = {"successful": True, "data": data}
                if case == "snippet":
                    message["payload"] = {}
                    message["snippet"] = "Do not pass this off as a full email"
                elif case == "pagination":
                    data["nextPageToken"] = "another-page"
                elif case == "mismatch":
                    message["threadId"] = "wrong-thread"
                elif case == "failed":
                    result["successful"] = False
                elif case == "size":
                    message["payload"]["body"]["size"] = 999
                preview = await email_preview(
                    "GMAIL_CREATE_EMAIL_DRAFT",
                    {"thread_id": "original-thread"},
                    AsyncMock(return_value=result),
                )
                assert preview is not None
                self.assertIn(preview.status, ("partial", "unavailable"))
                self.assertTrue(preview.unavailable_reason)
                if case in {"snippet", "size"}:
                    self.assertIsNone(preview.messages[0].body)

    async def test_unknown_and_unthreaded_actions_do_not_read(self):
        read = AsyncMock()
        self.assertIsNone(await email_preview("delete_event", {"thread_id": "x"}, read))
        self.assertIsNone(await email_preview("send_email", {"body": "Proposed"}, read))
        read.assert_not_awaited()

    async def test_mixed_unresolved_inline_is_partial_but_named_attachments_are_excluded(self):
        for filename in ("", "attachment.txt"):
            with self.subTest(filename=filename):
                message = gmail_message("Unused parent body")
                message["payload"] = {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        gmail_message("First inline section")["payload"],
                        {
                            "mimeType": "text/plain",
                            "filename": filename,
                            "body": {"attachmentId": "download-required", "size": 100},
                        },
                    ],
                }
                preview = await email_preview(
                    "GMAIL_CREATE_EMAIL_DRAFT",
                    {"thread_id": "original-thread"},
                    AsyncMock(return_value={"successful": True, "data": {"messages": [message]}}),
                )
                assert preview is not None
                if filename:
                    self.assertEqual(preview.status, "available")
                    self.assertEqual(preview.messages[0].body, "First inline section")
                else:
                    self.assertEqual(preview.status, "partial")
                    self.assertIsNone(preview.messages[0].body)
                    self.assertTrue(preview.unavailable_reason)


class RequestGroupingTests(unittest.TestCase):
    def test_new_sibling_tasks_share_source_but_updates_and_other_requests_do_not(self):
        with tempfile.TemporaryDirectory() as folder:
            log = EventLog(Path(folder) / "events.db")
            hub = Hub(log)
            log.append("feed", "session.created", {})
            store = WorkItems(hub)
            brief = TaskBrief.model_validate(
                {"goal": "Read mail", "inputs": [], "expected_result": "Report"}
            )
            first = store.create("feed", "One", brief, "source-1", None, "parent-request")
            second = store.create("feed", "Two", brief, "source-2", None, "parent-request")
            third = store.create("feed", "Other", brief, "source-3", None, "other-request")
            for item, source in ((first, "source-1"), (second, "source-2"), (third, "source-3")):
                run_id = f"child-{item.task_id}"
                log.append(
                    item.session_id,
                    "run.queued",
                    {"run_id": run_id, "message_id": source, "trigger": "message"},
                )
                log.finish_run(
                    item.session_id,
                    run_id,
                    "run.waiting",
                    {"run_id": run_id},
                    [
                        {
                            "run_id": run_id,
                            "approval_id": f"approval-{item.task_id}",
                            "call_id": "call",
                            "tool": "archive_email",
                            "args": {"thread_id": "x"},
                        }
                    ],
                )
            views = store.views("feed")
            self.assertEqual(views[0].pending_approvals[0].request_id, "parent-request")
            self.assertEqual(views[1].pending_approvals[0].request_id, "parent-request")
            self.assertEqual(views[2].pending_approvals[0].request_id, "other-request")
            store.publish(first.session_id)
            event = next(event for event in log.read("feed") if event.type == "task.approval")
            self.assertEqual(event.data["request_id"], "parent-request")
            self.assertEqual(event.data["task_id"], first.task_id)
            created = next(event for event in log.read("feed") if event.type == "task.created")
            self.assertEqual(created.data["source_run"], "parent-request")
            store.update(first, "Changed request", "update", None, "new-request")
            log.append(
                first.session_id,
                "run.queued",
                {"run_id": "new-child", "message_id": "update", "trigger": "message"},
            )
            groups = request_groups(log.read(first.session_id))
            self.assertEqual(groups["new-child"], "new-request")
            log.append(
                first.session_id, "message.edited", {"message_id": "update", "text": "Edited"}
            )
            log.append(
                first.session_id,
                "run.queued",
                {"run_id": "edited-child", "message_id": "update", "trigger": "message"},
            )
            groups = request_groups(log.read(first.session_id))
            self.assertEqual(groups["edited-child"], "edited-child")

    def test_legacy_and_approval_continuations(self):
        with tempfile.TemporaryDirectory() as folder:
            log = EventLog(Path(folder) / "events.db")
            log.append("s", "run.queued", {"run_id": "original", "trigger": "message"})
            log.append("s", "run.queued", {"run_id": "continuation", "trigger": "approval"})
            log.append("s", "run.queued", {"run_id": "new-request", "trigger": "message"})
            self.assertEqual(
                request_groups(log.read("s")),
                {"original": "original", "continuation": "original", "new-request": "new-request"},
            )
            log.append(
                "s",
                "approval.requested",
                {"approval_id": "legacy", "tool": "archive_email", "args": {"thread_id": "x"}},
            )
            log.append("s", "run.waiting", {"run_id": "new-request"})
            pending = snapshot("s", log.read("s")).pending_approvals[0]
            self.assertIsNone(pending.run_id)
            self.assertIsNone(pending.request_id)
            self.assertIsNone(pending.preview)

    def test_late_approval_uses_original_request_not_the_latest_request(self):
        with tempfile.TemporaryDirectory() as folder:
            log = EventLog(Path(folder) / "events.db")
            log.append("s", "run.queued", {"run_id": "original", "trigger": "message"})
            log.append("s", "approval.requested", {"run_id": "original", "approval_id": "parked"})
            log.append("s", "run.queued", {"run_id": "new", "trigger": "message"})
            log.append(
                "s",
                "run.queued",
                {"run_id": "late", "trigger": "approval", "late_call": {"approval_id": "parked"}},
            )
            self.assertEqual(request_groups(log.read("s"))["late"], "original")


if __name__ == "__main__":
    unittest.main()
