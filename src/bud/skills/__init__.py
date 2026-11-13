"""Versioned task methods, loaded through Pydantic's native Skills capability."""

import asyncio
import hashlib
import json
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness import Skills

from bud.skills.installer import Installer, SkillError

NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION = re.compile(r"^[a-f0-9]{64}$")
RULES = {
    "load_capability": "allow",
    "list_skills": "allow",
    "find_skills": "allow",
    "prepare_skill": "allow",
    "read_skill_reference": "allow",
    "read_prepared_skill": "allow",
    "install_skill": "ask",
    "disable_skill": "ask",
}
GUIDANCE = """Discover available task capabilities using `list_skills`.
Activate relevant methods for the task using `load_capability`.
Follow instructions in loaded skills alongside available tools.
"""


def valid_name(name: str) -> None:
    if len(name) > 64 or not NAME.fullmatch(name):
        raise SkillError("Use a skill name of lowercase letters/numbers separated by hyphens.")


def package_files(directory: Path) -> dict[str, bytes]:
    files = {}
    size = 0
    entries = []
    for path in directory.rglob("*"):
        entries.append(path)
        if len(entries) > 256:
            raise SkillError("Skill package has too many directories or files.")
    for path in sorted(entries):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise SkillError("Skill packages cannot contain symlinks or special files.")
        if path.is_file():
            if path.stat().st_size > 65_536:
                raise SkillError("A skill file exceeds 64 KiB.")
            content = path.read_bytes()
            size += len(content)
            if size > 524_288 or len(files) >= 64:
                raise SkillError("Skill package exceeds 512 KiB or 64 files.")
            files[path.relative_to(directory).as_posix()] = content
    if "SKILL.md" not in files:
        raise SkillError("Skill package has no SKILL.md.")
    return files


