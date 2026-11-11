//! Docked cards: approval, question, capability setup, picker. One frame, shared hint line.

use super::text::{atoms, elide, spans_of, spans_width, width, wrap};
use super::tool::{action_title, CHECK, RUNNING_ARROW};
use crate::app::{App, Mode};
use crate::hints::{self, Hint};
use crate::sanitize::clean_line;
use crate::theme::Theme;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Frame;
use serde_json::Value;

pub struct CardSpec {
    pub title: Vec<Span<'static>>,
    pub right: Option<String>,
    pub rows: Vec<Line<'static>>,
    pub hint: Vec<Hint>,
    pub border: ratatui::style::Color,
}

impl CardSpec {
    /// Rows plus the hint row and the two borders.
    pub fn height(&self) -> usize {
        self.rows.len() + 3
    }
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

/// Wraps `text` to rows with a 2-column hanging indent under a label.
fn paragraph(
    theme: &Theme,
    text: &str,
    style: Style,
    w: usize,
    out: &mut Vec<Line<'static>>,
    max: usize,
) {
    let rows = wrap(&atoms(text, style), w.max(1));
    let total = rows.len();
    for row in rows.into_iter().take(max) {
        out.push(Line::from(spans_of(&row)));
    }
    if total > max {
        out.push(Line::from(Span::styled(
            format!("… ({} more lines)", total - max),
            theme.dim_style(),
        )));
    }
}

fn truncate_rows(rows: &mut Vec<Line<'static>>, max_rows: usize, theme: &Theme) {
    if rows.len() > max_rows && max_rows >= 1 {
        let hidden = rows.len() - (max_rows - 1);
        rows.truncate(max_rows - 1);
        rows.push(Line::from(Span::styled(
            format!("… ({hidden} more rows)"),
            theme.dim_style(),
        )));
    }
}

/// Any tool that sends: the owner must see the whole draft before it goes out.
pub fn is_send_tool(tool: &str) -> bool {
    let t = tool.to_lowercase();
    t.contains("send") && !t.contains("draft")
}

fn recipients(value: &Value) -> String {
    match value {
        Value::Array(items) => items
            .iter()
            .map(|v| clean_line(&scalar(v)))
            .collect::<Vec<_>>()
            .join(", "),
        other => clean_line(&scalar(other)),
    }
}

/// A label with a hanging indent: long recipient lists wrap, never truncate.
fn wrapped_kv(theme: &Theme, key: &str, value: &str, inner: usize) -> Vec<Line<'static>> {
    let lead = format!("{key}: ");
    let lead_w = width(&lead);
    let rows = wrap(
        &atoms(value, theme.fg_style()),
        inner.saturating_sub(lead_w).max(1),
    );
    rows.iter()
        .enumerate()
        .map(|(i, row)| {
            let mut spans = vec![if i == 0 {
                Span::styled(lead.clone(), theme.dim_style())
            } else {
                Span::raw(" ".repeat(lead_w))
            }];
            spans.extend(spans_of(row));
            Line::from(spans)
        })
        .collect()
}

