"""Collapsible tool disclosure widgets for Bud TUI.

Provides calm, human-readable action cards inspired by Grok Bot and Town AI.
Eliminates all raw JSON brackets, curly braces, and internal function names
in favor of natural language summaries and clean text disclosures.
"""

from __future__ import annotations

import json
from typing import Any

from bud.tui.state import StateStore, ToolState
from bud.tui.terminal import truncate_visible
from bud.tui.views.base import View


def humanize_tool_name(tool: str) -> str:
    """Translate raw snake_case or external tool identifiers to natural English."""
    cleaned = tool.replace("GOOGLECALENDAR_", "").replace("GMAIL_", "")
    mapping: dict[str, str] = {
        "workspace_read": "Read file",
        "workspace_list": "Searched workspace files",
        "workspace_create": "Created file",
        "workspace_update": "Updated file",
        "draft_reply": "Drafted email reply",
        "send_email": "Sent email",
        "create_event": "Scheduled calendar event",
        "move_event": "Moved calendar event",
        "web_search": "Searched the web",
        "firecrawl_search": "Searched the web",
        "firecrawl_scrape": "Read web page",
    }
    if tool in mapping:
        return mapping[tool]
    if cleaned in mapping:
        return mapping[cleaned]
    # Fallback: clean snake_case to Title Case
    words = [w.capitalize() for w in cleaned.split("_") if w]
    return " ".join(words) if words else tool


def humanize_tool_action(tool: str, args: dict[str, Any], status: str) -> str:
    """Format an active or completed tool call into a natural English sentence."""
    t = tool.lower()

    if "read" in t and "path" in args:
        path = args["path"]
        return f"Reading {path}" if status == "running" else f"Read {path}"

    if "list" in t or "search" in t:
        prefix = args.get("prefix") or args.get("path") or args.get("directory") or ""
        pattern = args.get("pattern") or ""
        if pattern and prefix:
            target = f"{pattern} in {prefix}".strip()
        else:
            target = prefix or pattern or "files"

        if "web" in t:
            q = args.get("query") or args.get("q") or ""
            if status == "running":
                return f"Searching web for \"{q}\""
            return f"Searched web for \"{q}\""
        return f"Searching {target}" if status == "running" else f"Searched {target}"

    if "create" in t or "write" in t or "update" in t:
        path = args.get("path") or args.get("filename") or ""
        action = "Saving" if status == "running" else "Saved"
        return f"{action} {path}" if path else f"{humanize_tool_name(tool)}"

    if "email" in t or "draft" in t:
        recipient = args.get("to") or args.get("recipient") or ""
        if status == "running":
            return f"Drafting email to {recipient}" if recipient else "Drafting email"
        return f"Prepared email for {recipient}" if recipient else "Prepared email"

    if "event" in t or "calendar" in t:
        title = args.get("title") or args.get("summary") or ""
        if status == "running":
            return f"Scheduling \"{title}\"" if title else "Scheduling event"
        return f"Scheduled \"{title}\"" if title else "Scheduled event"

    # Default fallback
    name = humanize_tool_name(tool)
    return f"{name}…" if status == "running" else name


def format_clean_result(raw_result: str) -> list[str]:
    """Parse tool output and format it into clean text without curly braces or JSON brackets."""
    text = raw_result.strip()
    if not text:
        return []

    # Attempt to parse as structured JSON
    try:
        data = json.loads(text)
        if isinstance(data, list):
            lines: list[str] = []
            for item in data:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("path") or item.get("title") or ""
                    size = item.get("size")
                    if name:
                        size_str = f" · {size // 1024} KB" if isinstance(size, int) else ""
                        lines.append(f"  • {name}{size_str}")
                    else:
                        valid_items = [
                            f"{k}: {v}"
                            for k, v in item.items()
                            if not str(v).startswith(("{", "["))
                        ]
                        lines.append(f"  • {', '.join(valid_items)}")
                else:
                    lines.append(f"  • {item}")
            return lines
        elif isinstance(data, dict):
            lines = []
            for k, v in data.items():
                if isinstance(v, (dict, list)):
                    continue
                label = k.replace("_", " ").capitalize()
                lines.append(f"  {label}: {v}")
            return lines
    except Exception:
        pass

    # Plain text output: clean up code or raw lines
    clean_lines: list[str] = []
    for line in text.splitlines():
        trimmed = line.strip()
        if trimmed in ("{", "}", "[", "]", "{},", "[]"):
            continue
        clean_lines.append(f"  {line}")
    return clean_lines


class ToolView(View):
    """Renders a collapsible disclosure widget for a single tool execution."""

    def __init__(self, tool: ToolState, store: StateStore | None = None) -> None:
        super().__init__()
        self.tool: ToolState = tool
        self.store: StateStore | None = store

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme if self.store else None
        t = self.tool

        # Headline
        action_text = humanize_tool_action(t.tool_name, t.args, t.status)

        # Status indicator
        if t.status == "running":
            status_hint = theme.format_accent("· running") if theme else "· running"
        elif t.status == "completed":
            dur = (
                f"{int(t.duration_ms)}ms"
                if t.duration_ms is not None and t.duration_ms > 0
                else ""
            )
            raw_done = f"· done {dur}".strip()
            status_hint = theme.format_dim(raw_done) if theme else raw_done
        else:
            status_hint = theme.format_error("· failed") if theme else "· failed"

        arrow = "›"
        arrow_str = theme.format_accent(arrow) if theme else arrow
        title_str = theme.format_secondary(action_text) if theme else action_text
        tab_hint = theme.format_dim("(Tab)") if theme and not t.expanded else "(Tab)"

        collapsed_line = f"  {arrow_str} {title_str} {status_hint} {tab_hint}"
        truncated = truncate_visible(collapsed_line, width)
        lines = [truncated]

        if t.expanded:
            dim_pipe = theme.format_dim("│") if theme else "│"

            # Formatted human parameters
            if t.args:
                for k, v in t.args.items():
                    if isinstance(v, (dict, list)):
                        continue
                    label = k.replace("_", " ").capitalize()
                    val_str = str(v)
                    arg_line = f"    {dim_pipe} {label}: {val_str}"
                    lines.append(truncate_visible(arg_line, width))

            # Formatted clean result lines
            if t.result is not None:
                clean_results = format_clean_result(t.result)
                for rline in clean_results[:12]:
                    lines.append(truncate_visible(f"    {dim_pipe} {rline}", width))
                if len(clean_results) > 12:
                    more = f"    {dim_pipe}   … ({len(clean_results) - 12} more items)"
                    lines.append(truncate_visible(more, width))

        return lines


class ToolsContainerView(View):
    """Renders all currently active or recently finished tool disclosure widgets."""

    def __init__(self, store: StateStore) -> None:
        super().__init__()
        self.store: StateStore = store
        self.store.subscribe(self.mark_dirty)

    def _render(self, width: int) -> list[str]:
        if not self.store.tools:
            return []
        lines: list[str] = []
        for tool_state in self.store.tools.values():
            widget = ToolView(tool_state, self.store)
            lines.extend(widget.render(width))
        return lines
