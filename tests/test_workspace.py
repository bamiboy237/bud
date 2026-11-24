import hashlib
import re

import pytest
from pydantic_ai import ModelRetry, RunContext, ToolReturn, models
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.filesystem import FileSystemToolset

from bud.workspace import build_tools, read_capabilities
from tests.runner_support import reply, running_bud


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    root = tmp_path / "workspace"
    root.mkdir()
    tools = {tool.name: tool.function for tool in build_tools(root, tmp_path / "claims.jsonl")}
    return root, tools


class Reader:
    """The harness FileSystem tools bud's agent reads with, called outside a run."""

    def __init__(self, root):
        workspace, files = read_capabilities(root)
        ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())
        toolset = files.get_toolset()
        assert isinstance(toolset, FileSystemToolset)
        self.files = toolset
        # The read-only workspace the agent's runs get: no commands, files walked in Python.
        backend = workspace.get_workspace(ctx, ref=None)
        assert backend is not None
        self.workspace = backend

    async def read(self, path: str) -> str:
        text = await self.files.read_file(path, workspace=self.workspace)
        assert isinstance(text, str)
        return text

    async def listing(self, path: str = ".") -> str:
        return await self.files.list_directory(path, workspace=self.workspace)

    async def search(self, pattern: str, path: str = ".") -> str:
        return await self.files.search_files(pattern, path=path, workspace=self.workspace)


def content_hash(header: str) -> str:
    found = re.search(r"hash:([0-9a-f]{12})", header)
    assert found, header
    return found.group(1)


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


async def test_create_read_update_and_search_use_real_files(workspace):
    root, tools = workspace
    reader = Reader(root)
    created = tools["workspace_create"](approved(), "note.md", "First café note")
    assert isinstance(created, ToolReturn)
    assert created.return_value["ok"] is True
    assert created.metadata["artifact"]["path"] == "note.md"
    assert (root / "note.md").read_text() == "First café note"

    read = await reader.read("note.md")
    assert "First café note" in read
    revision = content_hash(read)
    assert hashlib.sha256("First café note".encode()).hexdigest().startswith(revision)

    updated = tools["workspace_update"](
        approved("approval-2"), "note.md", "Second café note", revision
    )
    assert result_of(updated)["ok"] is True
    assert (root / "note.md").read_text() == "Second café note"
    assert "note.md" in await reader.search("café")


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "nested/../../outside.txt", "/outside.txt", "./note.md", r"..\outside.txt"],
)
async def test_traversal_is_refused_for_approved_writes(workspace, tmp_path, path):
    root, tools = workspace
    outside = tmp_path / "outside.txt"
    outside.write_text("outside stays intact")
    before = sorted(root.iterdir())

    assert result_of(tools["workspace_create"](approved(), path, "overwrite"))["ok"] is False
    update = tools["workspace_update"](approved(), path, "overwrite", "0" * 64)
    assert result_of(update)["ok"] is False
    assert outside.read_text() == "outside stays intact"
    assert sorted(root.iterdir()) == before


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/hosts", "nested/../../outside.txt"])
async def test_reads_stay_inside_the_workspace(workspace, tmp_path, path):
    root, _ = workspace
    (tmp_path / "outside.txt").write_text("outside-only-marker")
    with pytest.raises(ModelRetry):
        await Reader(root).read(path)


@pytest.mark.parametrize("link_kind", ["file", "directory"])
async def test_symlink_escape_cannot_read_or_write_outside(workspace, tmp_path, link_kind):
    root, tools = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "note.md"
    target.write_text("outside-only-marker")
    if link_kind == "file":
        (root / "link.md").symlink_to(target)
        path = "link.md"
    else:
        (root / "linked").symlink_to(outside, target_is_directory=True)
        path = "linked/note.md"

    with pytest.raises(ModelRetry):
        await Reader(root).read(path)
    assert "outside-only-marker" not in await Reader(root).search("outside-only-marker")
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


async def test_stale_revision_and_existing_destination_preserve_file(workspace):
    root, tools = workspace
    note = root / "note.md"
    note.write_text("original")
    revision = content_hash(await Reader(root).read("note.md"))
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


