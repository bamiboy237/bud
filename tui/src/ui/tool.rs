//! Quiet one-line tool activity: humanized names, the marker table, grouped successes.

use super::text::{elide, elide_words, width};
use crate::app::{ToolEntry, ToolStatus};
use crate::sanitize::clean_line;
use crate::theme::Theme;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use serde_json::{Map, Value};

pub const FOLD_COLLAPSED: &str = "⏵";
pub const FOLD_EXPANDED: &str = "⏷";
pub const CHECK: &str = "✓";
pub const BALLOT_X: &str = "✕";
pub const BALLOT_BOX: &str = "□";
pub const INERT: &str = "▪";
pub const RUNNING_ARROW: &str = "⇢";
const INSET: &str = "⎢";
const INSET_LAST: &str = "⎣";
const NOISY_ARGS: [&str; 10] = [
    "verbose",
    "user_id",
    "max_results",
    "include_payload",
    "session_id",
    "message_id",
    "approval_id",
    "origin",
    "instructions",
    "id",
];
const IGNORED_KEYS: [&str; 7] = [
    "ok",
    "truncated",
    "revision",
    "next_offset",
    "scanned_files",
    "skipped_files",
    "status_code",
];

/// The pulse of a running marker: 6 frames lit, 4 dim, one frame per 100 ms.
pub fn pulse_lit(ms: u64) -> bool {
    (ms / 100) % 10 < 6
}

fn arg<'a>(args: &'a Map<String, Value>, keys: &[&str]) -> &'a str {
    keys.iter()
        .find_map(|k| args.get(*k).and_then(Value::as_str))
        .unwrap_or("")
}

pub fn humanize_name(tool: &str) -> String {
    let cleaned = tool.replace("GOOGLECALENDAR_", "").replace("GMAIL_", "");
    let mapped = |t: &str| -> Option<&'static str> {
        Some(match t {
            "workspace_read" => "Read file",
            "workspace_list" => "Searched workspace files",
            "workspace_create" => "Saved file",
            "workspace_update" => "Updated file",
            "draft_reply" => "Drafted email reply",
            "send_email" => "Sent email",
            "create_event" => "Scheduled calendar event",
            "move_event" => "Moved calendar event",
            "web_search" | "firecrawl_search" => "Searched the web",
            "firecrawl_scrape" => "Read web page",
            "setup_capability" => "Connect capability",
            "ask_user" => "Asked question",
            _ => return None,
        })
    };
    if let Some(m) = mapped(tool).or_else(|| mapped(&cleaned)) {
        return m.to_string();
    }
    let words: Vec<String> = cleaned
        .split('_')
        .filter(|w| !w.is_empty())
        .map(|w| {
            let mut c = w.chars();
            match c.next() {
                Some(f) => f.to_uppercase().collect::<String>() + &c.as_str().to_lowercase(),
                None => String::new(),
            }
        })
        .collect();
    if words.is_empty() {
        tool.to_string()
    } else {
        words.join(" ")
    }
}

pub fn humanize_action(tool: &str, args: &Map<String, Value>, running: bool) -> String {
    let t = tool.to_lowercase();
    if t.contains("read") && args.contains_key("path") {
        let path = arg(args, &["path"]);
        return if running {
            format!("Reading {path}")
        } else {
            format!("Read {path}")
        };
    }
    if t.contains("list") || t.contains("search") {
        if t.contains("web") {
            let q = arg(args, &["query", "q"]);
            return if running {
                format!("Searching web for \"{q}\"")
            } else {
                format!("Searched web for \"{q}\"")
            };
        }
        let prefix = arg(args, &["prefix", "path", "directory"]);
        let pattern = arg(args, &["pattern"]);
        let target = if !pattern.is_empty() && !prefix.is_empty() {
            format!("{pattern} in {prefix}")
        } else if !prefix.is_empty() {
            prefix.to_string()
        } else if !pattern.is_empty() {
            pattern.to_string()
        } else {
            "files".to_string()
        };
        return if running {
            format!("Searching {target}")
        } else {
            format!("Searched {target}")
        };
    }
    if t.contains("create") || t.contains("write") || t.contains("update") {
        let path = arg(args, &["path", "filename"]);
        if !path.is_empty() {
            return if running {
                format!("Saving {path}")
            } else {
                format!("Saved {path}")
            };
        }
        return humanize_name(tool);
    }
    if t.contains("email") || t.contains("draft") {
        let to = arg(args, &["to", "recipient"]);
        return match (running, to.is_empty()) {
            (true, false) => format!("Drafting email to {to}"),
            (true, true) => "Drafting email".into(),
            (false, false) => format!("Prepared email for {to}"),
            (false, true) => "Prepared email".into(),
        };
    }
    if t.contains("event") || t.contains("calendar") {
        let title = arg(args, &["title", "summary"]);
        return match (running, title.is_empty()) {
            (true, false) => format!("Scheduling \"{title}\""),
            (true, true) => "Scheduling event".into(),
            (false, false) => format!("Scheduled \"{title}\""),
            (false, true) => "Scheduled event".into(),
        };
    }
    if t == "setup_capability" {
        let cap = arg(args, &["capability"]);
        return if running {
            format!("Connecting {cap}…")
        } else {
            format!("Connect {cap}")
        };
    }
    let name = humanize_name(tool);
    if running {
        format!("{name}…")
    } else {
        name
    }
}

