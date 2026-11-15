"""A discovered app gets a setup card like the shipped ones; a bad slug does not."""

from typing import cast

from bud.composio.binding import valid_toolkit
from bud.composio.connection import Connections
from bud.core.setup import setup_registry


class FakeConnections:
    def accounts(self):
        return {}

    def connect(self, toolkit):
        return f"https://example.test/{toolkit}"

    def statuses(self):
        return {}


def test_discovered_toolkit_gets_a_setup_card():
    registry = setup_registry(cast(Connections, FakeConnections()))
    entry = registry.entry("slack")
    assert entry is not None
    assert entry.label == "Slack"
    assert entry.kind == "url"
    assert entry.initiate().url == "https://example.test/slack"


def test_shipped_capabilities_keep_their_labels():
    registry = setup_registry(cast(Connections, FakeConnections()))
    gmail, calendar = registry.entry("gmail"), registry.entry("calendar")
    assert gmail is not None and gmail.label == "Gmail"
    assert calendar is not None and calendar.label == "Calendar"


def test_invalid_capability_has_no_entry():
    registry = setup_registry(cast(Connections, FakeConnections()))
    assert registry.entry("../escape") is None
    assert registry.entry("Bad Slug") is None
    assert not valid_toolkit("Bad Slug")
