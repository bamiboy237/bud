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


def test_replacing_an_app_keeps_the_fence(connections):
    found, remote = connections
    connect(found, remote, "gmail", "acct-gmail")
    binding = found.capture_binding()

    connect(found, remote, "gmail", "acct-gmail-2")

    assert found.restart_required()
    with pytest.raises(ConnectionError):
        found.assert_current_binding(binding, "gmail")


def test_reconnecting_same_account_keeps_the_fence(connections):
    found, remote = connections
    connect(found, remote, "gmail", "acct-gmail")
    binding = found.capture_binding()
    old_scope = found.store.session_scope(binding)

    connect(found, remote, "gmail", "acct-gmail")

    assert found.restart_required()
    current = found.store.snapshot()[0]
    assert found.store.session_scope(current) != old_scope
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
