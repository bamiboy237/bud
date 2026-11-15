"""Catalog discovery: candidates only, valid slugs only, and never a hidden write."""

from types import SimpleNamespace
from typing import cast

import pytest

from bud.composio.connection import Connections
from bud.composio.discovery import search, valid_toolkit
from bud.composio.tools import discovery_tool


class FakeConnections:
    def __init__(self, items, accounts=None, error=None):
        self.items = items
        self._accounts = accounts or {}
        self.error = error
        self.queries = []
        self.settings = SimpleNamespace(composio_api_key=object())

    def accounts(self):
        return self._accounts

    def client(self):
        outer = self

        class Toolkits:
            def list(self, *, search, limit):
                outer.queries.append((search, limit))
                if outer.error is not None:
                    raise outer.error
                return SimpleNamespace(items=outer.items)

        return SimpleNamespace(client=SimpleNamespace(toolkits=Toolkits()))


def item(slug, name, description="", tools=3):
    return SimpleNamespace(
        slug=slug, name=name, meta=SimpleNamespace(description=description, tools_count=tools)
    )


def test_search_maps_candidates_and_marks_connected():
    connections = FakeConnections(
        [item("slack", "Slack", "Send messages", 12), item("notion", "Notion")],
        accounts={"slack": "acct"},
    )
    found = search(cast(Connections, connections), "send slack messages")
    assert [candidate.toolkit for candidate in found] == ["slack", "notion"]
    assert found[0].connected is True
    assert found[1].connected is False
    assert found[0].tools == 12
    assert connections.queries == [("send slack messages", 6)]


def test_invalid_slugs_are_skipped():
    connections = FakeConnections([item("../escape", "Bad"), item("ok_app", "Fine")])
    assert [
        candidate.toolkit for candidate in search(cast(Connections, connections), "anything")
    ] == ["ok_app"]


@pytest.mark.parametrize("query", ["", "   ", "x" * 201])
def test_empty_or_overlong_query_is_refused(query):
    with pytest.raises(ValueError):
        search(cast(Connections, FakeConnections([])), query)


def test_tool_reports_connector_errors_without_leaking_them():
    tool = discovery_tool(
        cast(Connections, FakeConnections([], error=RuntimeError("secret-looking detail")))
    )
    assert tool("slack") == {
        "error": "Could not search Composio's catalog. Check your key and connection."
    }


def test_tool_returns_candidates_as_plain_data():
    tool = discovery_tool(cast(Connections, FakeConnections([item("slack", "Slack", "Chat")])))
    result = tool("chat")
    assert result["candidates"][0]["toolkit"] == "slack"
    assert result["candidates"][0]["name"] == "Slack"


@pytest.mark.parametrize(
    "slug, ok",
    [("slack", True), ("google_calendar", True), ("Bad", False), ("../x", False), ("", False)],
)
def test_toolkit_slug_rules(slug, ok):
    assert valid_toolkit(slug) is ok
