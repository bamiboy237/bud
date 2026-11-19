"""bud's text workspace: harness FileSystem reads, and approved writes with a claim ledger.

Reads go through the harness `FileSystem` over a read-only `LocalWorkspace`. Writes stay
here, because each approved write must happen once even when a durable run replays.

The caller provisions an existing, dedicated root and an existing parent for
`dispatch_path` (an append-only claim file outside root). Neither path may contain
symlinks. Keep both under trusted ownership; do not share root with hostile local
processes. Hidden paths and credential-like names are refused for reads and writes;
special files and hard links are refused for writes. This is not a content-secret
detector: only put material approved for the agent in root. Visible text can itself
contain private information.

Claims are fsynced BEFORE writing. A crash can consume approval without saving;
recovery requires inspection and fresh approval, never automatic redispatch. A
replay of the same claim reports success only if the file already holds the
approved bytes, so a save that finished before a crash is still recorded.
Claims must not be deleted/rolled back while approvals can replay. flock serializes
this module's writers, including across processes. External editors do not honor
that lock: revision checks detect prior edits, not a hostile edit in the final
check/rename interval. Atomic replacement prevents partial files, not that race.
Directory rename/mount attacks by local processes are outside this trust boundary.
"""

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext, Tool, ToolReturn
from pydantic_ai.capabilities import AbstractCapability, LocalWorkspace
from pydantic_ai_harness import FileSystem

READ_TOOLS = {"read_file", "list_directory", "search_files", "find_files", "file_info"}
WRITE_TOOLS = {"workspace_create", "workspace_update"}
GUIDANCE = """Manage text files in the dedicated workspace using relative paths.
read_file shows each file's content hash. workspace_update needs the hash from the latest
read. Creating and updating files require user approval.
"""

_MAX_BYTES = 128 * 1024
# Path segments that name credentials or bud's own instruction files, as whole words.
_PRIVATE_WORDS = [
    "env",
    "credential",
    "credentials",
    "secret",
    "secrets",
    "token",
    "tokens",
    "password",
    "passwords",
    "passwd",
    "shadow",
    "private",
    "id_rsa",
    "id_ed25519",
    "key",
    "pem",
    "agents",
    "claude",
    "course",
    "progress",
]
# Words a file name must not contain, for code that names files in the workspace.
PRIVATE_WORDS = frozenset(_PRIVATE_WORDS)
_PRIVATE = re.compile(rf"(^|[._\- ])({'|'.join(_PRIVATE_WORDS)})([._\- ]|$)", re.I)
# The same rule as FileSystem globs, which match the whole relative path, case-insensitively.
_DENIED = [".?*", "*/.?*"] + [
    pattern
    for word in _PRIVATE_WORDS
    for prefix in ("", "*/", "*[._ -]")
    for pattern in (f"{prefix}{word}", f"{prefix}{word}[._ -]*")
]
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class _Refused(Exception):
    pass


def _parts(path: str) -> list[str]:
    parts = path.split("/")
    if (
        len(path) > 512
        or len(parts) > 8
        or any(
            not p
            or p.startswith(".")
            or "\\" in p
            or _PRIVATE.search(p)
            or any(ord(c) < 32 or ord(c) == 127 for c in p)
            for p in parts
        )
    ):
        raise _Refused("Path is not an allowed visible relative workspace path.")
    return parts


@contextmanager
def _directory(path: Path):
    # Walk from / rather than resolve(): resolving would silently accept symlinks.
    path = path.absolute()
    if ".." in path.parts:
        raise _Refused("Configured paths must not contain traversal.")
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            new = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = new
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _parent(root: Path, parts: list[str]):
    with _directory(root) as root_fd:
        fd = os.dup(root_fd)
        try:
            for part in parts:
                new = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = new
            yield fd
        finally:
            os.close(fd)


