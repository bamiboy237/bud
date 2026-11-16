"""Standing permissions the owner grants from an approval card.

A grant names one tool, never one exact call: "always allow Archive email" covers every later
archive. Hard rules win: sends, real-account writes, and skill changes can never be granted
(``bud.harness.writes.never_reviewed``). The set lives in one JSON file beside the approval
mode, so it survives event-log pruning and account-scope changes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock

from bud.harness.writes import never_reviewed


class Grants:
    """The owner's standing tool permissions, persisted as one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self.tools: frozenset[str] = frozenset()
        self.load()

    def load(self) -> frozenset[str]:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.tools = frozenset()
            return self.tools
        stored = raw.get("tools") if isinstance(raw, dict) else None
        self.tools = (
            frozenset(name for name in stored if isinstance(name, str) and name.strip())
            if isinstance(stored, list)
            else frozenset()
        )
        return self.tools

    def add(self, tool: str) -> bool:
        """Record a standing permission; False for a hard-rule tool or a repeat."""
        with self._lock:
            if never_reviewed(tool) or tool in self.tools:
                return False
            self._save(self.tools | {tool})
            return True

    def remove(self, tool: str) -> bool:
        with self._lock:
            if tool not in self.tools:
                return False
            self._save(self.tools - {tool})
            return True

    def _save(self, tools: frozenset[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"tools": sorted(tools)}))
        os.replace(tmp, self.path)
        self.tools = tools
