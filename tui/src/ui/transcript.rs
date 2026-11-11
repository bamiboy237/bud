//! The transcript document: measure every entry, then materialize only the rows on screen.

use super::text::{atoms, spans_of, spans_width, wrap};
use super::tool;
use crate::app::{App, Entry, Level, LineCache, ToolStatus, UserState};
use crate::mascot::COLS;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Banner,
    User,
    Assistant,
    Tools,
    Note,
}

/// One laid-out block of the document: an entry, or a run of consecutive tool entries.
#[derive(Clone, Debug)]
pub struct Item {
    pub kind: Kind,
    pub start: usize,
    pub end: usize,
    pub gap: usize,
    pub height: usize,
}

const USER_MAX_ROWS: usize = 12;

fn gap_between(prev: Option<Kind>, kind: Kind) -> usize {
    match (prev, kind) {
        (None, _) => 0,
        (Some(Kind::Tools), Kind::Tools) => 0,
        (Some(Kind::Note), Kind::Note) => 0,
        (Some(Kind::Banner), _) => 1,
        _ => 1,
    }
}

pub fn measure(app: &mut App, w: usize) -> (Vec<Item>, usize) {
    let mut items: Vec<Item> = Vec::new();
    let mut total = 0;
    let mut i = app.hide_before;
    let mut prev: Option<Kind> = None;
    let len = app.entries.len();
    while i < len {
        let (kind, end) = match &app.entries[i] {
            Entry::Banner => (Kind::Banner, i + 1),
            Entry::User(_) => (Kind::User, i + 1),
            Entry::Assistant(_) => (Kind::Assistant, i + 1),
            Entry::Note(_) => (Kind::Note, i + 1),
            Entry::Tool(_) => {
                let mut j = i;
                while j < len && matches!(app.entries[j], Entry::Tool(_)) {
                    j += 1;
                }
                (Kind::Tools, j)
            }
        };
        let mut item = Item {
            kind,
            start: i,
            end,
            gap: 0,
            height: 0,
        };
        item.height = item_height(app, &item, w);
        if kind == Kind::Assistant && item.height == 0 {
            i = end;
            continue;
        }
        item.gap = gap_between(prev, kind);
        total += item.gap + item.height;
        prev = Some(kind);
        items.push(item);
        i = end;
    }
    (items, total)
}

fn item_height(app: &mut App, item: &Item, w: usize) -> usize {
    match item.kind {
        Kind::Banner => banner_lines(app, w).len(),
        Kind::User => user_lines(app, item.start, w).len(),
        Kind::Assistant => assistant_len(app, item.start, w),
        Kind::Note => note_lines(app, item.start, w).len(),
        Kind::Tools => tool_block(app, item.start, item.end, w).len(),
    }
}

fn assistant_len(app: &mut App, idx: usize, w: usize) -> usize {
    let theme = app.theme.clone();
    let gen = app.theme_gen;
    match &mut app.entries[idx] {
        Entry::Assistant(a) => {
            let done = !a.streaming;
            a.doc.lines(w, &theme, gen, done).len()
        }
        _ => 0,
    }
}

/// Rows `lo..hi` of an item, each with the entry a click on it should toggle.
pub fn slice(
    app: &mut App,
    item: &Item,
    w: usize,
    lo: usize,
    hi: usize,
) -> Vec<(Line<'static>, Option<usize>)> {
    match item.kind {
        Kind::Assistant => {
            let theme = app.theme.clone();
            let gen = app.theme_gen;
            let Entry::Assistant(a) = &mut app.entries[item.start] else {
                return Vec::new();
            };
            let done = !a.streaming;
            let lines = a.doc.lines(w, &theme, gen, done);
            let hi = hi.min(lines.len());
            lines[lo.min(hi)..hi]
                .iter()
                .map(|l| (l.clone(), None))
                .collect()
        }
        Kind::Banner => take(&banner_lines(app, w), lo, hi, None),
        Kind::User => take(user_lines(app, item.start, w), lo, hi, None),
        Kind::Note => take(note_lines(app, item.start, w), lo, hi, None),
        Kind::Tools => {
            let rows = tool_block(app, item.start, item.end, w);
            let hi = hi.min(rows.len());
            rows[lo.min(hi)..hi].to_vec()
        }
    }
}