/// The full outgoing draft: recipients and subject stay pinned, the body scrolls.
fn send_card(
    app: &App,
    item: &crate::protocol::PendingApproval,
    title: &str,
    w: usize,
    max_rows: usize,
) -> CardSpec {
    let theme = &app.theme;
    let inner = w.saturating_sub(2).max(8);
    let mut header: Vec<Line<'static>> = Vec::new();
    let lookup = |names: &[&str]| -> Option<(String, String)> {
        item.args
            .iter()
            .find(|(k, _)| names.contains(&k.to_lowercase().as_str()))
            .map(|(k, v)| (k.clone(), recipients(v)))
    };
    for (label, names) in [
        ("To", &["to", "recipient", "recipients"][..]),
        ("Cc", &["cc"][..]),
        ("Bcc", &["bcc"][..]),
        ("Subject", &["subject"][..]),
    ] {
        if let Some((_, value)) = lookup(names) {
            if !value.is_empty() || label == "To" {
                header.extend(wrapped_kv(theme, label, &value, inner));
            }
        }
    }
    const SHOWN: [&str; 9] = [
        "to",
        "recipient",
        "recipients",
        "cc",
        "bcc",
        "subject",
        "body",
        "message_body",
        "content",
    ];
    for (key, value) in &item.args {
        let lower = key.to_lowercase();
        if SHOWN.contains(&lower.as_str()) || value.is_object() || value.is_array() {
            continue;
        }
        header.extend(wrapped_kv(
            theme,
            &label(key),
            &clean_line(&scalar(value)),
            inner,
        ));
    }
    let body_text = item
        .args
        .iter()
        .find(|(k, _)| {
            matches!(
                k.to_lowercase().as_str(),
                "body" | "message_body" | "content"
            )
        })
        .map(|(_, v)| crate::sanitize::clean(&scalar(v)))
        .unwrap_or_default();
    let mut body: Vec<Line<'static>> = Vec::new();
    for logical in body_text.split('\n') {
        for row in wrap(
            &atoms(logical, theme.text(theme.secondary)),
            inner.saturating_sub(2),
        ) {
            let mut spans = vec![Span::styled("│ ", theme.keyline_style())];
            spans.extend(spans_of(&row));
            body.push(Line::from(spans));
        }
    }
    let mut rows = header;
    rows.push(Line::from(Span::styled("Body:", theme.dim_style())));
    // Room left for the body after the pinned rows; one row is kept for the scroll note.
    let room = max_rows.saturating_sub(rows.len()).max(2);
    let scrollable = body.len() > room;
    let visible = if scrollable { room - 1 } else { room };
    let max_scroll = body.len().saturating_sub(visible);
    app.card_max.set(if scrollable { max_scroll } else { 0 });
    let top = app.card_scroll.min(max_scroll);
    let end = (top + visible).min(body.len());
    rows.extend(body[top..end].iter().cloned());
    if scrollable {
        let below = body.len() - end;
        let note = if below > 0 {
            format!("↓ {below} more lines")
        } else {
            "end of draft".to_string()
        };
        rows.push(Line::from(Span::styled(
            format!("{note} · line {}-{} of {}", top + 1, end, body.len()),
            theme.dim_style(),
        )));
    }
    let count = app.pending.iter().filter(|p| p.tool != "ask_user").count();
    let mut hint: Vec<Hint> = Vec::new();
    if scrollable && !matches!(app.mode, Mode::Revising(_)) {
        hint.push(hints::SCROLL);
    }
    if matches!(app.mode, Mode::Revising(_)) {
        hint.extend_from_slice(hints::REVISING);
    } else {
        hint.extend_from_slice(hints::APPROVAL);
    }
    CardSpec {
        title: vec![
            Span::styled("Review the draft ", theme.bold(theme.brand)),
            Span::styled(
                format!("· {}", elide(title, w.saturating_sub(24))),
                theme.bold(theme.fg),
            ),
        ],
        right: (count > 1).then(|| format!("1 of {count}")),
        rows,
        hint,
        border: theme.popup_border,
    }
}

pub fn approval_card(app: &App, w: usize, max_rows: usize) -> Option<CardSpec> {
    let theme = &app.theme;
    let item = app.current_action()?;
    let inner = w.saturating_sub(2).max(8);
    let title = clean_line(&action_title(&item.tool, &item.args));
    if is_send_tool(&item.tool) {
        return Some(send_card(app, item, &title, w, max_rows));
    }
    let mut rows: Vec<Line<'static>> = Vec::new();
    if let Some(r) = item
        .reference
        .as_ref()
        .map(|r| clean_line(r))
        .or(item.task_id.map(|t| t.to_string()))
    {
        rows.push(Line::from(Span::styled(
            format!("task {r}"),
            theme.dim_style(),
        )));
    }
    for (key, value) in &item.args {
        let name = label(key);
        let long = matches!(
            key.as_str(),
            "body" | "message" | "message_body" | "content"
        );
        match value {
            Value::Object(map) => {
                for (k, v) in map {
                    if !v.is_object() && !v.is_array() {
                        let text = clean_line(&scalar(v));
                        rows.push(kv(
                            theme,
                            &format!("{name} {}", label(k).to_lowercase()),
                            &text,
                            inner,
                        ));
                    }
                }
            }
            Value::Array(items) => {
                let flat: Vec<String> = items
                    .iter()
                    .filter(|v| !v.is_object() && !v.is_array())
                    .map(|v| clean_line(&scalar(v)))
                    .collect();
                if !flat.is_empty() {
                    rows.push(kv(theme, &name, &flat.join(", "), inner));
                }
            }
            _ if long => {
                rows.push(Line::from(Span::styled(
                    format!("{name}:"),
                    theme.dim_style(),
                )));
                let text = crate::sanitize::clean(&scalar(value));
                let mut body = Vec::new();
                for logical in text.split('\n') {
                    paragraph(
                        theme,
                        logical,
                        theme.text(theme.secondary),
                        inner.saturating_sub(2),
                        &mut body,
                        6,
                    );
                }
                let total = body.len();
                for line in body.into_iter().take(7) {
                    let mut spans = vec![Span::raw("  ")];
                    spans.extend(line.spans);
                    rows.push(Line::from(spans));
                }
                if total > 7 {
                    rows.push(Line::from(Span::styled(
                        format!("  … ({} more lines)", total - 7),
                        theme.dim_style(),
                    )));
                }
            }
            _ => rows.push(kv(theme, &name, &clean_line(&scalar(value)), inner)),
        }
    }
    truncate_rows(&mut rows, max_rows, theme);
    let count = app.pending.iter().filter(|p| p.tool != "ask_user").count();
    let mut spans = vec![Span::styled("Action required ", theme.bold(theme.brand))];
    spans.push(Span::styled(
        format!("· {}", elide(&title, w.saturating_sub(22))),
        theme.bold(theme.fg),
    ));
    Some(CardSpec {
        title: spans,
        right: (count > 1).then(|| format!("1 of {count}")),
        rows,
        hint: if matches!(app.mode, Mode::Revising(_)) {
            hints::REVISING.to_vec()
        } else {
            hints::APPROVAL.to_vec()
        },
        border: theme.popup_border,
    })
}

