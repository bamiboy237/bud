"""A connected catalog tool uses the real Composio build and approval path."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic_ai import DeferredToolRequests, RunContext, Tool
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import DeferredLoadingToolset, FunctionToolset
from pydantic_ai.usage import RunUsage

from bud.composio.binding import AccountBinding
from bud.composio.connection import Connections
from bud.composio.tools import build_toolset
from bud.harness.context import BackgroundCompaction
from tests.test_email_preview import held_call
from tests.test_live_tools import harness


async def test_catalog_tool_is_built_held_and_dispatched_once(tmp_path, monkeypatch):
    dispatched = []

    def send_message(text: str) -> str:
        dispatched.append(text)
        return "sent"

    remote = FunctionToolset([Tool(send_message, name="SLACK_SEND_MESSAGE")])
    monkeypatch.setattr("bud.composio.tools.MCPToolset", lambda *args, **kwargs: remote)

    catalog = Mock()
    catalog.list.side_effect = [
        SimpleNamespace(items=[SimpleNamespace(slug="SLACK_SEND_MESSAGE")], next_cursor="next"),
        SimpleNamespace(items=[SimpleNamespace(slug="OTHER_BAD")], next_cursor=None),
    ]
    client = Mock()
    client.client.tools = catalog
    client.create.return_value = SimpleNamespace(
        mcp=SimpleNamespace(url="https://example.test/mcp", headers={})
    )
    connections = Mock(spec=Connections)
    connections.settings = SimpleNamespace(composio_api_key=None)
    connections.binding = AccountBinding("owner", 1, (("slack", "account"),))
    connections.user_id = "owner"
    connections.client.return_value = client
    subject = harness(tmp_path)
    policy = subject.policy

    built = build_toolset(connections, {"slack": "account"}, tmp_path, policy)
    deferred = built.toolsets[0]
    assert isinstance(deferred, DeferredLoadingToolset)
    wrapped = deferred.wrapped
    assert client.create.call_args.kwargs["tools"] == {"slack": ["SLACK_SEND_MESSAGE"]}
    assert catalog.list.call_count == 2
    assert policy.decide("SLACK_SEND_MESSAGE") == "ask"
    assert not policy.grantable("SLACK_SEND_MESSAGE")
    policy.grants = frozenset({"SLACK_SEND_MESSAGE"})
    assert policy.decide("SLACK_SEND_MESSAGE") == "ask"

    subject.swap_tools(built)
    model_steps = []

    def respond(messages, info):
        names = {definition.name for definition in info.function_tools}
        model_steps.append(names)
        if len(model_steps) == 1:
            assert "search_tools" in names
            return ModelResponse(
                parts=[ToolCallPart("search_tools", {"queries": ["slack"]}, "search-1")]
            )
        assert "SLACK_SEND_MESSAGE" in names
        return ModelResponse(
            parts=[ToolCallPart("SLACK_SEND_MESSAGE", {"text": "hello"}, "send-1")]
        )

    agent = subject.agent(
        FunctionModel(respond, profile={"supported_native_tools": frozenset()}),
        lambda _: None,
        context=BackgroundCompaction(record=lambda _: None),
    )
    result = await agent.run("Send hello on Slack", metadata={"bud_request": "Send hello on Slack"})
    assert isinstance(result.output, DeferredToolRequests)
    assert result.output.approvals[0].tool_name == "SLACK_SEND_MESSAGE"
    assert dispatched == []

    requests = await held_call(wrapped, "SLACK_SEND_MESSAGE", {"text": "hello"}, policy.tools)
    assert requests.approvals[0].tool_name == "SLACK_SEND_MESSAGE"
    assert dispatched == []

    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    async with wrapped:
        tools = await wrapped.get_tools(ctx)
        approved = replace(
            ctx, tool_call_approved=True, tool_call_metadata={"approval_id": "approval-1"}
        )
        assert (
            await wrapped.call_tool(
                "SLACK_SEND_MESSAGE", {"text": "hello"}, approved, tools["SLACK_SEND_MESSAGE"]
            )
            == "sent"
        )
        with pytest.raises(Exception, match="already submitted"):
            await wrapped.call_tool(
                "SLACK_SEND_MESSAGE", {"text": "hello"}, approved, tools["SLACK_SEND_MESSAGE"]
            )
    assert dispatched == ["hello"]
