"""The account binding fence: a new app is additive, a removal or replacement is not."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest
from composio import Composio

from bud.composio.binding import BindingStore, ConnectionError
from bud.composio.connection import Connections
from bud.config import Settings


class FakeComposio:
    """Composio's connected-account reads, keyed by the ids the store holds."""

    def __init__(self, owner: str, accounts: dict[str, tuple[str, str]]) -> None:
        self.owner = owner
        self.accounts = accounts

    def get(self, *, nanoid: str):
        toolkit, status = self.accounts[nanoid]
        return SimpleNamespace(
            id=nanoid,
            user_id=self.owner,
            toolkit=SimpleNamespace(slug=toolkit),
            is_disabled=False,
            status=status,
            state=SimpleNamespace(
                model_dump=lambda: {"val": {"redirect_url": "https://example.test/sign-in"}}
            ),
        )


@pytest.fixture
def connections(tmp_path):
    settings = cast(Settings, SimpleNamespace(data_dir=tmp_path, composio_api_key=None))
    found = Connections(settings)
    remote: dict[str, tuple[str, str]] = {}
    found.client = cast(
        Callable[[], Composio],
        lambda: SimpleNamespace(connected_accounts=FakeComposio(found.user_id, remote)),
    )
    return found, remote


def connect(connections, remote, toolkit: str, account_id: str) -> None:
    """A finished sign-in: intent, then the verified reference, then a refresh."""
    attempt = connections.store.begin_connect(toolkit)
    connections.store.save_pending(attempt, toolkit, account_id)
    remote[account_id] = (toolkit, "ACTIVE")
    connections.refresh()


def test_a_new_app_does_not_fence_the_session(connections):
    found, remote = connections
    connect(found, remote, "gmail", "acct-gmail")
    binding = found.capture_binding()
    scope = found.store.session_scope(binding)

    connect(found, remote, "slack", "acct-slack")

    assert found.accounts() == {"gmail": "acct-gmail", "slack": "acct-slack"}
    assert not found.restart_required()
    assert found.store.session_scope(found.capture_binding()) == scope
    reopened = BindingStore(found.path)
    assert reopened.session_scope(reopened.snapshot()[0]) == scope
    # The tools built before Slack existed still hold a live gmail account.
    found.assert_current_binding(binding, "gmail")


def test_recovering_a_pending_link_preserves_the_chat_and_account_binding(connections):
    found, remote = connections
    found.capture_binding()
    attempt = found.store.begin_connect("github")
    found.store.save_pending(attempt, "github", "acct-github")
    remote["acct-github"] = ("github", "INITIATED")
    before = found.store.snapshot()
    assert found.pending_url("github") == "https://example.test/sign-in"
    assert found.store.snapshot() == before
    assert not found.restart_required()
    remote["acct-github"] = ("github", "ACTIVE")
    with pytest.raises(ConnectionError, match="already connected"):
        found.pending_url("github")
    found.refresh()
    assert found.accounts()["github"] == "acct-github"
    assert not found.restart_required()


def test_retrying_expired_first_sign_in_preserves_the_active_binding(connections, monkeypatch):
    found, remote = connections
    binding = found.capture_binding()
    scope = found.store.session_scope(binding)
    attempt = found.store.begin_connect("github")
    found.store.save_pending(attempt, "github", "expired-github")
    remote["expired-github"] = ("github", "EXPIRED")
    assert found.pending_url("github") is None
    request = SimpleNamespace(id="new-github", redirect_url="https://example.test/fresh-sign-in")
    client = SimpleNamespace(
        connected_accounts=FakeComposio(found.user_id, remote),
        create=lambda **kwargs: SimpleNamespace(authorize=lambda toolkit: request),
    )
    monkeypatch.setattr(found, "client", lambda: client)

    assert found.connect("github") == request.redirect_url
    assert found.store.snapshot()[0] == binding
    assert not found.restart_required()
    assert found.store.session_scope(found.capture_binding()) == scope


def test_pending_link_checks_the_owner_before_returning_it(connections, monkeypatch):
    found, remote = connections
    attempt = found.store.begin_connect("github")
    found.store.save_pending(attempt, "github", "acct-github")
    remote["acct-github"] = ("github", "INITIATED")
    monkeypatch.setattr(
        found,
        "client",
        lambda: SimpleNamespace(connected_accounts=FakeComposio("another-owner", remote)),
    )
    with pytest.raises(ConnectionError, match="owner or toolkit changed"):
        found.pending_url("github")