fn take(
    lines: &[Line<'static>],
    lo: usize,
    hi: usize,
    hit: Option<usize>,
) -> Vec<(Line<'static>, Option<usize>)> {
    let hi = hi.min(lines.len());
    lines[lo.min(hi)..hi]
        .iter()
        .map(|l| (l.clone(), hit))
        .collect()
}

fn banner_lines(app: &App, w: usize) -> Vec<Line<'static>> {
    let theme = &app.theme;
    let rows = app.mascot.rows();
    let model = crate::sanitize::clean_line(&app.model);
    let info: [Vec<Span<'static>>; 3] = [
        vec![
            Span::styled("bud", theme.bold(theme.brand)),
            Span::styled(
                format!(" v{}", env!("CARGO_PKG_VERSION")),
                theme.dim_style(),
            ),
        ],
        {
            let mut v = vec![Span::styled(model, theme.fg_style())];
            if !app.context_label.is_empty() {
                v.push(Span::styled(" · ", theme.dim_style()));
                v.push(Span::styled(
                    crate::sanitize::clean_line(&app.context_label),
                    theme.muted_style(),
                ));
            }
            v
        },
        vec![
            Span::styled("Type ", theme.fg_style()),
            Span::styled("/", theme.bold(theme.secondary)),
            Span::styled(" for commands", theme.fg_style()),
        ],
    ];
    let margin = 2;
    let budget = w.saturating_sub(margin);
    let fits = info.iter().all(|p| COLS + spans_width(p) <= budget);
    let mut out: Vec<Line<'static>> = vec![Line::default()];
    let art = |row: &str| Span::styled(format!("{row:<COLS$}"), theme.fg_style());
    if fits {
        for (row, part) in rows.iter().zip(info.iter()) {
            let mut spans = vec![Span::raw(" ".repeat(margin)), art(row)];
            spans.extend(part.clone());
            out.push(Line::from(spans));
        }
    } else {
        for row in &rows {
            out.push(Line::from(vec![Span::raw(" ".repeat(margin)), art(row)]));
        }
        out.push(Line::default());
        for (i, part) in info.iter().enumerate() {
            if spans_width(part) <= budget {
                let mut spans = vec![Span::raw(" ".repeat(margin))];
                spans.extend(part.clone());
                out.push(Line::from(spans));
            } else if i + 1 < info.len() {
                let mut spans = vec![Span::raw(" ".repeat(margin))];
                spans.extend(super::text::clip_spans(part.clone(), budget));
                out.push(Line::from(spans));
            }
        }
    }
    out
}

fn cached(
    cache: &mut Option<LineCache>,
    w: usize,
    gen: u64,
    build: impl FnOnce() -> Vec<Line<'static>>,
) -> &[Line<'static>] {
    let stale = cache
        .as_ref()
        .is_none_or(|c| c.width != w || c.theme_gen != gen);
    if stale {
        *cache = Some(LineCache {
            width: w,
            theme_gen: gen,
            lines: build(),
        });
    }
    &cache.as_ref().expect("cache just filled").lines
}

