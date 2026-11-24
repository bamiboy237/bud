"""Which tools are writes, and which of them the approval reviewer may ever judge.

Three tiers, from the most to the least trusted:

- Protected: sends and real-account (Gmail, Calendar) writes. Always ask the owner, in
  every mode, and never reach the reviewer.
- Always-ask: installing or disabling a skill. A skill is third-party code or a change to
  what bud can do, so no model judges it.
- Reviewable: every other write to memory and the simulated world, each step in the
  owner's browser, and starting a reminder or a background command. In auto mode the
  reviewer (bud.harness.review) judges each call; in strict mode all of them ask.

Writes to bud's own workspace folder run without asking in auto mode, like the sandboxed
shell, which can already write the same files; strict mode asks for them too.

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


def workspace_writes() -> frozenset[str]:
    """Writes to bud's own workspace folder: never reviewed; auto mode runs them unasked."""
    from bud.workspace import WRITE_TOOLS as WORKSPACE_WRITE_TOOLS

    return frozenset(WORKSPACE_WRITE_TOOLS)


def reviewable_tools() -> frozenset[str]:
    """Every tool the reviewer may judge: self-hosted writes outside the workspace and
    browser steps that no hard rule reserves."""
    from bud.apps import CHANGES as APP_CHANGES
    from bud.browser import STEPS as BROWSER_STEPS
    from bud.core.background import STARTS as BACKGROUND_STARTS

    return frozenset(
        name
        for name in (self_hosted_writes() - workspace_writes())
        | BROWSER_STEPS
        | BACKGROUND_STARTS
        | APP_CHANGES
        if not never_reviewed(name)
    )


def consequential_tools() -> frozenset[str]:
    """Calls that reach outward, whose targets matter: sends and account writes, shell and
    background commands, browser steps, network access, reminders, and the simulated world's
    writes. The taint check (bud.harness.taint) looks at these; protected tools also match by
    prefix (is_protected). A private note or memory is not here: its text has no target."""
    from bud.browser import STEPS as BROWSER_STEPS
    from bud.core.background import STARTS as BACKGROUND_STARTS
    from bud.shell import NETWORK_COMMAND, NETWORK_TOOL
    from bud.world.tools import WRITE_TOOLS as WORLD_WRITE_TOOLS

    return (
        PROTECTED
        | {NETWORK_COMMAND, NETWORK_TOOL}
        | BROWSER_STEPS
        | BACKGROUND_STARTS
        | WORLD_WRITE_TOOLS
    )
