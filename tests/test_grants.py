"""Standing tool permissions: what they cover, what they can never cover, and revocation."""

import pytest

from bud.harness.actions import Policy
from bud.harness.grants import Grants
from tests.runner_support import create_note, running_bud


@pytest.fixture
def policy():
    return Policy(
        tools={
            "workspace_create": "ask",
            "archive_email": "review",
            "send_email": "ask",
            "install_skill": "ask",
            "read_thread": "allow",
        }
    )


def test_grant_turns_ask_and_review_into_allow(policy):
    assert policy.decide("workspace_create") == "ask"
    assert policy.decide("archive_email") == "review"
    policy.grants = frozenset({"workspace_create", "archive_email"})
    assert policy.decide("workspace_create") == "allow"
    assert policy.decide("archive_email") == "allow"


def test_grant_never_covers_hard_rules_or_unknown_tools(policy):
    policy.grants = frozenset({"send_email", "install_skill", "not_a_tool"})
    assert policy.decide("send_email") == "ask"
    assert policy.decide("install_skill") == "ask"
    assert policy.decide("not_a_tool") == "deny"
    assert not policy.grantable("send_email")
    assert not policy.grantable("install_skill")
    assert not policy.grantable("read_thread")
    assert policy.grantable("workspace_create")


def test_store_persists_and_refuses_hard_rules(tmp_path):
    path = tmp_path / "grants.json"
    store = Grants(path)
    assert store.tools == frozenset()
    assert store.add("archive_email")
    assert not store.add("archive_email")
    assert not store.add("send_email")
    assert not store.add("install_skill")
    assert Grants(path).tools == frozenset({"archive_email"})
    assert store.remove("archive_email")
    assert not store.remove("archive_email")
    assert Grants(path).tools == frozenset()


def test_corrupt_file_reads_as_no_grants(tmp_path):
    path = tmp_path / "grants.json"
    path.write_text("not json")
    assert Grants(path).tools == frozenset()
    path.write_text('{"tools": [1, "", "archive_email"]}')
    assert Grants(path).tools == frozenset({"archive_email"})


async def test_snapshot_marks_only_grantable_pending_tools(tmp_path):
    async with running_bud(tmp_path, lambda messages, info: create_note()) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Create note.txt", "message-1")
        await bud.settle()
        pending = bud.runner.snapshot("owner").pending_approvals[0]
        assert pending.tool == "workspace_create"
        assert pending.grantable
        bud.runner.harness.policy.grants = frozenset({"workspace_create"})
        assert not bud.runner.snapshot("owner").pending_approvals[0].grantable
