import hashlib

import pytest
from pydantic_ai import RunContext, ToolReturn, models
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from bud.workspace import build_tools


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    root = tmp_path / "workspace"
    root.mkdir()
    tools = {tool.name: tool.function for tool in build_tools(root, tmp_path / "claims.jsonl")}
    return root, tools


def approved(approval_id="approval-1"):
    return RunContext(
        deps=None,
        model=TestModel(),
        usage=RunUsage(),
        tool_call_approved=True,
        tool_call_metadata={"approval_id": approval_id},
    )


def result_of(result):
    return result.return_value if isinstance(result, ToolReturn) else result


def test_create_read_update_and_search_use_real_files(workspace):
    root, tools = workspace
    created = tools["workspace_create"](approved(), "note.md", "First café note")
    assert isinstance(created, ToolReturn)
    assert created.return_value["ok"] is True
    assert created.metadata["artifact"]["path"] == "note.md"
    assert (root / "note.md").read_text() == "First café note"

    read = tools["workspace_read"]("note.md", limit=6)
    assert read["text"] == "First "
    assert read["next_offset"] == 6
    assert read["revision"] == hashlib.sha256("First café note".encode()).hexdigest()
    assert tools["workspace_read"]("note.md", offset=6)["text"] == "café note"

    updated = tools["workspace_update"](
        approved("approval-2"), "note.md", "Second café note", read["revision"]
    )
    assert result_of(updated)["ok"] is True
    assert (root / "note.md").read_text() == "Second café note"
    assert tools["workspace_search"]("café")["matches"][0]["path"] == "note.md"


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "nested/../../outside.txt", "/outside.txt", "./note.md", r"..\outside.txt"],
)
def test_traversal_is_refused_for_reads_and_approved_writes(workspace, tmp_path, path):
    root, tools = workspace
    outside = tmp_path / "outside.txt"
    outside.write_text("outside stays intact")
    before = sorted(root.iterdir())

    assert tools["workspace_read"](path)["ok"] is False
    assert result_of(tools["workspace_create"](approved(), path, "overwrite"))["ok"] is False
    update = tools["workspace_update"](approved(), path, "overwrite", "0" * 64)
    assert result_of(update)["ok"] is False
    assert outside.read_text() == "outside stays intact"
    assert sorted(root.iterdir()) == before


@pytest.mark.parametrize("directory", ["..", "../outside", "/outside", "nested/../.."])
def test_listing_and_search_refuse_directory_traversal(workspace, directory):
    _, tools = workspace
    assert tools["workspace_list"](directory)["ok"] is False
    assert tools["workspace_search"]("outside", directory)["ok"] is False


@pytest.mark.parametrize("link_kind", ["file", "directory"])
def test_symlink_escape_cannot_read_search_or_write_outside(workspace, tmp_path, link_kind):
    root, tools = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "note.md"
    target.write_text("outside-only-marker")
    if link_kind == "file":
        (root / "link.md").symlink_to(target)
        path, directory = "link.md", ""
    else:
        (root / "linked").symlink_to(outside, target_is_directory=True)
        path, directory = "linked/note.md", "linked"

    assert tools["workspace_read"](path)["ok"] is False
    assert tools["workspace_list"]()["entries"] == []
    search = tools["workspace_search"]("outside-only-marker", directory)
    if directory:
        assert search["ok"] is False
    else:
        assert search["matches"] == []
    revision = hashlib.sha256(target.read_bytes()).hexdigest()
    assert result_of(tools["workspace_create"](approved(), path, "changed"))["ok"] is False
    update = tools["workspace_update"](approved("approval-2"), path, "changed", revision)
    assert result_of(update)["ok"] is False
    assert target.read_text() == "outside-only-marker"
    if link_kind == "directory":
        create = tools["workspace_create"](approved("approval-3"), "linked/new.md", "new")
        assert result_of(create)["ok"] is False
        assert not (outside / "new.md").exists()


def test_workspace_root_must_not_be_a_symlink(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(OSError):
        build_tools(linked, tmp_path / "claims.jsonl")


@pytest.mark.parametrize(
    "metadata, is_approved",
    [(None, False), ({"approval_id": "id"}, False), ({}, True), ({"approval_id": " "}, True)],
)
def test_writes_require_both_approval_and_nonempty_id(workspace, metadata, is_approved):
    root, tools = workspace
    ctx = RunContext(
        deps=None,
        model=TestModel(),
        usage=RunUsage(),
        tool_call_approved=is_approved,
        tool_call_metadata=metadata,
    )
    result = tools["workspace_create"](ctx, "note.md", "not approved")
    assert result["ok"] is False
    assert not (root / "note.md").exists()


def test_stale_revision_and_existing_destination_preserve_file(workspace):
    root, tools = workspace
    note = root / "note.md"
    note.write_text("original")
    revision = tools["workspace_read"]("note.md")["revision"]
    note.write_text("edited outside Bud")

    update = tools["workspace_update"](approved(), "note.md", "replacement", revision)
    assert result_of(update)["ok"] is False
    create = tools["workspace_create"](approved("approval-2"), "note.md", "replacement")
    assert result_of(create)["ok"] is False
    assert note.read_text() == "edited outside Bud"


def test_replayed_approval_does_not_redispatch_or_authorize_other_bytes(workspace):
    root, tools = workspace
    first = tools["workspace_create"](approved(), "note.md", "approved bytes")
    replay = tools["workspace_create"](approved(), "note.md", "approved bytes")
    assert result_of(first)["ok"] is True
    assert result_of(replay)["replayed"] is True
    other = tools["workspace_create"](approved(), "other.md", "approved bytes")
    assert result_of(other)["ok"] is False
    assert not (root / "other.md").exists()
    changed = tools["workspace_create"](approved(), "note.md", "different bytes")
    assert result_of(changed)["ok"] is False
    assert (root / "note.md").read_text() == "approved bytes"

    (root / "note.md").write_text("later edit")
    replay_after_edit = tools["workspace_create"](approved(), "note.md", "approved bytes")
    assert result_of(replay_after_edit)["ok"] is False
    assert (root / "note.md").read_text() == "later edit"
