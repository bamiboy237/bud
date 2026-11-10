"""Collapsible tool disclosure widgets for Bud TUI.

Provides calm, human-readable action cards inspired by Grok Bot and Town AI.
Eliminates all raw JSON brackets, curly braces, and internal function names
in favor of natural language summaries and clean text disclosures.
"""

from __future__ import annotations

import ast
import contextlib
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


_IGNORE_METADATA_KEYS = {
    "ok",
    "truncated",
    "revision",
    "next_offset",
    "scanned_files",
    "skipped_files",
    "status_code",
}


def _format_list_result(data: list[Any]) -> list[str]:
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
                    f"{k.replace('_', ' ').capitalize()}: {v}"
                    for k, v in item.items()
                    if not isinstance(v, (dict, list)) and k not in _IGNORE_METADATA_KEYS
                ]
                if valid_items:
                    lines.append(f"  • {', '.join(valid_items)}")
        else:
            lines.append(f"  • {item}")
    return lines


def _format_dict_result(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []

    # Check for nested collections like entries, matches, items, files, results
    for coll_key in ("entries", "matches", "items", "files", "results", "data"):
        val = data.get(coll_key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    path = item.get("path") or item.get("name") or item.get("title") or ""
                    line_num = item.get("line")
                    snippet = item.get("text") or item.get("snippet") or ""
                    size = item.get("size")
                    if path and line_num is not None and snippet:
                        lines.append(f"  • {path}:{line_num}: {snippet.strip()[:60]}")
                    elif path:
                        size_str = f" · {size // 1024} KB" if isinstance(size, int) else ""
                        lines.append(f"  • {path}{size_str}")
                    else:
                        valid_items = [
                            f"{k.replace('_', ' ').capitalize()}: {v}"
                            for k, v in item.items()
                            if not isinstance(v, (dict, list)) and k not in _IGNORE_METADATA_KEYS
                        ]
                        if valid_items:
                            lines.append(f"  • {', '.join(valid_items)}")
                else:
                    lines.append(f"  • {item}")
            if lines:
                return lines

    # Check for text/content preview (e.g. workspace_read)
    for text_key in ("text", "content", "body"):
        val = data.get(text_key)
        if isinstance(val, str) and val.strip():
            text_lines = val.strip().splitlines()
            for tline in text_lines[:12]:
                lines.append(f"  {tline}")
            if len(text_lines) > 12:
                lines.append(f"  … ({len(text_lines) - 12} more lines)")
            return lines

    # Generic key-values
    for k, v in data.items():
        if k in _IGNORE_METADATA_KEYS or isinstance(v, (dict, list)):
            continue
        label = k.replace("_", " ").capitalize()
        val_str = str(v).replace("{", "").replace("}", "")
        lines.append(f"  {label}: {val_str}")

    return lines


def format_clean_result(raw_result: str) -> list[str]:
    """Parse tool output and format it into clean text without curly braces or JSON brackets."""
    text = raw_result.strip()
    if not text:
        return []

    # Attempt to parse as structured JSON or Python literal dict/list
    data: Any = None
    try:
        data = json.loads(text)
    except Exception:
        with contextlib.suppress(Exception):
            data = ast.literal_eval(text)

    if isinstance(data, list):
        return _format_list_result(data)
    elif isinstance(data, dict):
        return _format_dict_result(data)

    # Plain text output: clean up code or raw lines
    clean_lines: list[str] = []
    for line in text.splitlines():
        trimmed = line.strip()
        if trimmed in ("{", "}", "[", "]", "{},", "[]"):
            continue
        if trimmed.startswith("{") and trimmed.endswith("}"):
            inner = trimmed[1:-1].strip()
            if inner:
                clean_lines.append(f"  {inner}")
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
                flat_args: list[tuple[str, str]] = []
                for k, v in t.args.items():
                    if isinstance(v, dict):
                        for sub_k, sub_v in v.items():
                            if not isinstance(sub_v, (dict, list)):
                                flat_args.append((f"{k} {sub_k}", str(sub_v)))
                    elif not isinstance(v, list):
                        flat_args.append((k, str(v)))

                for key_name, val in flat_args:
                    label = key_name.replace("_", " ").capitalize()
                    val_str = val.replace("{", "").replace("}", "")
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
