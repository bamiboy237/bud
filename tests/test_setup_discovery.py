"""A discovered app gets a setup card like the shipped ones; a bad slug does not."""

from typing import cast

from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from bud.composio.binding import ConnectionError, valid_toolkit
from bud.composio.connection import Connections
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.setup import (
    SetupEntry,
    SetupInitiation,
    SetupJournal,
    SetupRegistry,
    SetupStatus,
    setup_registry,
    setup_tool,
)


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


def test_failed_tool_refresh_keeps_the_card_and_retries_without_reconnecting(tmp_path):
    journal = SetupJournal()
    journal.bind(Hub(EventLog(tmp_path / "events.db")))
    initiated = []
    registry = SetupRegistry()
    registry.register(
        "slack",
        SetupEntry(
            label="Slack",
            kind="url",
            initiate=lambda: (
                initiated.append(True)
                or SetupInitiation(kind="url", url="https://example.test/slack")
            ),
            status=lambda: SetupStatus(state="connected"),
        ),
    )
    attempts = []

    def refresh():
        attempts.append(True)
        if len(attempts) == 1:
            raise ConnectionError("Could not load tools. Try again.")

    setup = setup_tool(registry, journal, refresh=refresh)

    def ctx(call_id):
        return RunContext(
            deps=None,
            model=TestModel(),
            usage=RunUsage(),
            run_id="run",
            tool_call_id=call_id,
            metadata={"bud_session_id": "session"},
        )

    assert setup(ctx("one"), "slack")["status"] == "waiting"
    result = setup(ctx("two"), "slack")
    assert result["status"] == "failed"
    assert result["reason"] == "Could not load tools. Try again."
    assert "slack" in journal.pending("session")
    assert setup(ctx("three"), "slack")["status"] == "connected"
    assert journal.pending("session") == {}
    assert len(initiated) == 1
