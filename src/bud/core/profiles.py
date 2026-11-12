"""Approval profiles: one mode knob that shifts how often bud asks.

Three modes (design fork B, step A; `notes/approval-levels.md`):

- ``strict``: today's behavior — every write asks.
- ``balanced``: workspace, memory, and simulated-world writes auto-run
  (journaled); account writes and skill installs still ask. With the opt-in decision
  model on (``DECISION_AUTO_ALLOW``), reversible writes become ``decide`` instead: the
  model scores each call, a high score auto-runs it, anything else asks. Every other
  self-hosted write (deletes, unclassified tools) then asks outright.
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
from bud.harness.writes import decidable_tools, self_hosted_writes
from bud.skills import RULES as SKILL_RULES

Mode = Literal["strict", "balanced", "autonomous"]
MODES: tuple[Mode, ...] = ("strict", "balanced", "autonomous")
# The learner's daily default, decided 2026-11-12 (notes/approval-levels.md).
DEFAULT_MODE: Mode = "autonomous"

# Writes that leave bud's own workspace (files, notes, sim world state) or
# the simulated world. Journaled when they auto-run; never irreversible.
# Sending is never automatic in any mode: the owner always sees the full draft
# (recipients, subject, body) on an approval card before anything goes out.
SEND_TOOLS = frozenset({"send_email"})
_SELF_HOSTED_WRITES = self_hosted_writes()
_DECIDABLE = decidable_tools()
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
    return next((mode for mode in MODES if mode == value), None)


def _overlay(mode: Mode, decisions: bool) -> dict[str, Action]:
    self_hosted, skills = _MODE_ACTIONS[mode], _SKILL_ACTIONS[mode]
    overlay: dict[str, Action] = {name: self_hosted for name in _SELF_HOSTED_WRITES}
    if mode == "balanced" and decisions:
        overlay.update(
            {name: "decide" if name in _DECIDABLE else "ask" for name in _SELF_HOSTED_WRITES}
        )
    overlay.update({name: skills for name in _SKILL_WRITES})
    return overlay


def apply_mode(policy: Policy, mode: Mode, *, decisions: bool = False) -> dict[str, Action]:
    """Shift the inspectable classes onto ``mode``, in place.

    ``decisions`` says a decision model is configured; only balanced mode uses it. Returns
    the overlay it applied, so callers (the `/mode` endpoint) can report exactly what
    changed. Real-account write tools are untouched: their base classification is ``ask``
    in every mode.
    """
    overlay = _overlay(mode, decisions)
    policy.tools.update(overlay)
    return overlay


def mode_summary(mode: Mode, *, decisions: bool = False) -> str:
    """One line for the chat: what auto-runs now, what still asks."""
    if mode == "strict":
        return "strict: every write (workspace, memory, simulated world, skills) asks."
    if mode == "balanced" and decisions:
        return (
            "balanced: reversible workspace, memory, and simulated-world writes are reviewed by a "
            "decision model and auto-run only when it is confident they match your request "
            "(otherwise they ask) · deletes and skill installs ask · sends and real "
            "Gmail/Calendar writes still ask"
        )
    self_hosted = "workspace, memory, and simulated-world writes auto-run (sends still ask)"
    skills = "skill installs auto-run" if mode == "autonomous" else "skill installs ask"
    return " · ".join([f"{mode}: {self_hosted}", skills, "real Gmail/Calendar writes still ask"])


def guidance(mode: Mode, *, decisions: bool = False) -> str:
    """The system-prompt paragraph that keeps the model's claims honest."""
    if mode == "strict":
        # The base instructions and tool descriptions already say writes ask.
        return ""
    if mode == "balanced" and decisions:
        return (
            "\nApproval mode is balanced: a reviewer checks each reversible workspace, memory, "
            "and simulated-world write against the owner's request. It either runs at once "
            "(journaled; say plainly what you did) or the owner is asked first; you cannot "
            "tell in advance, so call the tool without asking in text. Deletes, skill "
            "installs, sending email, and real Gmail/Calendar writes always ask. The reviewer "
            "judges only what the owner asked for, so make the call match their request."
        )
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
