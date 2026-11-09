"""Pydantic's memory notebook, persisted locally and changed only with approval."""

from pathlib import Path

from pydantic_ai_harness import Memory
from pydantic_ai_harness.memory import SqliteMemoryStore

from bud.harness.actions import Action

MEMORY_RULES: dict[str, Action] = {
    "read_memory": "allow",
    "search_memory": "allow",
    "write_memory": "ask",
    "delete_memory": "ask",
}

GUIDANCE = """
Personalize responses using verified facts and preferences in MEMORY.md or relevant topic notes.
Record, update, or clear memories when explicitly requested by the user.
Memory modifications require user approval through the system's write flow.
"""


def build_memory(path: Path, *, namespace: str) -> Memory:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Memory(
        store=SqliteMemoryStore(database=path),
        namespace=namespace,
        guidance=GUIDANCE,
        injection_errors="raise",
    )
