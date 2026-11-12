"""Which tools are writes, and which of them the approval reviewer may ever judge.

Three tiers, from the most to the least trusted:

- Protected: sends and real-account (Gmail, Calendar) writes. Always ask the owner, in
  every mode, and never reach the reviewer.
- Always-ask: installing or disabling a skill. A skill is third-party code or a change to
  what bud can do, so no model judges it.
- Reviewable: every other write to bud's own workspace, memory, and the simulated world.
  In auto mode the reviewer (bud.harness.review) judges each call; in strict mode all of
  them ask.

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

# Third-party code and capability changes: the owner decides these in every mode.
ALWAYS_ASK = frozenset({"install_skill", "disable_skill"})


def is_protected(name: str) -> bool:
    return name in PROTECTED or name.startswith(PROTECTED_PREFIXES)


def never_reviewed(name: str) -> bool:
    """Hard rules, kept in code: these calls never reach the reviewer in any mode."""
    return is_protected(name) or name in ALWAYS_ASK


def self_hosted_writes() -> frozenset[str]:
    """Writes to bud's own workspace, memory, and the simulated world, minus sends."""
    from bud.harness.memory import MEMORY_RULES
    from bud.workspace import WRITE_TOOLS as WORKSPACE_WRITE_TOOLS
    from bud.world.tools import WRITE_TOOLS as WORLD_WRITE_TOOLS

    memory = {name for name, action in MEMORY_RULES.items() if action == "ask"}
    return frozenset((WORLD_WRITE_TOOLS | WORKSPACE_WRITE_TOOLS | memory) - PROTECTED)


def reviewable_tools() -> frozenset[str]:
    """Every tool the reviewer may judge: self-hosted writes that no hard rule reserves."""
    return frozenset(name for name in self_hosted_writes() if not never_reviewed(name))