def _regular(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _Refused("Only regular files with one hard link are allowed.")
    return info


def _read(fd: int, name: str) -> tuple[bytes, os.stat_result]:
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        before = _regular(file_fd)
        if before.st_size > _MAX_BYTES:
            raise _Refused("File exceeds the 128 KiB limit.")
        data = b""
        while len(data) <= _MAX_BYTES:
            chunk = os.read(file_fd, min(16384, _MAX_BYTES + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        after = _regular(file_fd)
        if len(data) > _MAX_BYTES or _identity(before) != _identity(after):
            raise _Refused("File changed during read or exceeds the size limit.")
        data.decode("utf-8")
        if b"\x00" in data:
            raise _Refused("Binary content is not supported.")
        return data, after
    finally:
        os.close(file_fd)


def _identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _matches(data: bytes, expected_revision: str | None) -> bool:
    """FileSystem shows the first 12 hex digits of the SHA-256; a full digest also matches."""
    return expected_revision is not None and _revision(data).startswith(expected_revision)


def _saved(path: str, data: bytes, expected_revision: str | None) -> dict[str, Any]:
    return {
        "ok": True,
        "path": path,
        "revision": _revision(data),
        "operation": "created" if expected_revision is None else "updated",
    }


def _confirm(
    root: Path, path: str, data: bytes, expected_revision: str | None
) -> dict[str, Any] | None:
    """Report a replayed save as done only when the file holds exactly the approved bytes."""
    try:
        parts = _parts(path)
        with _parent(root, parts[:-1]) as fd:
            current, _ = _read(fd, parts[-1])
    except (OSError, _Refused, UnicodeError, ValueError):
        return None
    if current != data:
        return None
    return {**_saved(path, data, expected_revision), "replayed": True}


def _safe(call) -> dict[str, Any]:
    try:
        return call()
    except _Refused as exc:
        return {"ok": False, "error": str(exc)}
    except (OSError, UnicodeError, ValueError):
        return {"ok": False, "error": "Workspace operation refused or unavailable."}


def build_tools(root: Path, dispatch_path: Path) -> list[Tool[None]]:
    """Build the two write tools; provision root/ledger parent first.

    dispatch_path is a trusted persistent file, not a model-controlled argument.
    """
    root, dispatch_path = root.absolute(), dispatch_path.absolute()
    if dispatch_path.is_relative_to(root):
        raise ValueError("Dispatch claims must live outside the workspace.")
    with _directory(root), _directory(dispatch_path.parent):
        pass

    def write(
        ctx: RunContext[None], path: str, content: str, expected_revision: str | None
    ) -> dict[str, Any] | ToolReturn:
        def run():
            metadata = ctx.tool_call_metadata
            approval = metadata.get("approval_id") if isinstance(metadata, dict) else None
            if (
                ctx.tool_call_approved is not True
                or not isinstance(approval, str)
                or not (1 <= len(approval) <= 256 and approval.strip())
            ):
                raise _Refused("An approved call with an approval_id is required.")
            parts = _parts(path)
            data = content.encode("utf-8")
            if len(data) > _MAX_BYTES or "\x00" in content:
                raise _Refused("Content must be UTF-8 text of at most 128 KiB.")
            if expected_revision is not None and not re.fullmatch(
                "[0-9a-f]{12}|[0-9a-f]{64}", expected_revision
            ):
                raise _Refused("Expected revision must be the content hash from read_file.")
            claim = _revision(approval.encode())
            fingerprint = _revision(
                json.dumps(
                    [str(root), path, expected_revision, _revision(data)], ensure_ascii=True
                ).encode()
            )
            with _directory(dispatch_path.parent) as ledger_parent:
                ledger = os.open(
                    dispatch_path.name,
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=ledger_parent,
                )
                try:
                    _regular(ledger)
                    fcntl.flock(ledger, fcntl.LOCK_EX)
                    with os.fdopen(os.dup(ledger), "r", encoding="ascii") as records:
                        for line in records:
                            record = json.loads(line)
                            if not isinstance(record, dict) or not all(
                                isinstance(record.get(key), str) for key in ("claim", "request")
                            ):
                                raise _Refused("Invalid dispatch ledger; inspect claim storage.")
                            if record["claim"] == claim:
                                if record["request"] == fingerprint:
                                    saved = _confirm(root, path, data, expected_revision)
                                    if saved is not None:
                                        return saved
                                return {
                                    "ok": False,
                                    "error": "Approval already consumed; inspect "
                                    "the file before requesting fresh approval.",
                                    "same_request": record["request"] == fingerprint,
                                }
                    record = json.dumps({"claim": claim, "request": fingerprint}) + "\n"
                    os.lseek(ledger, 0, os.SEEK_END)
                    raw = record.encode("ascii")
                    if os.write(ledger, raw) != len(raw):
                        raise _Refused("Incomplete dispatch claim; inspect claim storage.")
                    os.fsync(ledger)
                    os.fsync(ledger_parent)
                    with _parent(root, parts[:-1]) as fd:
                        previous = None
                        if expected_revision is not None:
                            old, previous = _read(fd, parts[-1])
                            if not _matches(old, expected_revision):
                                raise _Refused(
                                    "Revision conflict; read again and request approval."
                                )
                        temporary = ".bud-write-" + uuid.uuid4().hex
                        temp_fd = os.open(
                            temporary,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=fd,
                        )
                        try:
                            with os.fdopen(temp_fd, "wb") as stream:
                                stream.write(data)
                                stream.flush()
                                os.fsync(stream.fileno())
                            if previous is None:
                                # link is atomic and fails if ANY destination already exists.
                                os.link(
                                    temporary,
                                    parts[-1],
                                    src_dir_fd=fd,
                                    dst_dir_fd=fd,
                                    follow_symlinks=False,
                                )
                            else:
                                current, info = _read(fd, parts[-1])
                                if _identity(previous) != _identity(info) or (
                                    not _matches(current, expected_revision)
                                ):
                                    raise _Refused("Revision conflict; file changed before save.")
                                os.replace(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
                            os.fsync(fd)
                        finally:
                            with suppress(FileNotFoundError):
                                os.unlink(temporary, dir_fd=fd)
                    return _saved(path, data, expected_revision)
                finally:
                    os.close(ledger)

        result = _safe(run)
        if result.get("ok") is not True:
            return result
        # The runner reads saved files from metadata; the model only sees the result.
        artifact = {key: result[key] for key in ("path", "revision", "operation")}
        return ToolReturn(result, metadata={"artifact": artifact})

    def workspace_create(
        ctx: RunContext[None], path: str, content: str
    ) -> dict[str, Any] | ToolReturn:
        """Create a new text file after approval. Rejects paths that already exist."""
        return write(ctx, path, content, None)

    def workspace_update(
        ctx: RunContext[None], path: str, content: str, expected_revision: str
    ) -> dict[str, Any] | ToolReturn:
        """Replace a text file after approval, only if its content hash still matches."""
        return write(ctx, path, content, expected_revision)

    return [Tool[None](workspace_create), Tool[None](workspace_update)]


class WorkspaceFiles(FileSystem[None]):
    def get_description(self) -> str:
        return "List, search, and read text files in Bud's workspace."


def read_capabilities(root: Path, *, deferred: bool = False) -> list[AbstractCapability[None]]:
    """Harness file reading over the workspace. Its files are data, never instructions."""
    return [
        LocalWorkspace(root, read_only=True, authority="none"),
        WorkspaceFiles(
            id="workspace_files",
            defer_loading=deferred,
            tools=sorted(READ_TOOLS),
            denied_patterns=_DENIED,
        ),
    ]
