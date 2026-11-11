"""Approval profiles: one mode knob that shifts how often bud asks.

Three modes (design fork B, step A; `notes/approval-levels.md`):

- ``strict``: today's behavior — every write asks.
- ``balanced``: workspace, memory, and simulated-world writes auto-run
  (journaled); account writes and skill installs still ask.
- ``autonomous``: skill installs auto-run too (journaled).

Two invariants survive every mode:

1. Real-account writes (Gmail/Calendar sends and drafts) always ask. The
   composio write tools keep their base ``ask`` classification; this module
   never touches them, so no mode can auto-send on a real account. Standing
   rules may narrow this later, deliberately.
2. Auto-passed calls still journal: the ``ActionGateway`` records every
   dispatch decision (``action.decided``) before it runs, allowed or not.

The mode is per-owner, persisted server-side as JSON (``data_dir``), read at
boot, and switchable at runtime via ``POST /mode`` — the policy object is
shared by reference with the dispatch guard, which re-reads it on every
call, so a switch applies to the very next tool call without a restart.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from bud.harness.actions import Action, Policy
from bud.harness.memory import MEMORY_RULES
from bud.skills import RULES as SKILL_RULES
from bud.workspace import WRITE_TOOLS as WORKSPACE_WRITE_TOOLS
from bud.world.tools import WRITE_TOOLS as WORLD_WRITE_TOOLS

Mode = Literal["strict", "balanced", "autonomous"]
MODES: tuple[Mode, ...] = ("strict", "balanced", "autonomous")
# The learner's daily default, decided 2026-11-12 (notes/approval-levels.md).
DEFAULT_MODE: Mode = "autonomous"

# Writes that leave bud's own workspace (files, notes, sim world state) or
# the simulated world. Journaled when they auto-run; never irreversible.
# Sending is never automatic in any mode: the owner always sees the full draft
# (recipients, subject, body) on an approval card before anything goes out.
SEND_TOOLS = frozenset({"send_email"})
_SELF_HOSTED_WRITES = (
    (WORLD_WRITE_TOOLS - SEND_TOOLS)
    | WORKSPACE_WRITE_TOOLS
    | {name for name, action in MEMORY_RULES.items() if action == "ask"}
)
_SKILL_WRITES = {name for name, action in SKILL_RULES.items() if action == "ask"}

_MODE_ACTIONS: dict[Mode, Action] = {
    "strict": "ask",
    "balanced": "allow",
    "autonomous": "allow",
}
_SKILL_ACTIONS: dict[Mode, Action] = {
    "strict": "ask",
    "balanced": "ask",
    "autonomous": "allow",
}


def as_mode(value: object) -> Mode | None:
    """Coerce a stored or posted value into a Mode, or None if foreign."""
    return value if value in MODES else None  # type: ignore[return-value]


def _overlay(mode: Mode) -> dict[str, Action]:
    self_hosted, skills = _MODE_ACTIONS[mode], _SKILL_ACTIONS[mode]
    overlay: dict[str, Action] = {name: self_hosted for name in _SELF_HOSTED_WRITES}
    overlay.update({name: skills for name in _SKILL_WRITES})
    return overlay


def apply_mode(policy: Policy, mode: Mode) -> dict[str, Action]:
    """Shift the inspectable classes onto ``mode``, in place.

    Returns the overlay it applied, so callers (the `/mode` endpoint) can
    report exactly what changed. Real-account write tools are untouched:
    their base classification is ``ask`` in every mode.
    """
    overlay = _overlay(mode)
    policy.tools.update(overlay)
    return overlay


def mode_summary(mode: Mode) -> str:
    """One line for the chat: what auto-runs now, what still asks."""
    if mode == "strict":
        return "strict: every write (workspace, memory, simulated world, skills) asks."
    self_hosted = "workspace, memory, and simulated-world writes auto-run (sends still ask)"
    skills = "skill installs auto-run" if mode == "autonomous" else "skill installs ask"
    return " · ".join([f"{mode}: {self_hosted}", skills, "real Gmail/Calendar writes still ask"])


def guidance(mode: Mode) -> str:
    """The system-prompt paragraph that keeps the model's claims honest."""
    if mode == "strict":
        # The base instructions and tool descriptions already say writes ask.
        return ""
    auto_scope = (
        "skills activation, workspace, memory, and simulated-world writes"
        if mode == "autonomous"
        else "workspace, memory, and simulated-world writes"
    )
    return (
        f"\nApproval mode is {mode}: {auto_scope} auto-run and are journaled. "
        "When a write auto-runs, say plainly what you did. Sending email always "
        "asks, with the full draft shown first. Real Gmail/Calendar writes always ask. "
        "The mode can change at runtime (/mode); gated tool descriptions state the "
        "current gate."
    )


class ProfileStore:
    """The per-owner mode, persisted as one JSON file in the data dir."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Mode:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return DEFAULT_MODE
        mode = as_mode(raw.get("mode") if isinstance(raw, dict) else None)
        return mode if mode is not None else DEFAULT_MODE

    def save(self, mode: Mode) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"mode": mode}))
        os.replace(tmp, self.path)