fn kv(theme: &Theme, key: &str, value: &str, inner: usize) -> Line<'static> {
    let key_text = format!("{key}: ");
    let room = inner.saturating_sub(width(&key_text));
    Line::from(vec![
        Span::styled(key_text, theme.dim_style()),
        Span::styled(elide(value, room), theme.fg_style()),
    ])
}

/// One list row: optional current marker, optional digit, label, bar when focused.
#[allow(clippy::too_many_arguments)]
pub fn option_row(
    theme: &Theme,
    selected: bool,
    current: bool,
    number: Option<usize>,
    show_marker_slot: bool,
    label: &str,
    detail: &str,
    w: usize,
) -> Line<'static> {
    let mut spans: Vec<Span<'static>> = Vec::new();
    let bar = theme.list_cursor();
    let base = |s: Style| if selected { bar } else { s };
    if show_marker_slot {
        let (text, style) = if current {
            ("✓ ", base(theme.text(theme.text_success)))
        } else {
            ("  ", base(Style::new()))
        };
        spans.push(Span::styled(text, style));
    }
    let digit = match number {
        Some(n) => format!("{n}. "),
        None => "   ".to_string(),
    };
    spans.push(Span::styled(digit, base(theme.dim_style())));
    let used = spans_width(&spans);
    let room = w.saturating_sub(used);
    let detail_w = if detail.is_empty() {
        0
    } else {
        width(detail) + 2
    };
    let label_room = if width(label) + 8 <= room {
        width(label)
    } else {
        room.saturating_sub(detail_w.min(room.saturating_sub(8)))
    };
    let label_text = elide(label, label_room);
    let label_style = if selected { bar } else { theme.fg_style() };
    spans.push(Span::styled(label_text.clone(), label_style));
    let mut total = used + width(&label_text);
    if !detail.is_empty() && total + 2 < w {
        let d = elide(detail, w - total - 2);
        total += 2 + width(&d);
        spans.push(Span::styled(format!("  {d}"), base(theme.muted_style())));
    }
    if selected && total < w {
        spans.push(Span::styled(" ".repeat(w - total), bar));
    }
    Line::from(spans)
}

fn window(selected: usize, len: usize, rows: usize) -> usize {
    if len <= rows {
        0
    } else {
        selected.saturating_sub(rows / 2).min(len - rows)
    }
}

