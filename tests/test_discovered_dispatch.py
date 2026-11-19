"""Connected app discovery and approvals at Bud's model and account boundaries."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolApproved
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from bud.composio.binding import AccountBinding
from bud.composio.connection import Connections
from bud.composio.tools import build_toolset, policy
from bud.harness.context import BackgroundCompaction
from tests.test_live_tools import harness


def app_catalog(tmp_path):
    connections = Mock(spec=Connections)
    connections.settings = SimpleNamespace(composio_api_key=None)
    connections.binding = AccountBinding("owner", 1, (("slack", "account"),))
    client = connections.client.return_value
    client.create.return_value = SimpleNamespace(session_id="search-session")
    client.client.tool_router.session.search.return_value = SimpleNamespace(
        results=[
            SimpleNamespace(
                primary_tool_slugs=["SLACK_SEND_MESSAGE"], related_tool_slugs=["GMAIL_SEND_EMAIL"]
            )
        ]
    )
    api = client.client.tools
    api.list.return_value = SimpleNamespace(
        items=[
            SimpleNamespace(
                slug="SLACK_SEND_MESSAGE",
                description="Send a Slack message.",
                version="20261118_00",
                input_parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
            SimpleNamespace(slug="GMAIL_SEND_EMAIL"),
        ]
    )
    api.execute.return_value.model_dump.return_value = {"successful": True, "data": {"id": "m1"}}
    subject = harness(tmp_path)
    subject.policy.tools.update(policy().tools)
    subject.swap_tools(build_toolset(connections, connections.binding.accounts, tmp_path))
    return subject, connections, api


async def test_plain_chat_does_not_fetch_a_catalog(tmp_path):
    subject, connections, _ = app_catalog(tmp_path)

    def respond(messages, info):
        names = {definition.name for definition in info.function_tools}
        assert "find_app_actions" in names
        assert "SLACK_SEND_MESSAGE" not in names
        return ModelResponse(parts=[TextPart("Hello.")])

    agent = subject.agent(
        FunctionModel(respond), lambda _: None, context=BackgroundCompaction(record=lambda _: None)
    )
    assert (await agent.run("Hi")).output == "Hello."
    connections.client.assert_not_called()
    connections.assert_current_binding.assert_not_called()


async def test_first_turn_search_loads_one_app_then_holds_exact_write(tmp_path):
    subject, connections, api = app_catalog(tmp_path)
    subject.policy.grants = frozenset({"SLACK_SEND_MESSAGE"})
    step = 0

    def respond(messages, info):
        nonlocal step
        step += 1
        names = {definition.name for definition in info.function_tools}
        assert "GMAIL_SEND_EMAIL" not in names
        if step == 1:
            assert "SLACK_SEND_MESSAGE" not in names
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "find_app_actions", {"app": "slack", "query": "send message"}, "find"
                    ),
                ]
            )
        if step == 2:
            assert "SLACK_SEND_MESSAGE" in names
            return ModelResponse(
                parts=[
                    ToolCallPart("SLACK_SEND_MESSAGE", {"text": "hello"}, "send"),
                ]
            )
        return ModelResponse(parts=[TextPart("Sent.")])

    agent = subject.agent(
        FunctionModel(respond), lambda _: None, context=BackgroundCompaction(record=lambda _: None)
    )
    metadata = {"bud_app_turn": "first", "bud_request": "Send hello on Slack"}
    result = await agent.run("Send hello on Slack", metadata=metadata)
    assert isinstance(result.output, DeferredToolRequests)
    assert [call.tool_name for call in result.output.approvals] == ["SLACK_SEND_MESSAGE"]
    connections.client.return_value.client.tool_router.session.search.assert_called_once_with(
        "search-session", queries=[{"use_case": "send message"}]
    )
    # GMAIL_SEND_EMAIL belongs to another app, so only the Slack definition is loaded.
    api.list.assert_called_once_with(tool_slugs="SLACK_SEND_MESSAGE", limit=1)
    api.execute.assert_not_called()
    saved = result.all_messages()
    choice = DeferredToolResults(
        approvals={"send": ToolApproved()},
        metadata={"send": {"approval_id": "approval-1"}},
    )
    resumed = await agent.run(
        conversation=result.conversation, deferred_tool_results=choice, metadata=metadata
    )
    assert resumed.output == "Sent."
    api.execute.assert_called_once_with(
        "SLACK_SEND_MESSAGE",
        arguments={"text": "hello"},
        user_id="owner",
        connected_account_id="account",
        version="20261118_00",
    )
    connections.assert_current_binding.assert_called_with(connections.binding, "slack")
    with pytest.raises(Exception, match="already submitted"):
        await agent.run(message_history=saved, deferred_tool_results=choice, metadata=metadata)
    assert api.execute.call_count == 1
