"""Which tools are writes, and which of them a decision model may ever judge.

Three tiers, from the most to the least trusted:

- Protected: sends and real-account (Gmail, Calendar) writes. Always ask the owner, in
  every mode, and never reach a decision model.
- Decidable: an explicit allowlist of reversible self-hosted writes. In balanced mode a
  decision model scores each call, and only a high score skips the owner's approval.
- Everything else self-hosted (deletes, skill installs, any write nobody classified yet)
  asks in balanced mode: no model score stands in for the owner there.

The sets are built on demand: bud.harness.memory imports bud.harness.actions, which imports
this module, so a module-level import would be circular.
"""

# Sends and real-account writes. The prefixes cover composio tools that are added later.
PROTECTED = frozenset(
    {
        "send_email",
        "GMAIL_SEND_EMAIL",
        "GMAIL_CREATE_EMAIL_DRAFT",
        "GOOGLECALENDAR_CREATE_EVENT",
        "GOOGLECALENDAR_PATCH_EVENT",
    }
)
PROTECTED_PREFIXES = ("GMAIL_", "GOOGLECALENDAR_")

# The simulated-world writes a decision model may judge. An explicit allowlist: a new write
# tool is not on it, so it asks in balanced mode until someone decides it is reversible and
# narrow enough to judge. delete_event, delete_memory, and skill installs are deliberately
# absent: destructive or capability-changing, so the owner always decides.
DECIDABLE_WORLD_WRITES = frozenset(
    {
        "draft_reply",
        "create_event",
        "move_event",
        "rsvp_event",
        "archive_email",
        "mark_read",
        "add_task",
        "complete_task",
        "update_contact_note",
    }
)


def is_protected(name: str) -> bool:
    return name in PROTECTED or name.startswith(PROTECTED_PREFIXES)


def self_hosted_writes() -> frozenset[str]:
    """Writes to bud's own workspace, memory, and the simulated world, minus sends."""
    from bud.harness.memory import MEMORY_RULES
    from bud.workspace import WRITE_TOOLS as WORKSPACE_WRITE_TOOLS
    from bud.world.tools import WRITE_TOOLS as WORLD_WRITE_TOOLS

    memory = {name for name, action in MEMORY_RULES.items() if action == "ask"}
    return frozenset((WORLD_WRITE_TOOLS | WORKSPACE_WRITE_TOOLS | memory) - PROTECTED)


def decidable_tools() -> frozenset[str]:
    """The only tools a decision model may score."""
    from bud.workspace import WRITE_TOOLS as WORKSPACE_WRITE_TOOLS

    return (
        DECIDABLE_WORLD_WRITES | WORKSPACE_WRITE_TOOLS | {"write_memory"}
    ) & self_hosted_writes()
