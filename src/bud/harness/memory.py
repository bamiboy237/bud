"""Pydantic's memory notebook, persisted locally and changed only with approval."""

from pathlib import Path

from pydantic_ai_harness import ArtifactScanner, Memory
from pydantic_ai_harness.memory import SqliteMemoryStore

from bud.harness.actions import Action

MEMORY_RULES: dict[str, Action] = {
    "read_memory": "allow",
    "search_memory": "allow",
    "write_memory": "ask",
    "delete_memory": "ask",
    "inspect_preferences": "allow",
    "set_preference": "ask",
    "forget_preference": "ask",
}

GUIDANCE = """
Personalize responses using saved preferences and verified facts in MEMORY.md or relevant topic
notes. Keep `## Saved Preferences` distinct from `## Facts`: preferences apply across outputs by
scope (`global` or `skill:<name>`), while facts are historical context.
Precedence: task-specific instructions for the current output > matching `skill:<name>`
preferences > `global` preferences > historical facts or older summaries. Never restore a
superseded or cleared preference from an older summary or recalled conversation.
Use `inspect_preferences`, `set_preference`, and `forget_preference` when the person explicitly
asks to view, save, correct, or forget a preference. Use `write_memory` or `delete_memory` for
durable facts and topic notes. Unrequested style corrections in conversation are learned after
the turn on their own, so do not save them yourself unless asked to remember them.
"""


def build_memory(path: Path, *, namespace: str) -> Memory:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Memory(
        store=SqliteMemoryStore(database=path),
        namespace=namespace,
        guidance=GUIDANCE,
        injection_errors="raise",
        # Notes are model-written and loaded into later sessions: refuse planted
        # instructions, exfiltration links and invisible text on the way in and out.
        scanner=ArtifactScanner(),
    )
