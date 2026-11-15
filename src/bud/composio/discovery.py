"""Find Composio toolkits and tools for a request, without running anything.

Discovery is read-only: it returns names and descriptions the model can offer to the owner.
Connecting and loading tools is a separate, owner-approved step (bud.core.setup), and every
discovered write still asks (bud.harness.writes).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bud.composio.connection import Connections, safe_errors

MAX_CANDIDATES = 6
MAX_QUERY = 200
SLUG = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


@dataclass(frozen=True)
class Candidate:
    toolkit: str
    name: str
    description: str
    tools: int
    connected: bool


def valid_toolkit(slug: str) -> bool:
    return bool(SLUG.fullmatch(slug))


def search(connections: Connections, query: str) -> list[Candidate]:
    """Toolkits matching the request, most relevant first. Never raises connector text."""
    query = query.strip()
    if not query or len(query) > MAX_QUERY:
        raise ValueError(f"Describe what you need in 1 to {MAX_QUERY} characters.")
    connected = set(connections.accounts())
    with safe_errors("Could not search Composio's catalog. Check your key and connection."):
        found = connections.client().client.toolkits.list(search=query, limit=MAX_CANDIDATES)
    candidates = []
    for item in found.items:
        slug = getattr(item, "slug", "")
        if not valid_toolkit(slug):
            continue
        meta = getattr(item, "meta", None)
        candidates.append(
            Candidate(
                toolkit=slug,
                name=getattr(item, "name", slug),
                description=(getattr(meta, "description", "") or "")[:400],
                tools=int(getattr(meta, "tools_count", 0) or 0),
                connected=slug in connected,
            )
        )
    return candidates
