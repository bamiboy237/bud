"""Fingerprint packaged source and framework versions to guard durable replay.

Comment and docstring edits also change the fingerprint and can block pending runs.
"""

from hashlib import sha256
from importlib.metadata import version
from pathlib import Path


def replay_version() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".toml", ".md", ".json"}:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    for package in ("dbos", "pydantic-ai-slim", "pydantic-ai-harness", "pydantic-monty"):
        digest.update(f"{package}:{version(package)}".encode())
    return digest.hexdigest()
