"""Collapsible tool disclosure widgets and Workspace Activity cards for Bud TUI.

Provides calm, human-readable activity cards inspired by OpenCode, Grok Bot, and Town AI.
Eliminates mechanical 'Read this, do that' micro-logs in favor of:
1. Streaming transient progress while tools run (e.g. '› Checking workspace files…').
2. Consolidated Workspace Activity summary cards when settled, enclosed in hairline borders.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
from typing import Any

from bud.tui.state import StateStore, ToolState
from bud.tui.terminal import truncate_visible
from bud.tui.themes import Theme
from bud.tui.views.base import View
from bud.tui.views.box import BoxStyle, render_box


def humanize_tool_name(tool: str) -> str:
    """Translate raw snake_case or external tool identifiers to natural English."""
    cleaned = tool.replace("GOOGLECALENDAR_", "").replace("GMAIL_", "")
    mapping: dict[str, str] = {
        "workspace_read": "Read file",
        "workspace_list": "Searched workspace files",
        "workspace_create": "Saved file",
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
                return f'Searching web for "{q}"'
            return f'Searched web for "{q}"'
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
            return f'Scheduling "{title}"' if title else "Scheduling event"
        return f'Scheduled "{title}"' if title else "Scheduled event"

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


def build_activity_summary(tools: list[ToolState], theme: Theme | None = None) -> list[str]:
    """Consolidate multiple completed tool operations into a concise human outcome summary."""
    lines: list[str] = []

    # 1. Group read operations
    reads: list[str] = []
    for t in tools:
        if "read" in t.tool_name.lower() and "path" in t.args:
            reads.append(str(t.args["path"]))

    if len(reads) == 1:
        f_text = f"Checked file: {reads[0]}"
        prefix = theme.format_primary(f_text) if theme else f_text
        lines.append(prefix)
    elif len(reads) > 1:
        # Determine common directory
        common_dir = os.path.dirname(os.path.commonprefix(reads))
        dir_suffix = f" in {common_dir}" if common_dir else ""
        header = f"Checked {len(reads)} workspace files{dir_suffix}:"
        lines.append(theme.format_primary(header) if theme else header)
        for p in reads[:5]:
            display_p = p.removeprefix(common_dir).lstrip("/") if common_dir else p
            lines.append(f"  • {theme.format_secondary(display_p) if theme else display_p}")
        if len(reads) > 5:
            more_text = f"  … ({len(reads) - 5} more files)"
            lines.append(theme.format_dim(more_text) if theme else more_text)

    # 2. Group searches and listings
    for t in tools:
        name_lower = t.tool_name.lower()
        if ("list" in name_lower or "search" in name_lower) and "read" not in name_lower:
            q = t.args.get("query") or t.args.get("q") or t.args.get("pattern") or ""
            target = t.args.get("prefix") or t.args.get("path") or ""
            if "web" in name_lower:
                summary = f'Searched web for "{q}"' if q else "Searched the web"
            elif q and target:
                summary = f"Searched for '{q}' in {target}"
            elif q:
                summary = f"Searched for '{q}'"
            elif target:
                summary = f"Listed files in {target}"
            else:
                summary = "Searched workspace files"

            # Check result count if available
            count_str = ""
            if t.result:
                parsed = format_clean_result(t.result)
                bullet_count = sum(1 for line in parsed if line.strip().startswith("•"))
                if bullet_count > 0:
                    count_str = f" ({bullet_count} matches)"

            line_text = f"{summary}{count_str}"
            lines.append(theme.format_secondary(line_text) if theme else line_text)

    # 3. Saves / Writes / Updates
    for t in tools:
        name_lower = t.tool_name.lower()
        if "create" in name_lower or "write" in name_lower or "update" in name_lower:
            path = t.args.get("path") or t.args.get("filename") or ""
            action = "Updated" if "update" in name_lower else "Saved"
            desc = f"{action} {path}" if path else f"{action} file"
            lines.append(theme.format_success(desc) if theme else desc)

    # 4. External communications (Email, Slack, Calendar)
    for t in tools:
        name_lower = t.tool_name.lower()
        if "email" in name_lower or "draft" in name_lower:
            recipient = t.args.get("to") or t.args.get("recipient") or ""
            desc = f"Drafted email to {recipient}" if recipient else "Prepared email draft"
            lines.append(theme.format_primary(desc) if theme else desc)
        elif "event" in name_lower or "calendar" in name_lower:
            title = t.args.get("title") or t.args.get("summary") or ""
            desc = f'Scheduled "{title}"' if title else "Scheduled calendar event"
            lines.append(theme.format_primary(desc) if theme else desc)
        elif "slack" in name_lower:
            ch = t.args.get("channel") or ""
            desc = f"Sent Slack message to {ch}" if ch else "Sent Slack message"
            lines.append(theme.format_primary(desc) if theme else desc)

    return lines


class ToolView(View):
    """Renders a single tool execution widget with clean text disclosure."""

    def __init__(self, tool: ToolState, store: StateStore | None = None) -> None:
        super().__init__()
        self.tool: ToolState = tool
        self.store: StateStore | None = store

    def _render(self, width: int) -> list[str]:
        theme = self.store.theme if self.store else None
        t = self.tool

        action_text = humanize_tool_action(t.tool_name, t.args, t.status)

        if t.status == "running":
            status_hint = theme.format_accent("· running") if theme else "· running"
        elif t.status == "completed":
            dur = (
                f"{int(t.duration_ms)}ms" if t.duration_ms is not None and t.duration_ms > 0 else ""
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
        lines = [truncate_visible(collapsed_line, width)]

        if t.expanded:
            dim_pipe = theme.format_dim("│") if theme else "│"
            if t.args:
                for k, v in t.args.items():
                    if isinstance(v, (dict, list)):
                        continue
                    label = k.replace("_", " ").capitalize()
                    val_str = str(v).replace("{", "").replace("}", "")
                    arg_line = f"    {dim_pipe} {label}: {val_str}"
                    lines.append(truncate_visible(arg_line, width))

            if t.result is not None:
                clean_results = format_clean_result(t.result)
                for rline in clean_results[:12]:
                    lines.append(truncate_visible(f"    {dim_pipe} {rline}", width))
                if len(clean_results) > 12:
                    more = f"    {dim_pipe}   … ({len(clean_results) - 12} more items)"
                    lines.append(truncate_visible(more, width))

        return lines


class ToolsContainerView(View):
    """Renders tool operations.

    When running: displays a transient streaming progress line.
    When completed: renders a framed Workspace Activity card with unified outcome summaries.
    """

    def __init__(self, store: StateStore) -> None:
        super().__init__()
        self.store: StateStore = store
        self.store.subscribe(self.mark_dirty)

    def _render(self, width: int) -> list[str]:
        if not self.store.tools:
            return []

        tools_list = list(self.store.tools.values())
        theme = self.store.theme
        border_style: BoxStyle = getattr(self.store, "border_style", "rounded")

        # 1. If any tool is running, display transient streaming line
        running_tools = [t for t in tools_list if t.status == "running"]
        if running_tools:
            active = running_tools[-1]
            active_text = humanize_tool_action(active.tool_name, active.args, "running")
            arrow = theme.format_accent("›")
            action_styled = theme.format_secondary(active_text)
            hint = theme.format_accent("…")
            line = f"  {arrow} {action_styled}{hint}"
            return [truncate_visible(line, width)]

        # 2. If tools are completed, consolidate into a framed Workspace Activity card
        summary_lines = build_activity_summary(tools_list, theme)
        if not summary_lines:
            # Fallback if specific groupings produced nothing
            summary_lines = [
                humanize_tool_action(t.tool_name, t.args, "completed") for t in tools_list
            ]

        return render_box(
            title="Workspace Activity",
            body_lines=summary_lines,
            width=width,
            theme=theme,
            style=border_style,
        )