pub fn question_card(app: &App, w: usize, max_rows: usize) -> Option<CardSpec> {
    let theme = &app.theme;
    let questions = app.questions();
    let q = questions.get(app.q_tab).or(questions.first())?;
    let inner = w.saturating_sub(2).max(8);
    let text = clean_line(
        q.args
            .get("question")
            .and_then(Value::as_str)
            .unwrap_or("Select an option"),
    );
    let mut rows: Vec<Line<'static>> = Vec::new();
    for row in wrap(&atoms(&text, theme.bold(theme.fg)), inner)
        .into_iter()
        .take(3)
    {
        rows.push(Line::from(spans_of(&row)));
    }
    let mut choices: Vec<String> = q
        .args
        .get("choices")
        .and_then(Value::as_array)
        .map(|a| a.iter().map(|c| clean_line(&scalar(c))).collect())
        .unwrap_or_default();
    choices.push(crate::keys::FREE_TEXT.to_string());
    let room = max_rows.saturating_sub(rows.len()).max(1);
    let top = window(app.q_sel, choices.len(), room);
    for (i, choice) in choices.iter().enumerate().skip(top).take(room) {
        let number = (i < crate::keys::DIGIT_ROWS).then_some(i + 1);
        rows.push(option_row(
            theme,
            i == app.q_sel,
            false,
            number,
            false,
            choice,
            "",
            inner,
        ));
    }
    let tabs = questions.len();
    let mut title = vec![Span::styled("Question", theme.bold(theme.brand))];
    if tabs > 1 {
        for i in 0..tabs {
            let style = if i == app.q_tab {
                theme.list_cursor()
            } else {
                theme.muted_style()
            };
            title.push(Span::styled(format!(" {} ", i + 1), style));
        }
    }
    Some(CardSpec {
        title,
        right: None,
        rows,
        hint: {
            let mut h = if tabs > 1 {
                vec![hints::TABS, hints::NAVIGATE, hints::SELECT]
            } else {
                vec![hints::NAVIGATE, hints::SELECT]
            };
            h.push(hints::pick(choices.len()));
            h.push(hints::BACK);
            h
        },
        border: theme.popup_border,
    })
}

pub fn setup_card(app: &App, w: usize, max_rows: usize) -> Option<CardSpec> {
    let theme = &app.theme;
    let card = app.current_setup()?;
    let inner = w.saturating_sub(2).max(8);
    let mut rows: Vec<Line<'static>> = Vec::new();
    let url_waiting =
        card.status == "waiting" && card.kind.as_deref() == Some("url") && card.url.is_some();
    if card.status == "failed" {
        let sentence = card.reason.clone().unwrap_or_else(|| {
            "The setup failed without a reason recorded. Ask bud for the next step.".into()
        });
        let style = theme.text(theme.text_error);
        for (i, row) in wrap(&atoms(&sentence, style), inner.saturating_sub(2))
            .iter()
            .enumerate()
        {
            let mut spans = vec![Span::styled(if i == 0 { "✗ " } else { "  " }, style)];
            spans.extend(spans_of(row));
            rows.push(Line::from(spans));
        }
    } else {
        let explanation = if card.status == "connected" {
            format!(
                "{} is connected. Its tools load after the bud server restarts and a new conversation starts.",
                card.label
            )
        } else if card.kind.as_deref() == Some("steps") && !card.steps.is_empty() {
            format!("Finish the steps below to connect {}.", card.label)
        } else {
            format!(
                "Open the sign-in link below in a browser to finish connecting {}. The card settles once the connection completes.",
                card.label
            )
        };
        for row in wrap(&atoms(&explanation, theme.text(theme.secondary)), inner) {
            rows.push(Line::from(spans_of(&row)));
        }
        if card.status == "connected" {
            rows.push(Line::from(Span::styled(
                format!("{CHECK} {} connected", card.label),
                theme.text(theme.text_success),
            )));
        } else {
            let lit = super::tool::pulse_lit(app.now_ms());
            let arrow = theme.text(if lit { theme.brand } else { theme.dim });
            rows.push(Line::from(vec![
                Span::styled(RUNNING_ARROW, arrow),
                Span::styled(format!(" Waiting for {}…", card.label), theme.dim_style()),
            ]));
            for step in &card.steps {
                for (i, row) in wrap(
                    &atoms(step, theme.text(theme.secondary)),
                    inner.saturating_sub(2),
                )
                .iter()
                .enumerate()
                {
                    let mut spans = vec![Span::raw(if i == 0 { "· " } else { "  " })];
                    spans.extend(spans_of(row));
                    rows.push(Line::from(spans));
                }
            }
        }
        if url_waiting {
            if let Some(url) = &card.url {
                rows.push(Line::from(Span::styled(
                    elide(url, inner),
                    theme.fg_style().add_modifier(Modifier::UNDERLINED),
                )));
            }
        }
    }
    truncate_rows(&mut rows, max_rows, theme);
    Some(CardSpec {
        title: vec![Span::styled(
            format!("Connect {}", card.label),
            theme.bold(theme.brand),
        )],
        right: None,
        rows,
        hint: if url_waiting {
            hints::SETUP_URL.to_vec()
        } else {
            hints::SETUP_PLAIN.to_vec()
        },
        border: theme.popup_border,
    })
}