/// Title of an approval card, in plain words.
pub fn action_title(tool: &str, args: &Map<String, Value>) -> String {
    let t = tool.to_lowercase();
    let quoted = |prefix: &str, title: &str, bare: &str| -> String {
        if title.is_empty() {
            bare.to_string()
        } else {
            format!("{prefix}: \"{title}\"")
        }
    };
    if t.contains("draft") {
        let to = arg(args, &["to", "recipient"]);
        return if to.is_empty() {
            "Save an email draft (not send)".into()
        } else {
            format!("Save a draft to {to} (not send)")
        };
    }
    if t.contains("email") {
        let to = arg(args, &["to", "recipient"]);
        return if to.is_empty() {
            "Send email".into()
        } else {
            format!("Send email to {to}")
        };
    }
    if t.contains("event") || t.contains("calendar") {
        let verb = if t.contains("move") || t.contains("patch") {
            "Move meeting"
        } else {
            "Schedule meeting"
        };
        return quoted(verb, arg(args, &["title", "summary"]), verb);
    }
    if t.contains("delete") || t.contains("remove") {
        let target = arg(args, &["path", "title"]);
        return if target.is_empty() {
            "Delete item".into()
        } else {
            format!("Delete {target}")
        };
    }
    if t.contains("create") {
        let p = arg(args, &["path"]);
        return if p.is_empty() {
            "Create file".into()
        } else {
            format!("Create file: {p}")
        };
    }
    if t.contains("update") {
        let p = arg(args, &["path"]);
        return if p.is_empty() {
            "Update file".into()
        } else {
            format!("Update file: {p}")
        };
    }
    let cleaned = tool
        .replace("COMPOSIO_", "")
        .replace("GOOGLECALENDAR_", "")
        .replace("GMAIL_", "")
        .replace("WORKSPACE_", "")
        .replace('_', " ");
    let mut chars = cleaned.chars();
    match chars.next() {
        Some(f) => f.to_uppercase().collect::<String>() + &chars.as_str().to_lowercase(),
        None => "Action".into(),
    }
}

fn has_body(t: &ToolEntry) -> bool {
    t.result.as_deref().is_some_and(|r| !r.trim().is_empty())
        || t.args
            .iter()
            .any(|(k, v)| !v.is_object() && !v.is_array() && !NOISY_ARGS.contains(&k.as_str()))
}

fn always_open(t: &ToolEntry) -> bool {
    t.tool == "ask_user"
}

fn marker_glyph(t: &ToolEntry) -> &'static str {
    if !has_body(t) {
        INERT
    } else if t.expanded {
        FOLD_EXPANDED
    } else {
        FOLD_COLLAPSED
    }
}

fn question_count(args: &Map<String, Value>) -> usize {
    ["question", "questions", "choices"]
        .iter()
        .find_map(|k| args.get(*k).and_then(Value::as_array).map(Vec::len))
        .unwrap_or(1)
}