async def test_general_text_files_supported(workspace):
    root, tools = workspace
    reader = Reader(root)
    files = {
        "script.py": "def hello():\n    return 'world'\n",
        "config.json": '{"setting": true, "count": 42}',
        "data.csv": "col1,col2\nval1,val2\n",
        "run.sh": "#!/bin/sh\necho ok\n",
    }
    for index, (path, content) in enumerate(files.items()):
        created = tools["workspace_create"](approved(f"create-{index}"), path, content)
        assert result_of(created)["ok"] is True
        assert (root / path).read_text() == content
        assert content.splitlines()[0] in await reader.read(path)

    assert "script.py" in await reader.search("def hello")
    listing = await reader.listing()
    assert all(path in listing for path in files)


async def test_binary_content_is_never_written_or_decoded(workspace):
    root, tools = workspace
    result = tools["workspace_create"](approved(), "binary.dat", "hello\x00world")
    assert result_of(result)["ok"] is False

    (root / "binary.bin").write_bytes(b"some\x00data")
    assert "some" not in await Reader(root).read("binary.bin")


@pytest.mark.parametrize(
    "secret_path",
    [
        ".env",
        ".env.local",
        "secrets.txt",
        "api_token.json",
        "api-key.txt",
        "id_rsa",
        "server.key",
        "cert.pem",
        "credentials.yaml",
        "sub/password.txt",
        "AGENTS.md",
    ],
)
async def test_secret_and_private_files_refused(workspace, secret_path):
    root, tools = workspace
    secret = root / secret_path
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("secret content")
    reader = Reader(root)

    with pytest.raises(ModelRetry):
        await reader.read(secret_path)
    assert "secret content" not in await reader.search("secret content")
    assert secret_path.split("/")[-1] not in await reader.listing(secret_path.rpartition("/")[0])
    create = tools["workspace_create"](approved(), secret_path, "secret content")
    assert result_of(create)["ok"] is False


async def test_hidden_folders_cannot_be_listed_or_read(workspace):
    root, _ = workspace
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret content")
    reader = Reader(root)
    with pytest.raises(ModelRetry):
        await reader.read(".git/config")
    with pytest.raises(ModelRetry):
        await reader.listing(".git")
    assert ".git" not in await reader.listing()


@pytest.mark.parametrize(
    "directory", ["secrets", "credentials", "sub/secrets", "sub/api-key", "private"]
)
async def test_private_directories_block_reads_and_searches(workspace, directory):
    root, _ = workspace
    folder = root / directory
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("private-directory-marker")
    reader = Reader(root)

    with pytest.raises(ModelRetry):
        await reader.read(f"{directory}/notes.txt")
    with pytest.raises(ModelRetry):
        await reader.listing(directory)
    assert "private-directory-marker" not in await reader.search("private-directory-marker")


@pytest.mark.parametrize("path", ["keyboard.md", "monkey.txt", "progressive.md"])
async def test_words_inside_other_words_are_not_private(workspace, path):
    root, tools = workspace
    created = tools["workspace_create"](approved(), path, "visible")
    assert result_of(created)["ok"] is True
    assert "visible" in await Reader(root).read(path)


async def test_the_agent_reads_workspace_files_through_code_mode(tmp_path):
    code = 'await read_file(path="note.md")'

    def model(messages, info):
        if any(
            getattr(part, "tool_name", None) == "run_code" for m in messages for part in m.parts
        ):
            return reply("done")
        return ModelResponse(parts=[ToolCallPart("run_code", {"code": code}, tool_call_id="c1")])

    async with running_bud(tmp_path, model) as bud:
        (bud.root / "note.md").write_text("Cedar first draft")
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "What does note.md say?", "message-1")
        await bud.settle()
        results = [e.data for e in bud.log.read("owner") if e.type == "tool.result"]

    assert [r["tool"] for r in results] == ["run_code"]
    assert results[0]["outcome"] == "success"
    assert "Cedar first draft" in results[0]["result"]


async def test_create_auto_creates_missing_parent_folders_and_update_requires_existing_parent(
    workspace,
):
    root, tools = workspace
    created = tools["workspace_create"](approved("sub-1"), "notes/2026/review.md", "Nested note")
    assert result_of(created)["ok"] is True
    assert (root / "notes" / "2026" / "review.md").read_text() == "Nested note"
    assert oct((root / "notes").stat().st_mode & 0o777) == "0o700"
    assert oct((root / "notes" / "2026").stat().st_mode & 0o777) == "0o700"

    revision = content_hash(await Reader(root).read("notes/2026/review.md"))
    missing_update = tools["workspace_update"](
        approved("sub-2"), "missing/folder/review.md", "Nope", revision
    )
    assert result_of(missing_update)["ok"] is False
    assert not (root / "missing").exists()