def package_version(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(json.dumps([name, len(content)]).encode())
        digest.update(content)
    return digest.hexdigest()


FOUND = re.compile(r"^([\w.-]+/[\w.-]+)@([\w.:-]+)\s+(\d+)\s+installs?\s*$")
MAX_FOUND = 10


def search_results(output: str) -> dict:
    """The registry CLI prints a block-letter banner and an `npx` install hint; the model
    needs neither (bud installs through prepare_skill and approval). Keep the listings."""
    lines = output.splitlines()
    found = []
    for index, line in enumerate(lines):
        match = FOUND.match(line.strip())
        if match is None:
            continue
        url = lines[index + 1].strip().lstrip("└ ").strip() if index + 1 < len(lines) else ""
        found.append(
            {
                "source": match[1],
                "name": match[2],
                "installs": int(match[3]),
                "url": url if url.startswith("https://skills.sh/") else None,
            }
        )
    if not found:
        return {"results": [], "note": "No matching skills were listed."}
    return {"results": found[:MAX_FOUND], "next": "Use prepare_skill(source, name) to inspect one."}


class SkillStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.objects = self.root / "objects"
        self.objects.mkdir(exist_ok=True, mode=0o700)
        self.path = self.root / "skills.db"
        self.installer = Installer(self.root / "tooling")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS active(
                    name TEXT PRIMARY KEY, version TEXT NOT NULL,
                    source TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS prepared(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL,
                    source TEXT NOT NULL, revision TEXT NOT NULL, previous TEXT
                );
                CREATE TABLE IF NOT EXISTS claims(
                    approval_id TEXT PRIMARY KEY, operation TEXT NOT NULL
                );
            """)
        builtin = Path(str(resources.files("bud.skills").joinpath("builtin")))
        for directory in sorted(builtin.iterdir()):
            if not directory.is_dir():
                continue
            version = self.capture(directory, directory.name)
            with self.connect() as db:
                db.execute(
                    "INSERT INTO active(name,version,source) VALUES(?,?,'builtin') "
                    "ON CONFLICT(name) DO UPDATE SET version=excluded.version "
                    "WHERE active.source='builtin'",
                    (directory.name, version),
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            yield db

    def capture(self, directory: Path, name: str) -> str:
        valid_name(name)
        if directory.is_symlink() or not directory.is_dir():
            raise SkillError("Expected a regular skill directory.")
        files = package_files(directory)
        version = package_version(files)
        target = self.objects / version
        if not target.exists():
            with tempfile.TemporaryDirectory(dir=self.objects) as temporary:
                content = Path(temporary) / "package"
                for relative, data in files.items():
                    destination = content / name / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                try:
                    content.rename(target)
                except OSError:
                    if not target.is_dir():
                        raise
        return version

    def snapshot(self) -> dict[str, str]:
        with self.connect() as db:
            return dict(
                db.execute(
                    "SELECT name,version FROM active WHERE enabled=1 ORDER BY name"
                ).fetchall()
            )

    def check(self, name: str, version: str) -> Path:
        valid_name(name)
        if not VERSION.fullmatch(version):
            raise SkillError("Invalid skill version.")
        directory = self.objects / version / name
        if not directory.is_dir() or directory.is_symlink():
            raise SkillError(f"The saved version of {name} is unavailable.")
        if package_version(package_files(directory)) != version:
            raise SkillError(f"The saved version of {name} was modified.")
        return directory

    def list_skills(self) -> list[dict]:
        """List installed skills, active versions, sources, and enabled status."""
        with self.connect() as db:
            return [
                dict(zip(("name", "version", "source", "enabled"), row, strict=True))
                for row in db.execute(
                    "SELECT name,version,source,enabled FROM active ORDER BY name"
                )
            ]

    async def find_skills(self, query: str) -> dict:
        """Search the public ecosystem registry for reusable skills using a generic task phrase.

        Query using general capability descriptions. Omit private messages, personal
        details, and credentials.
        """
        if not 2 <= len(query.strip()) <= 200 or query.startswith("-"):
            raise SkillError("Use a search phrase between 2 and 200 characters.")
        return search_results(await self.installer.run(["find", query], self.root))

    async def prepare_skill(self, source: str, name: str) -> dict:
        """Fetch and inspect a public owner/repository skill before proposing installation."""
        valid_name(name)
        with self.connect() as db:
            previous = db.execute(
                "SELECT version,source FROM active WHERE name=?", (name,)
            ).fetchone()
        if previous and previous[1] == "builtin":
            raise SkillError("Imported skills cannot replace a bundled method.")
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            directory, revision = await self.installer.prepare(source, name, Path(temporary))
            version = await asyncio.to_thread(self.capture, directory, name)
        catalog = await Skills(
            str(self.objects / version), workspace=LocalWorkspaceBackend(self.root)
        ).load()
        if catalog.skipped or len(catalog.skills) != 1 or catalog.skills[0].name != name:
            raise SkillError("The package does not define one valid matching skill.")
        definition = catalog.skills[0]
        if len(definition.description) > 1024 or len(definition.body.encode()) > 32_768:
            raise SkillError(
                "Skill descriptions must fit 1,024 characters and instructions 32 KiB."
            )
        if not definition.model_invocable:
            raise SkillError("This skill is manual-only; Bud requires model-invocable methods.")
        preview = uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO prepared VALUES(?,?,?,?,?,?)",
                (preview, name, version, source, revision, previous[0] if previous else None),
            )
        files = package_files(self.check(name, version))
        return {
            "preview_id": preview,
            "name": name,
            "version": version,
            "source": source,
            "revision": revision,
            "files": list(files),
            "instructions": files["SKILL.md"].decode(),
            "note": "Review dependencies and supporting files before proposing installation.",
        }

    def install_skill(
        self, ctx: RunContext, preview_id: str, source: str, name: str, version: str
    ) -> dict:
        """Install an exact inspected skill package previously fetched with prepare_skill.

        Requires user approval. The installed version becomes active on subsequent tasks
        while the current task retains its active methods.
        """
        approval = (ctx.tool_call_metadata or {}).get("approval_id")
        if not ctx.tool_call_approved or not approval:
            raise SkillError("Skill installation requires an exact approved proposal.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT source,previous FROM prepared "
                "WHERE id=? AND source=? AND name=? AND version=?",
                (preview_id, source, name, version),
            ).fetchone()
            if row is None:
                raise SkillError("No matching prepared skill. Prepare it before installation.")
            operation = json.dumps(["install", preview_id, name, version])
            claim = db.execute(
                "SELECT operation FROM claims WHERE approval_id=?", (approval,)
            ).fetchone()
            if claim:
                if claim[0] != operation:
                    raise SkillError("Approval already belongs to a different operation.")
                return {"installed": True, "name": name, "version": version, "applied": False}
            current = db.execute("SELECT version FROM active WHERE name=?", (name,)).fetchone()
            if (current[0] if current else None) != row[1]:
                raise SkillError("Installed version changed. Prepare a fresh proposal.")
            active = self.snapshot()
            if len(active) >= 32 and name not in active:
                raise SkillError("The library is limited to 32 enabled skills.")
            self.check(name, version)
            db.execute(
                "INSERT INTO active VALUES(?,?,?,1) ON CONFLICT(name) DO UPDATE SET "
                "version=excluded.version,source=excluded.source,enabled=1",
                (name, version, row[0]),
            )
            db.execute("INSERT INTO claims VALUES(?,?)", (approval, operation))
        return {"installed": True, "name": name, "version": version, "applied": True}

    def disable_skill(self, ctx: RunContext, name: str, version: str) -> dict:
        """Disable an installed skill version. Requires user approval."""
        approval = (ctx.tool_call_metadata or {}).get("approval_id")
        if not ctx.tool_call_approved or not approval:
            raise SkillError("Disabling a skill requires approval.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            operation = json.dumps(["disable", name, version])
            claim = db.execute(
                "SELECT operation FROM claims WHERE approval_id=?", (approval,)
            ).fetchone()
            if claim:
                if claim[0] != operation:
                    raise SkillError("Approval already belongs to a different operation.")
                return {"disabled": name, "applied": False}
            cursor = db.execute(
                "UPDATE active SET enabled=0 WHERE name=? AND version=?", (name, version)
            )
            if not cursor.rowcount:
                raise SkillError("Installed version changed; list skills before trying again.")
            db.execute("INSERT INTO claims VALUES(?,?)", (approval, operation))
        return {"disabled": name, "applied": True}

    def read_skill_reference(self, ctx: RunContext, name: str, path: str) -> str:
        """Read a UTF-8 reference document from a skill loaded in the active task."""
        versions = (ctx.metadata or {}).get("bud_skills", {})
        version = versions.get(name)
        if name not in ctx.loaded_capability_ids or version is None:
            raise SkillError("Load the skill before reading its references.")
        return self._read(name, version, path)

    def read_prepared_skill(self, preview_id: str, path: str) -> str:
        """Inspect a supporting file from an exact prepared package before installation."""
        with self.connect() as db:
            row = db.execute(
                "SELECT name,version FROM prepared WHERE id=?", (preview_id,)
            ).fetchone()
        if row is None:
            raise SkillError("No such prepared skill.")
        return self._read(row[0], row[1], path)

    def _read(self, name: str, version: str, path: str) -> str:
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or "\\" in path:
            raise SkillError("Use a relative path inside the skill.")
        directory = self.check(name, version)
        target = directory / path
        if not target.is_file() or not target.resolve().is_relative_to(directory):
            raise SkillError("No such skill reference.")
        try:
            return target.read_text()
        except UnicodeError:
            raise SkillError("This resource is not UTF-8 text.") from None

    def tools(self) -> list[Tool]:
        return [
            Tool(method)
            for method in (
                self.list_skills,
                self.find_skills,
                self.prepare_skill,
                self.install_skill,
                self.disable_skill,
                self.read_skill_reference,
                self.read_prepared_skill,
            )
        ]


@dataclass(kw_only=True)
class SkillMethods(AbstractCapability):
    store: SkillStore
    id: str = "bud_skill_methods"

    async def for_run(self, ctx: RunContext) -> AbstractCapability:
        versions = (ctx.metadata or {}).get("bud_skills")
        if versions is None:
            versions = self.store.snapshot()
        paths = [str(self.store.check(name, version).parent) for name, version in versions.items()]
        if not paths:
            return self
        return await Skills(paths, workspace=LocalWorkspaceBackend(self.store.root)).for_run(ctx)