def test_resuming_an_unfinished_setup_does_not_replace_the_active_accounts(connections):
    found, remote = connections
    connect(found, remote, "googlecalendar", "acct-calendar")
    binding = found.capture_binding()
    found.store.begin_connect("github")
    retry = found.store.begin_connect("github")
    assert retry.binding == binding
    assert not found.restart_required()
    found.store.save_pending(retry, "github", "acct-github")
    remote["acct-github"] = ("github", "ACTIVE")
    found.refresh()
    assert not found.restart_required()
    found.assert_current_binding(binding, "googlecalendar")


def test_resuming_cannot_bypass_the_fence_for_an_active_account(connections):
    found, remote = connections
    connect(found, remote, "gmail", "acct-gmail")
    binding = found.capture_binding()
    found.store.begin_connect("gmail")
    assert found.restart_required()
    with pytest.raises(ConnectionError):
        found.assert_current_binding(binding, "gmail")


def test_cancelled_sign_in_is_not_adopted_when_the_browser_finishes(connections):
    found, remote = connections
    connect(found, remote, "googlecalendar", "acct-calendar")
    binding = found.capture_binding()
    attempt = found.store.begin_connect("slack")
    found.store.save_pending(attempt, "slack", "acct-slack")
    remote["acct-slack"] = ("slack", "ACTIVE")
    assert found.store.cancel_connect("slack")
    assert not found.store.cancel_connect("slack")
    found.refresh()
    assert found.accounts() == {"googlecalendar": "acct-calendar"}
    assert not found.restart_required()
    found.assert_current_binding(binding, "googlecalendar")


def test_replacing_an_app_keeps_the_fence(connections):
    found, remote = connections
    connect(found, remote, "gmail", "acct-gmail")
    binding = found.capture_binding()

    connect(found, remote, "gmail", "acct-gmail-2")

    assert found.restart_required()
    with pytest.raises(ConnectionError):
        found.assert_current_binding(binding, "gmail")


def test_reconnecting_keeps_the_chat_and_the_dispatch_fence(connections):
    found, remote = connections
    connect(found, remote, "gmail", "acct-gmail")
    binding = found.capture_binding()
    old_scope = found.store.session_scope(binding)

    connect(found, remote, "gmail", "acct-gmail")

    assert found.restart_required()
    current = found.store.snapshot()[0]
    # The same chat and memory carry over the reconnect...
    assert found.store.session_scope(current) == old_scope
    # ...but tools built for the old binding still refuse to dispatch.
    with pytest.raises(ConnectionError):
        found.assert_current_binding(binding, "gmail")


def test_removing_an_app_keeps_the_fence(connections):
    found, remote = connections
    connect(found, remote, "gmail", "acct-gmail")
    binding = found.capture_binding()

    found.store.retire(binding, disconnected="gmail")
    found.refresh()

    assert found.restart_required()
    with pytest.raises(ConnectionError):
        found.assert_current_binding(binding, "gmail")


def test_restarting_an_unfinished_first_sign_in_keeps_the_chat(connections):
    """An app that was never active cannot redirect an approval, so a fresh sign-in link
    for it, like Cancel, leaves the binding and every open chat alone."""
    found, remote = connections
    connect(found, remote, "googlecalendar", "acct-calendar")
    binding = found.capture_binding()
    first = found.store.begin_connect("gmail")
    found.store.save_pending(first, "gmail", "acct-gmail-old")

    second = found.store.begin_connect("gmail")
    assert second.binding == binding
    assert not found.restart_required()
    found.store.save_pending(second, "gmail", "acct-gmail")
    remote["acct-gmail"] = ("gmail", "ACTIVE")
    remote["acct-gmail-old"] = ("gmail", "ACTIVE")  # the old link finishing late
    found.refresh()

    assert found.accounts() == {"googlecalendar": "acct-calendar", "gmail": "acct-gmail"}
    assert not found.restart_required()
    found.assert_current_binding(binding, "googlecalendar")
    found.assert_current_binding(found.capture_binding(), "gmail")


def test_an_unverifiable_pending_sign_in_does_not_fence_the_chat(connections):
    found, remote = connections
    connect(found, remote, "googlecalendar", "acct-calendar")
    binding = found.capture_binding()
    attempt = found.store.begin_connect("gmail")
    found.store.save_pending(attempt, "gmail", "acct-gone")  # Composio no longer knows it

    statuses = found.refresh()[1]

    assert statuses["gmail"] == "UNAVAILABLE"
    assert statuses["googlecalendar"] == "ACTIVE"
    assert not found.restart_required()
    found.assert_current_binding(binding, "googlecalendar")
    # Dropped, so the next check does not ask Composio about it again.
    assert "gmail" not in found.store.snapshot()[1]
    assert found.refresh()[1]["gmail"] == "NOT_CONNECTED"