pub fn picker_card(app: &App, w: usize, max_rows: usize) -> Option<CardSpec> {
    let theme = &app.theme;
    let picker = app.picker.as_ref()?;
    let inner = w.saturating_sub(2).max(8);
    let visible = picker.visible();
    let searching = picker.search || !picker.filter.is_empty();
    let mut rows: Vec<Line<'static>> = Vec::new();
    if searching {
        let caret = if picker.search { "▏" } else { "" };
        rows.push(Line::from(vec![
            Span::styled("/ ", theme.primary_style().add_modifier(Modifier::BOLD)),
            Span::styled(format!("{}{caret}", picker.filter), theme.fg_style()),
        ]));
    }
    let room = max_rows.saturating_sub(rows.len()).max(1);
    if visible.is_empty() {
        let text = if picker.items.is_empty() {
            picker.empty.clone()
        } else {
            "No match. Esc clears the filter.".to_string()
        };
        for row in wrap(&atoms(&text, theme.muted_style()), inner)
            .into_iter()
            .take(room)
        {
            rows.push(Line::from(spans_of(&row)));
        }
    } else {
        let top = window(picker.selected, visible.len(), room);
        let label_w = visible
            .iter()
            .map(|&i| width(&picker.items[i].label))
            .max()
            .unwrap_or(0)
            .min(inner / 2);
        for (pos, &idx) in visible.iter().enumerate().skip(top).take(room) {
            let it = &picker.items[idx];
            let label = elide(&it.label, label_w);
            let label = format!(
                "{label}{}",
                " ".repeat(label_w.saturating_sub(width(&label)))
            );
            let current = picker.current.as_deref() == Some(it.value.as_str());
            let number = (pos < crate::keys::DIGIT_ROWS && !searching).then_some(pos + 1);
            rows.push(option_row(
                theme,
                pos == picker.selected,
                current,
                number,
                true,
                &label,
                &it.detail,
                inner,
            ));
        }
    }
    let hint = if picker.search {
        vec![hints::ARROWS, hints::SELECT, hints::BACK]
    } else {
        let mut h = vec![hints::NAVIGATE, hints::SELECT];
        if !searching && visible.len() > 1 {
            h.push(hints::pick(visible.len()));
        }
        h.push(hints::FILTER);
        h.push(hints::BACK);
        h
    };
    Some(CardSpec {
        title: vec![Span::styled(picker.title.clone(), theme.bold(theme.brand))],
        right: None,
        rows,
        hint,
        border: theme.popup_border,
    })
}

pub fn render(frame: &mut Frame, area: Rect, spec: &CardSpec, theme: &Theme) {
    let border = Style::new().fg(spec.border);
    let mut title_spans = vec![Span::raw(" ")];
    title_spans.extend(spec.title.clone());
    title_spans.push(Span::raw(" "));
    let mut block = Block::default()
        .borders(Borders::TOP | Borders::BOTTOM)
        .border_style(border)
        .title(Line::from(title_spans));
    if let Some(right) = &spec.right {
        block = block.title(
            Line::from(Span::styled(format!(" {right} "), theme.muted_style())).right_aligned(),
        );
    }
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let content = Rect {
        x: inner.x + 1,
        y: inner.y,
        width: inner.width.saturating_sub(2),
        height: inner.height,
    };
    let mut lines: Vec<Line<'static>> = spec.rows.clone();
    let hint_budget = content.width as usize;
    let hint_spans = if hints::width(&spec.hint) <= hint_budget {
        hints::spans(&spec.hint, theme, Some(hint_budget))
    } else if hints::condensed_width(&spec.hint) <= hint_budget {
        hints::condensed(&spec.hint, theme)
    } else {
        hints::spans(&spec.hint, theme, Some(hint_budget))
    };
    lines.push(Line::from(hint_spans));
    // The hint line always sits on the last content row.
    let body_rows = content.height.saturating_sub(1) as usize;
    let hint = lines.pop().unwrap_or_default();
    lines.truncate(body_rows);
    while lines.len() < body_rows {
        lines.push(Line::default());
    }
    lines.push(hint);
    frame.render_widget(Paragraph::new(lines), content);
}
