"""Capability setup: the registry, the tool, and the events a setup card needs.

Whatever a task needs but bud lacks — an account, a messaging feed, a provider
sign-in — surfaces as a card the person can act on inside their conversation,
never as a `run /connect` dead end (rubric §7). The registry maps a capability
id to its initiation and status check; the model calls `setup_capability` and
the journaled `capability.*` events render as cards on every interface. A
future capability (messaging, skills source) is one `register` call — nothing
else learns about it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from bud.composio.binding import ConnectionError, valid_toolkit
from bud.composio.connection import Connections
from bud.core.events import EventType
from bud.core.hub import Hub
from bud.core.projection import PendingSetup, pending_setups

SetupState = Literal["waiting", "connected", "failed"]


class SetupInitiation(BaseModel):
    """What a person finishes: one sign-in link, or steps the chat lists."""

    kind: Literal["url", "steps"]
    url: str | None = None
    steps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> SetupInitiation:
        if self.kind == "url" and not self.url:
            raise ValueError("a url initiation needs its sign-in link")
        if self.kind == "steps" and not self.steps:
            raise ValueError("a steps initiation needs its steps")
        return self


class SetupStatus(BaseModel):
    """One card state: waiting on the person, connected, or failed with why."""

    state: SetupState
    reason: str | None = None


@dataclass(frozen=True)
class SetupEntry:
    """One capability the model can offer to set up in place."""

    label: str
    kind: Literal["url", "steps"]
    initiate: Callable[[], SetupInitiation]
    status: Callable[[], SetupStatus]


class SetupRegistry:
    """Capability id → label, initiate, and status check.

    ``fallback`` builds an entry on demand for a capability bud did not ship, such as an app
    the model discovered in Composio's catalog.
    """

    def __init__(self, fallback: Callable[[str], SetupEntry | None] | None = None) -> None:
        self._entries: dict[str, SetupEntry] = {}
        self._fallback = fallback

    def register(self, capability: str, entry: SetupEntry) -> None:
        """Add one setup-able capability; the card UX comes for free."""
        if not capability or capability != capability.strip().lower():
            raise ValueError(f"Capability ids are plain lowercase ids: {capability!r}.")
        if capability in self._entries:
            raise ValueError(f"{capability!r} is already registered.")
        self._entries[capability] = entry

    def entry(self, capability: str) -> SetupEntry | None:
        found = self._entries.get(capability)
        if found is not None:
            return found
        return self._fallback(capability) if self._fallback is not None else None

    def capabilities(self) -> list[str]:
        return sorted(self._entries)


class SetupJournal:
    """Setup events are journaled through the session log; never a side channel.

    The toolset is built into the harness before the event log and hub exist,
    so bind() is the seam that hands the hub over afterwards.
    """

    def __init__(self) -> None:
        self._hub: Hub | None = None

    def bind(self, hub: Hub) -> None:
        self._hub = hub

    @property
    def _live(self) -> Hub:
        if self._hub is None:
            raise RuntimeError("The setup journal is not bound to the event hub yet.")
        return self._hub

    def publish(self, session_id: str, type: EventType, data: dict[str, Any], *, key: str) -> None:
        self._live.publish_once(session_id, type, data, key=key)

    def show_live(self, session_id: str, type: EventType, data: dict[str, Any]) -> None:
        """Deliver data to the streams open now and store nothing."""
        self._live.broadcast(session_id, type, data)

    def pending(self, session_id: str) -> dict[str, PendingSetup]:
        """The session's setup cards that still need the person, by id."""
        events = self._live.log.read(session_id)
        return {card.capability: card for card in pending_setups(events)}