/// The one-line header, without its 2-column margin.
pub fn headline(theme: &Theme, t: &ToolEntry, w: usize, pulse_ms: u64) -> Vec<Span<'static>> {
    let dim = theme.text(theme.effect_message);
    if t.status == ToolStatus::Running {
        let mut action = clean_line(&humanize_action(&t.tool, &t.args, true));
        if !action.ends_with('…') {
            action.push('…');
        }
        let action = elide_words(&action, w.saturating_sub(2));
        let marker = if pulse_lit(pulse_ms) {
            theme.fg
        } else {
            theme.dim
        };
        return vec![
            Span::styled(RUNNING_ARROW, theme.text(marker)),
            Span::styled(format!(" {action}"), theme.text(theme.effect_message)),
        ];
    }
    let open = always_open(t);
    let message = if open {
        let n = question_count(&t.args);
        if n == 1 {
            "Asked 1 question".to_string()
        } else {
            format!("Asked {n} questions")
        }
    } else {
        clean_line(&humanize_action(&t.tool, &t.args, false))
    };
    let reserve = |marker: &str, suffix: Option<&str>| -> String {
        let used = width(marker) + 1 + suffix.map_or(0, |s| 1 + width(s));
        elide_words(&message, w.saturating_sub(used))
    };
    if t.interrupted {
        let marker = if open { BALLOT_BOX } else { marker_glyph(t) };
        let text = reserve(marker, Some("(interrupted)"));
        return vec![
            Span::styled(marker, theme.muted_style()),
            Span::styled(format!(" {text}"), dim.add_modifier(Modifier::CROSSED_OUT)),
            Span::styled(" (interrupted)", theme.muted_style()),
        ];
    }
    if t.status == ToolStatus::Failed {
        let marker = if open { BALLOT_X } else { marker_glyph(t) };
        let text = reserve(marker, Some("(failed)"));
        let body = if open {
            theme.fg_style()
        } else {
            theme.dim_style()
        };
        return vec![
            Span::styled(marker, theme.dim_style()),
            Span::styled(format!(" {text}"), body),
            Span::styled(" (failed)", theme.muted_style()),
        ];
    }
    if open {
        let text = reserve(CHECK, None);
        return vec![
            Span::styled(CHECK, theme.text(theme.success)),
            Span::styled(format!(" {text}"), theme.fg_style()),
        ];
    }
    let marker = marker_glyph(t);
    let text = reserve(marker, None);
    let tint = if has_body(t) {
        theme.success
    } else {
        theme.muted
    };
    vec![
        Span::styled(marker, theme.text(tint)),
        Span::styled(format!(" {text}"), dim),
    ]
}

fn json_lines(data: &Value) -> Vec<String> {
    let mut lines = Vec::new();
    let describe = |item: &Value| -> Option<String> {
        let obj = item.as_object()?;
        let path = ["path", "name", "title"]
            .iter()
            .find_map(|k| obj.get(*k).and_then(Value::as_str));
        let size = obj.get("size").and_then(Value::as_u64);
        if let Some(p) = path {
            let line = obj.get("line").and_then(Value::as_u64);
            let snippet = ["text", "snippet"]
                .iter()
                .find_map(|k| obj.get(*k).and_then(Value::as_str));
            return Some(match (line, snippet) {
                (Some(l), Some(s)) => format!("• {p}:{l}: {}", elide(s.trim(), 60)),
                _ => match size {
                    Some(s) => format!("• {p} · {} KB", s / 1024),
                    None => format!("• {p}"),
                },
            });
        }
        let parts: Vec<String> = obj
            .iter()
            .filter(|(k, v)| !v.is_object() && !v.is_array() && !IGNORED_KEYS.contains(&k.as_str()))
            .map(|(k, v)| format!("{}: {}", label(k), scalar(v)))
            .collect();
        if parts.is_empty() {
            None
        } else {
            Some(format!("• {}", parts.join(", ")))
        }
    };
    match data {
        Value::Array(items) => {
            for item in items {
                match describe(item) {
                    Some(l) => lines.push(l),
                    None if !item.is_object() => lines.push(format!("• {}", scalar(item))),
                    None => {}
                }
            }
        }
        Value::Object(map) => {
            for key in ["entries", "matches", "items", "files", "results", "data"] {
                if let Some(Value::Array(items)) = map.get(key) {
                    for item in items {
                        if let Some(l) = describe(item) {
                            lines.push(l);
                        } else if !item.is_object() {
                            lines.push(format!("• {}", scalar(item)));
                        }
                    }
                    if !lines.is_empty() {
                        return lines;
                    }
                }
            }
            for key in ["text", "content", "body"] {
                if let Some(Value::String(s)) = map.get(key) {
                    if !s.trim().is_empty() {
                        let all: Vec<&str> = s.trim().lines().collect();
                        lines.extend(all.iter().take(12).map(|l| (*l).to_string()));
                        if all.len() > 12 {
                            lines.push(format!("… ({} more lines)", all.len() - 12));
                        }
                        return lines;
                    }
                }
            }
            for (k, v) in map {
                if IGNORED_KEYS.contains(&k.as_str()) || v.is_object() || v.is_array() {
                    continue;
                }
                lines.push(format!("{}: {}", label(k), scalar(v)));
            }
        }
        other => lines.push(scalar(other)),
    }
    lines
}

