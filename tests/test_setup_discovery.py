"""A discovered app gets a setup card like the shipped ones; a bad slug does not."""

from typing import cast

from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from bud.composio.binding import ConnectionError, valid_toolkit
from bud.composio.connection import Connections
from bud.core.hub import Hub
from bud.core.log import EventLog
from bud.core.runner import setup_updates_note
from bud.core.setup import (
    SetupEntry,
    SetupInitiation,
    SetupJournal,
    SetupRegistry,
    SetupStatus,
    check_setup,
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


def test_owner_check_settles_the_card_without_exposing_or_reissuing_the_link(tmp_path):
    log = EventLog(tmp_path / "events.db")
    journal = SetupJournal()
    journal.bind(Hub(log))
    initiated = []
    refreshed = []
    entry = SetupEntry(
        label="GitHub",
        kind="url",
        initiate=lambda: (
            initiated.append(True)
            or SetupInitiation(kind="url", url="https://example.test/private-sign-in")
        ),
        status=lambda: SetupStatus(state="connected"),
    )
    registry = SetupRegistry()
    registry.register("github", entry)
    setup = setup_tool(registry, journal)
    ctx = RunContext(
        deps=None,
        model=TestModel(),
        usage=RunUsage(),
        run_id="run",
        tool_call_id="call",
        metadata={"bud_session_id": "session"},
    )
    result = setup(ctx, "github")
    assert "private-sign-in" not in str(result)
    assert "private-sign-in" not in str(log.read("session"))
    assert journal.pending("session")["github"].url is None
    result = check_setup(
        entry,
        journal,
        "session",
        "github",
        key="owner-check",
        refresh=lambda: refreshed.append(True),
    )
    assert result["status"] == "connected"
    assert journal.pending("session") == {}
    assert initiated == [True]
    assert refreshed == [True]


def test_automatic_checks_do_not_fill_history_with_unchanged_status(tmp_path):
    log = EventLog(tmp_path / "events.db")
    journal = SetupJournal()
    journal.bind(Hub(log))
    journal.publish(
        "session",
        "capability.setup",
        {"capability": "github", "label": "GitHub", "status": "waiting"},
        key="setup",
    )
    entry = SetupEntry(
        label="GitHub",
        kind="url",
        initiate=lambda: SetupInitiation(kind="url", url="https://example.test/sign-in"),
        status=lambda: SetupStatus(state="waiting"),
    )
    before = log.read("session")
    for index in range(5):
        result = check_setup(entry, journal, "session", "github", key=f"check-{index}")
        assert result["status"] == "waiting"
    assert log.read("session") == before


def test_model_sees_connected_and_cancelled_setups_without_sign_in_links(tmp_path):
    log = EventLog(tmp_path / "events.db")
    log.append("session", "run.queued", {"run_id": "one", "trigger": "message"})
    log.append("session", "run.started", {"run_id": "one"})
    log.append("session", "capability.status", {"capability": "slack", "status": "connected"})
    log.append("session", "capability.status", {"capability": "notion", "status": "cancelled"})
    log.append("session", "run.queued", {"run_id": "two", "trigger": "message"})
    log.append("session", "run.started", {"run_id": "two"})
    note = setup_updates_note(log.read("session"), {"run_id": "two", "trigger": "message"})
    assert '"slack": "connected"' in note
    assert '"notion": "cancelled"' in note
    assert "not a new request from the owner" in note
    assert "unless they ask" in note
    assert "https://" not in note