def test_a_rejected_key_is_not_hidden_behind_a_pending_sign_in(connections, monkeypatch):
    found, remote = connections
    attempt = found.store.begin_connect("gmail")
    found.store.save_pending(attempt, "gmail", "acct-gmail")

    class Rejected(Exception):
        status_code = 401

    def get(*, nanoid):
        raise Rejected()

    monkeypatch.setattr(
        found,
        "client",
        lambda: type("C", (), {"connected_accounts": type("A", (), {"get": staticmethod(get)})}),
    )
    with pytest.raises(ConnectionError, match="API key"):
        found.refresh()


def test_two_sign_ins_for_one_app_cannot_take_each_others_place(connections):
    found, remote = connections
    first = found.store.begin_connect("gmail")
    second = found.store.begin_connect("gmail")
    assert first.binding == second.binding and first.token != second.token

    with pytest.raises(ConnectionError, match="changed during sign-in"):
        found.store.save_pending(first, "gmail", "acct-1")
    found.store.save_pending(second, "gmail", "acct-2")
    assert found.store.snapshot()[1] == {"gmail": "acct-2"}


def test_a_check_keeps_an_unfinished_sign_in_open(connections):
    found, remote = connections
    connect(found, remote, "googlecalendar", "acct-calendar")
    attempt = found.store.begin_connect("gmail")
    found.refresh()  # checks while the sign-in link is still being made
    found.store.save_pending(attempt, "gmail", "acct-gmail")
    assert found.store.snapshot()[1] == {"gmail": "acct-gmail"}


def test_an_unverifiable_active_account_still_fences(connections):
    found, remote = connections
    connect(found, remote, "gmail", "acct-gmail")
    found.capture_binding()
    del remote["acct-gmail"]
    with pytest.raises(ConnectionError):
        found.refresh()
    assert found.restart_required()


def test_adopting_a_changed_binding_makes_it_current(connections):
    found, remote = connections
    connect(found, remote, "gmail", "acct-gmail")
    connect(found, remote, "googlecalendar", "acct-calendar")
    before = found.capture_binding()

    connect(found, remote, "gmail", "acct-gmail-2")  # a different Gmail account
    assert found.restart_required()
    old, new = found.adopt()
    assert old == before and new.generation > before.generation
    assert not found.restart_required()
    # The new binding dispatches; the old one stays fenced.
    found.assert_current_binding(new, "googlecalendar")
    with pytest.raises(ConnectionError):
        found.assert_current_binding(old, "gmail")


def test_cards_asked_under_an_old_account_generation_are_denied(tmp_path):
    from bud.core.events import EventType
    from bud.core.log import EventLog
    from bud.core.server import deny_stale

    log = EventLog(tmp_path / "events.db")
    events: list[tuple[EventType, dict]] = [
        ("run.queued", {"run_id": "r"}),
        (
            "approval.requested",
            {
                "approval_id": "old",
                "tool": "GOOGLE_DRIVE_DELETE_FILE",
                "review": {"account_generation": 1},
            },
        ),
        (
            "approval.requested",
            {"approval_id": "new", "tool": "GMAIL_SEND_EMAIL", "review": {"account_generation": 2}},
        ),
        ("approval.requested", {"approval_id": "legacy", "tool": "workspace_create"}),
        ("run.waiting", {"run_id": "r"}),
    ]
    for type, data in events:
        log.append("chat", type, data)
    asked: list[tuple[str, str, bool]] = []

    def decide(session_id, approval_id, approved, origin):
        asked.append((session_id, approval_id, approved))
        return True

    # An app with an underscore in its name is caught too: the stamp, not the tool name,
    # decides.
    assert deny_stale(log, decide, 2) == ["old"]
    assert asked == [("chat", "old", False)]


def test_an_approval_from_another_generation_never_dispatches():
    from types import SimpleNamespace

    from bud.composio.binding import AccountBinding
    from bud.composio.tools import stale_approval

    current = AccountBinding("owner", 3, (("gmail", "acct-2"),))

    def ctx(metadata):
        return SimpleNamespace(tool_call_metadata=metadata)

    # Granted, parked, or recovered after a restart: the stamp travels with the approval.
    assert stale_approval(ctx({"review": {"account_generation": 2}}), current)  # type: ignore[arg-type]
    assert stale_approval(ctx({"account_generation": 1}), current)  # type: ignore[arg-type]
    assert not stale_approval(ctx({"review": {"account_generation": 3}}), current)  # type: ignore[arg-type]
    # A card from before the stamp keeps the binding fence alone.
    assert not stale_approval(ctx({"approval_id": "a"}), current)  # type: ignore[arg-type]