fn scalar(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn label(key: &str) -> String {
    let s = key.replace('_', " ");
    let mut c = s.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
        None => String::new(),
    }
}

/// Human lines for a tool result: structured JSON flattened, anything else shown plainly.
pub fn result_lines(raw: &str) -> Vec<String> {
    let text = raw.trim();
    if text.is_empty() {
        return Vec::new();
    }
    if let Ok(data) = serde_json::from_str::<Value>(text) {
        if data.is_object() || data.is_array() {
            return json_lines(&data);
        }
    }
    text.lines()
        .map(str::trim_end)
        .filter(|l| !matches!(l.trim(), "{" | "}" | "[" | "]" | "{}," | "[]"))
        .map(str::to_string)
        .collect()
}

/// Header plus, when open and non-empty, the bordered body.
pub fn lines(theme: &Theme, t: &ToolEntry, w: usize, pulse_ms: u64) -> Vec<Line<'static>> {
    let head = headline(theme, t, w.saturating_sub(2), pulse_ms);
    let mut spans = vec![Span::raw("  ")];
    spans.extend(head);
    let mut out = vec![Line::from(super::text::clip_spans(spans, w))];
    let settled_open = t.status != ToolStatus::Running && always_open(t);
    if !has_body(t) || !(t.expanded || settled_open) {
        return out;
    }
    let mut rows: Vec<(String, Style)> = Vec::new();
    for (k, v) in &t.args {
        if v.is_object() || v.is_array() || NOISY_ARGS.contains(&k.as_str()) {
            continue;
        }
        let val = elide(&clean_line(&scalar(v)), 120);
        rows.push((format!("{}: {val}", label(k)), theme.dim_style()));
    }
    if let Some(result) = &t.result {
        let lines = result_lines(result);
        if t.status == ToolStatus::Failed {
            let traceback = lines
                .iter()
                .any(|l| l.trim_start().starts_with("Traceback"));
            let first = if traceback {
                lines.iter().rev().find(|l| !l.trim().is_empty()).cloned()
            } else {
                lines.first().cloned()
            }
            .unwrap_or_else(|| "(no diagnostic output)".into());
            rows.push((clean_line(&first), theme.text(theme.error)));
        } else {
            for l in lines.iter().take(12) {
                rows.push((l.clone(), theme.muted_style()));
            }
            if lines.len() > 12 {
                rows.push((
                    format!("… ({} more items)", lines.len() - 12),
                    theme.muted_style(),
                ));
            }
        }
    }
    let budget = w.saturating_sub(6).max(4);
    let n = rows.len();
    for (i, (text, style)) in rows.into_iter().enumerate() {
        let inset = if i + 1 == n { INSET_LAST } else { INSET };
        out.push(Line::from(vec![
            Span::raw("  "),
            Span::styled(format!("{inset} "), theme.muted_style()),
            Span::styled(elide(&text.replace('\t', " "), budget), style),
        ]));
    }
    out
}

/// "Read 4 files" for a run of identical successes, with the first paths when they fit.
pub fn group_line(theme: &Theme, group: &[&ToolEntry], w: usize) -> Line<'static> {
    let name = humanize_name(&group[0].tool);
    let (verb, noun) = name.split_once(' ').unwrap_or((name.as_str(), ""));
    let summary = if noun.is_empty() {
        format!("{name} × {}", group.len())
    } else {
        let noun = if noun.ends_with('s') {
            noun.to_string()
        } else {
            format!("{noun}s")
        };
        format!("{verb} {} {noun}", group.len())
    };
    let base = vec![
        Span::raw("  "),
        Span::styled(FOLD_COLLAPSED, theme.text(theme.success)),
        Span::styled(format!(" {summary}"), theme.text(theme.effect_message)),
    ];
    let paths: Vec<String> = group
        .iter()
        .take(3)
        .filter_map(|t| {
            let p = arg(&t.args, &["path", "filename"]);
            (!p.is_empty()).then(|| clean_line(p))
        })
        .collect();
    let mut spans = base.clone();
    if !paths.is_empty() {
        let candidate = format!(" {}", paths.join(" "));
        if super::text::spans_width(&spans) + width(&candidate) <= w {
            spans.push(Span::styled(candidate, theme.text(theme.effect_message)));
        }
    }
    Line::from(super::text::clip_spans(spans, w))
}