fn user_lines(app: &mut App, idx: usize, w: usize) -> &[Line<'static>] {
    let theme = app.theme.clone();
    let gen = app.theme_gen;
    let Entry::User(u) = &mut app.entries[idx] else {
        return &[];
    };
    let (state, text) = (u.state, u.text.clone());
    cached(&mut u.cache, w, gen, || {
        let inner = w.saturating_sub(4).max(1);
        let rail_color = if state == UserState::Failed {
            theme.error
        } else {
            theme.brand
        };
        let mut text_style = Style::new()
            .fg(theme.fg)
            .bg(theme.surface)
            .add_modifier(Modifier::BOLD);
        if state == UserState::Queued || state == UserState::Pending {
            text_style = text_style.add_modifier(Modifier::ITALIC);
        }
        let panel = Style::new().bg(theme.surface);
        let mut rows: Vec<Vec<super::text::Atom>> = Vec::new();
        let source = atoms(&text, text_style);
        for logical in wrap(&source, inner) {
            rows.push(logical);
        }
        let hidden = rows.len().saturating_sub(USER_MAX_ROWS);
        rows.truncate(USER_MAX_ROWS);
        let mut lines: Vec<Line<'static>> = Vec::new();
        let n = rows.len();
        for (i, row) in rows.iter().enumerate() {
            let mut spans = vec![
                Span::styled("▌", Style::new().fg(rail_color).bg(theme.surface)),
                Span::styled(
                    if i == 0 { " > " } else { "   " },
                    Style::new()
                        .fg(theme.brand)
                        .bg(theme.surface)
                        .add_modifier(Modifier::BOLD),
                ),
            ];
            spans.extend(spans_of(row));
            if i + 1 == n {
                let note = match state {
                    UserState::Queued => Some(" · queued"),
                    UserState::Failed => Some(" · not sent"),
                    _ => None,
                };
                if let Some(note) = note {
                    spans.push(Span::styled(
                        note,
                        Style::new().fg(theme.muted).bg(theme.surface),
                    ));
                }
            }
            lines.push(Line::from(spans).style(panel));
        }
        if hidden > 0 {
            lines.push(
                Line::from(vec![
                    Span::styled("▌", Style::new().fg(rail_color).bg(theme.surface)),
                    Span::styled(
                        format!("   … ({hidden} more lines)"),
                        Style::new().fg(theme.muted).bg(theme.surface),
                    ),
                ])
                .style(panel),
            );
        }
        lines
    })
}

fn note_lines(app: &mut App, idx: usize, w: usize) -> &[Line<'static>] {
    let theme = app.theme.clone();
    let gen = app.theme_gen;
    let Entry::Note(n) = &mut app.entries[idx] else {
        return &[];
    };
    let level = n.level;
    let text = n.text.clone();
    cached(&mut n.cache, w, gen, || {
        let style = match level {
            Level::Info => theme.fg_style(),
            Level::Success => theme.text(theme.text_success),
            Level::Warn => theme.text(theme.warning),
            Level::Error => theme.text(theme.text_error),
            Level::Muted => theme.muted_style(),
        };
        let inner = w.saturating_sub(4).max(1);
        let mut out = Vec::new();
        for logical in text.split('\n') {
            let rows = wrap(&atoms(logical, style), inner);
            for row in rows {
                let mut spans = vec![Span::raw("  ")];
                spans.extend(spans_of(&row));
                out.push(Line::from(spans));
            }
        }
        out
    })
}

/// Lines of a run of consecutive tool entries, with the entry each row toggles.
fn tool_block(
    app: &App,
    start: usize,
    end: usize,
    w: usize,
) -> Vec<(Line<'static>, Option<usize>)> {
    let theme = &app.theme;
    let pulse = app.now_ms();
    let mut out: Vec<(Line<'static>, Option<usize>)> = Vec::new();
    let mut i = start;
    while i < end {
        let Entry::Tool(first) = &app.entries[i] else {
            i += 1;
            continue;
        };
        let groupable = |t: &crate::app::ToolEntry| {
            t.status == ToolStatus::Done && !t.interrupted && !t.expanded && t.tool != "ask_user"
        };
        if groupable(first) {
            let mut j = i + 1;
            while j < end {
                match &app.entries[j] {
                    Entry::Tool(t) if groupable(t) && t.tool == first.tool => j += 1,
                    _ => break,
                }
            }
            if j - i > 1 {
                let group: Vec<&crate::app::ToolEntry> = (i..j)
                    .filter_map(|k| match &app.entries[k] {
                        Entry::Tool(t) => Some(t),
                        _ => None,
                    })
                    .collect();
                out.push((tool::group_line(theme, &group, w), Some(i)));
                i = j;
                continue;
            }
        }
        for line in tool::lines(theme, first, w, pulse) {
            out.push((line, Some(i)));
        }
        i += 1;
    }
    out
}