def setup_tool(
    registry: SetupRegistry, journal: SetupJournal, *, refresh: Callable[[], None] | None = None
) -> Callable[[RunContext[None], str], dict[str, Any]]:
    """The model-facing half of the machinery: one tool over the whole registry."""

    def setup_capability(ctx: RunContext[None], capability: str) -> dict[str, Any]:
        """Start a capability setup the person finishes in place, or recheck one already waiting.

        Call it the moment a missing or disconnected capability — an account,
        messaging, a provider — blocks the task, instead of telling the person
        to run a command: their chat gets an actionable card with the sign-in
        link or the steps, and the person authorizes in their browser.
        Initiation is metadata only; no approval is requested. While the card
        waits, another call rechecks and settles it. Once it is connected, the
        app's tools load for the next message; no restart is needed.

        Args:
            capability: The capability id, such as gmail, calendar, or a discovered app slug.
        """
        session = str((ctx.metadata or {}).get("bud_session_id") or "")
        if not session:
            raise RuntimeError("setup_capability runs only inside a bud session.")
        entry = registry.entry(capability)
        if entry is None:
            known = ", ".join(registry.capabilities())
            return {
                "error": f"bud has no setup for '{capability}'. Capabilities that can be "
                f"set up now: {known}. Call setup_capability with one of those, or say "
                "plainly that the capability is unavailable today."
            }
        key = f"{ctx.run_id}:setup:{ctx.tool_call_id}"
        card = journal.pending(session).get(capability)
        if card is not None and card.status == "waiting":
            # The card is up: a new call rechecks it, never hands out a second link.
            try:
                status = entry.status()
            except ConnectionError as exc:
                status = SetupStatus(state="failed", reason=str(exc))
            data: dict[str, Any] = {
                "capability": capability,
                "label": entry.label,
                "status": status.state,
            }
            if status.reason:
                data["reason"] = status.reason
            if status.state == "waiting":
                journal.publish(session, "capability.status", data, key=key)
                return {
                    "capability": capability,
                    "label": entry.label,
                    "status": "waiting",
                    "note": "The person has not finished the sign-in yet. The card keeps "
                    "its link; ask them to finish in their browser, then check again.",
                }
            if status.state == "connected":
                if refresh is not None:
                    try:
                        refresh()
                    except ConnectionError as exc:
                        return {
                            "capability": capability,
                            "label": entry.label,
                            "status": "failed",
                            "reason": str(exc),
                            "note": "Do not reconnect the account. Follow the reason above.",
                        }
                journal.publish(session, "capability.status", data, key=key)
                return {
                    "capability": capability,
                    "label": entry.label,
                    "status": "connected",
                    "note": "The capability is connected. Its tools load for the next "
                    "message. End this turn and continue when the person sends a message.",
                }
            journal.publish(session, "capability.status", data, key=key)
            return {
                "capability": capability,
                "label": entry.label,
                "status": "failed",
                "reason": status.reason,
            }
        try:
            initiation = entry.initiate()
        except ConnectionError as exc:
            reason = str(exc) or "bud could not start the sign-in. Try again."
            journal.publish(
                session,
                "capability.setup",
                {
                    "capability": capability,
                    "label": entry.label,
                    "kind": entry.kind,
                    "status": "failed",
                    "reason": reason,
                },
                key=key,
            )
            return {"error": reason, "capability": capability, "label": entry.label}
        data = {
            "capability": capability,
            "label": entry.label,
            "kind": initiation.kind,
        }
        if initiation.kind == "url":
            data["url"] = initiation.url
        else:
            data["steps"] = list(initiation.steps)
        data["status"] = "waiting"
        # The sign-in link is single-use and grants access to an account, so it never enters
        # the journal, the model's input, or status metadata. The journaled card says a link
        # is waiting; the link itself goes once, live, to the owner's open streams. A client
        # that connects later gets the card without it, and /connect issues a fresh link.
        link = data.pop("url", None)
        journal.publish(session, "capability.setup", data, key=key)
        if link is not None:
            journal.show_live(session, "capability.setup", {**data, "url": link})
        note = (
            "The person's chat shows a setup card with the sign-in link. Ask them to "
            "finish the sign-in in their browser, then check again."
            if initiation.kind == "url"
            else "The person's chat shows a setup card with the steps. Ask them to "
            "finish them, then check again."
        )
        return {
            "capability": capability,
            "label": entry.label,
            "kind": initiation.kind,
            "status": "waiting",
            "note": note,
        }

    return setup_capability


def build_setup_toolset(
    registry: SetupRegistry, journal: SetupJournal, *, refresh: Callable[[], None] | None = None
) -> FunctionToolset[None]:
    """Registration for the harness: the setup tool as one toolset.

    ``refresh`` runs when a card settles as connected, so the new app's tools load without
    a restart.
    """
    return FunctionToolset([setup_tool(registry, journal, refresh=refresh)])


# Remote statuses a card reads as still waiting on the person.
_REMOTE_WAITING = frozenset({"CONNECT_REQUESTED", "INITIATED", "INITIALIZING", "NOT_CONNECTED"})
# Every failure sentence ends in the recovery move the person can take.
_REMOTE_FAILED = {
    "FAILED": "The {label} sign-in failed. Connect {label} again to give bud access.",
    "EXPIRED": "The {label} connection expired. Connect {label} again to give bud access.",
    "REVOKED": "{label} access was revoked. Connect {label} again to give bud access.",
    "DISABLED": "The {label} account is disabled. Connect {label} again to give bud access.",
    "INACTIVE": "The {label} connection is inactive. Connect {label} again to give bud access.",
}
_UNAVAILABLE = (
    "The {label} connection could not be checked. Inspect it with /connections, "
    "then connect {label} again to give bud access."
)


def _composio_entry(connections: Connections, toolkit: str, label: str) -> SetupEntry:
    """One account capability on the existing Connections surface.

    The initiation opens the sign-in session only when actually called — the
    human authorizes in the browser, never in chat.
    """

    def initiate() -> SetupInitiation:
        return SetupInitiation(kind="url", url=connections.connect(toolkit))

    def status() -> SetupStatus:
        remote = connections.statuses().get(toolkit, "NOT_CONNECTED")
        if remote == "ACTIVE":
            return SetupStatus(state="connected")
        if remote in _REMOTE_WAITING:
            return SetupStatus(state="waiting")
        reason = (_REMOTE_FAILED.get(remote, _UNAVAILABLE)).format(label=label)
        return SetupStatus(state="failed", reason=reason)

    return SetupEntry(label=label, kind="url", initiate=initiate, status=status)


def _discovered_entry(connections: Connections, slug: str) -> SetupEntry | None:
    """One app from Composio's catalog, set up exactly like the shipped ones."""
    if not valid_toolkit(slug):
        return None
    return _composio_entry(connections, slug, slug.replace("_", " ").title())


def setup_registry(connections: Connections) -> SetupRegistry:
    """The capabilities bud can set up today, plus any app the model discovers."""
    registry = SetupRegistry(fallback=lambda slug: _discovered_entry(connections, slug))
    registry.register("gmail", _composio_entry(connections, "gmail", "Gmail"))
    registry.register("calendar", _composio_entry(connections, "googlecalendar", "Calendar"))
    return registry
