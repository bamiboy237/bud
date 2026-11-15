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
        client = connections.client()
        items = _lookup(client, query)
        if not items:
            # The catalog matches words, not sentences: retry with the longest words.
            for word in _words(query)[:3]:
                items = _lookup(client, word)
                if items:
                    break
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for item in items:
        slug = getattr(item, "slug", "")
        if not valid_toolkit(slug) or slug in seen:
            continue
        seen.add(slug)
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
        if len(candidates) == MAX_CANDIDATES:
            break
    return candidates


def _lookup(client, text: str):
    return client.client.toolkits.list(search=text, limit=MAX_CANDIDATES).items


def _words(query: str) -> list[str]:
    return sorted(
        {word for word in re.findall(r"[a-z0-9]+", query.lower()) if len(word) > 2},
        key=len,
        reverse=True,
    )
