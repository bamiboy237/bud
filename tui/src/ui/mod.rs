//! Frame composition: transcript, loading row, composer or card, footer, slash popup.

pub mod cards;
pub mod composer;
pub mod markdown;
pub mod text;
pub mod tool;
pub mod transcript;

use crate::app::{App, Mode, Status};
use crate::hints;
use crate::theme::{Polarity, Theme};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};
use ratatui::Frame;
use text::{clip_spans, elide, spans_width, width};

const SPINNER: [char; 10] = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
const MAX_MENU_ROWS: usize = 10;

pub fn draw(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    frame.render_widget(Block::default().style(app.theme.base()), area);
    if area.width < 10 || area.height < 4 {
        frame.render_widget(Paragraph::new("bud: terminal too small"), area);
        return;
    }
    let w = area.width as usize;
    let h = area.height as usize;
    let footer = Rect {
        x: 0,
        y: area.height - 1,
        width: area.width,
        height: 1,
    };
    let loading_rows = usize::from(loading_visible(app));
    let free = h.saturating_sub(1 + loading_rows + 3);

    let composing = matches!(
        app.mode,
        Mode::Prompt | Mode::Revising(_) | Mode::Answering(_) | Mode::Editing(_)
    );
    let revising_card = matches!(app.mode, Mode::Revising(_));

    // The composer is sized first: while revising it must keep its place under the card.
    let marker = composer_marker(app);
    let text_w = w.saturating_sub(text::width(&marker) + 1).max(1);
    app.composer_width = text_w;
    let rows = app.composer.rows(text_w);
    let max_rows = (h / 2).max(3);
    let min_rows = if revising_card {
        1
    } else if h >= 28 {
        3
    } else if h >= 22 {
        2
    } else {
        1
    };
    let content_rows = rows.len().clamp(min_rows, max_rows.max(min_rows));
    let composer_h = content_rows + 2;
    let card_rows = free
        .saturating_sub(3)
        .saturating_sub(if revising_card { composer_h } else { 0 })
        .max(2);
    let card = match &app.mode {
        Mode::Approval | Mode::Revising(_) => cards::approval_card(app, w, card_rows),
        Mode::Question => cards::question_card(app, w, card_rows),
        Mode::Setup => cards::setup_card(app, w, card_rows),
        Mode::Picker => cards::picker_card(app, w, card_rows),
        _ => None,
    };
    let card_h = card.as_ref().map_or(0, |c| {
        c.height().min(
            h.saturating_sub(1 + loading_rows + 3 + if revising_card { composer_h } else { 0 }),
        )
    });
    let bottom_h = if revising_card {
        card_h + composer_h
    } else if composing {
        composer_h
    } else {
        card_h
    };
    let bottom_h = bottom_h.min(h.saturating_sub(1 + loading_rows + 2));
    let transcript_h = h.saturating_sub(1 + loading_rows + bottom_h);
    let transcript = Rect {
        x: 0,
        y: 0,
        width: area.width,
        height: transcript_h as u16,
    };
    let loading = Rect {
        x: 0,
        y: transcript_h as u16,
        width: area.width,
        height: loading_rows as u16,
    };
    let bottom = Rect {
        x: 0,
        y: (transcript_h + loading_rows) as u16,
        width: area.width,
        height: bottom_h as u16,
    };

    draw_transcript(frame, app, transcript);
    if loading_rows == 1 {
        draw_loading(frame, app, loading);
    }
    let theme = app.theme.clone();
    let mut composer_area = None;
    if revising_card {
        if let Some(spec) = &card {
            let card_area = Rect {
                height: card_h as u16,
                ..bottom
            };
            cards::render(frame, card_area, spec, &theme);
        }
        composer_area = Some(Rect {
            y: bottom.y + card_h as u16,
            height: composer_h as u16,
            ..bottom
        });
    } else if composing {
        composer_area = Some(bottom);
    } else if let Some(spec) = &card {
        cards::render(frame, bottom, spec, &theme);
    }
    if let Some(area) = composer_area {
        draw_composer(frame, app, area, &marker, &rows, text_w);
        draw_menu(frame, app, area);
    }
    draw_footer(frame, app, footer);
}

fn composer_marker(app: &App) -> String {
    match &app.mode {
        Mode::Revising(_) => "revise › ".into(),
        Mode::Answering(_) => "answer › ".into(),
        Mode::Editing(_) => "edit › ".into(),
        _ => "> ".into(),
    }
}

fn draw_transcript(frame: &mut Frame, app: &mut App, area: Rect) {
    let view_h = area.height as usize;
    app.view_h = view_h;
    app.hits.clear();
    if view_h == 0 {
        return;
    }
    let mut width = area.width as usize;
    let sticky = app.last_total > view_h;
    let mut tw = if sticky {
        width.saturating_sub(1)
    } else {
        width
    };
    let (mut items, mut total) = transcript::measure(app, tw);
    if (total > view_h) != sticky {
        tw = if total > view_h {
            width.saturating_sub(1)
        } else {
            width
        };
        let again = transcript::measure(app, tw);
        items = again.0;
        total = again.1;
    }
    width = area.width as usize;
    let scrollbar = total > view_h;
    // Hold the viewport on the same rows while the document grows beneath it.
    if app.scroll > 0 && total > app.last_total {
        app.scroll += total - app.last_total;
    }
    let max_scroll = total.saturating_sub(view_h);
    app.scroll = app.scroll.min(max_scroll);
    app.last_total = total;
    let top = total.saturating_sub(view_h).saturating_sub(app.scroll);
    let bottom_row = top + view_h;
    let buf = frame.buffer_mut();
    let mut row = 0usize;
    for item in &items {
        let start = row + item.gap;
        let end = start + item.height;
        row = end;
        if end <= top {
            continue;
        }
        if start >= bottom_row {
            break;
        }
        let lo = top.saturating_sub(start);
        let hi = (bottom_row - start).min(item.height);
        let rows = transcript::slice(app, item, tw, lo, hi);
        for (i, (line, hit)) in rows.into_iter().enumerate() {
            let y = start + lo + i - top;
            if y >= view_h {
                break;
            }
            let sy = area.y + y as u16;
            let line_area = Rect {
                x: area.x,
                y: sy,
                width: tw as u16,
                height: 1,
            };
            buf.set_style(line_area, line.style);
            buf.set_line(area.x, sy, &line, tw as u16);
            if let Some(entry) = hit {
                app.hits.push((sy, entry));
            }
        }
    }
    if scrollbar {
        draw_scrollbar(
            buf,
            app,
            Rect {
                x: area.x + width as u16 - 1,
                y: area.y,
                width: 1,
                height: area.height,
            },
            total,
            top,
        );
    }
}

fn draw_scrollbar(buf: &mut Buffer, app: &App, col: Rect, total: usize, top: usize) {
    let h = col.height as usize;
    let thumb = ((h * h) / total.max(1)).clamp(1, h);
    let max_top = total.saturating_sub(h).max(1);
    let pos = (top * (h - thumb)) / max_top;
    for i in 0..h {
        let on = i >= pos && i < pos + thumb;
        let style = Style::new().bg(if on {
            app.theme.scrollbar
        } else {
            app.theme.scrollbar_bg
        });
        buf.set_string(col.x, col.y + i as u16, " ", style);
    }
}

// Loading row -----------------------------------------------------------------

fn loading_visible(app: &App) -> bool {
    app.running()
        || app.status == Status::Waiting
        || app.queue_paused
        || !app.queued.is_empty()
        || app.reconnecting
}

pub fn format_elapsed(secs: u64) -> String {
    if secs < 60 {
        format!("{secs}s")
    } else if secs < 3600 {
        format!("{}m{}s", secs / 60, secs % 60)
    } else {
        format!("{}h{}m{}s", secs / 3600, (secs % 3600) / 60, secs % 60)
    }
}

fn draw_loading(frame: &mut Frame, app: &App, area: Rect) {
    let theme = &app.theme;
    let w = area.width as usize;
    let ms = app.now_ms();
    let mut spans: Vec<Span<'static>> = vec![Span::raw("  ")];
    if app.reconnecting {
        spans.push(Span::styled("◷ Reconnecting to bud…", theme.muted_style()));
    } else if app.running() {
        let frame_i = (ms / 100) as usize;
        spans.push(Span::styled(
            format!("{} ", SPINNER[frame_i % SPINNER.len()]),
            theme.text(theme.brand),
        ));
        let label = match &app.status {
            Status::Thinking => "Thinking".to_string(),
            Status::Streaming => "Generating".to_string(),
            Status::Tool(_) => "Working".to_string(),
            _ => "Working".to_string(),
        };
        let chars: Vec<char> = label.chars().collect();
        let front = frame_i % (chars.len() + 6);
        for (i, c) in chars.iter().enumerate() {
            let d = front as isize - i as isize;
            let color = match d {
                0 | 1 => theme.brand_ramp[0],
                -1 | 2 => theme.brand_ramp[1],
                _ => theme.brand_ramp[2],
            };
            spans.push(Span::styled(c.to_string(), theme.text(color)));
        }
        spans.push(Span::styled("… ", theme.text(theme.brand_ramp[2])));
        if let Some(start) = app.run_start {
            let secs = ms.saturating_sub(start) / 1000;
            spans.push(Span::styled(format_elapsed(secs), theme.muted_style()));
        }
        if w >= 44 {
            spans.push(Span::styled("  ", Style::new()));
            spans.extend(hints::spans(&[hints::INTERRUPT], theme, None));
        }
    } else if app.status == Status::Waiting {
        spans.push(Span::styled(
            "◷ Waiting for your decision…",
            theme.muted_style(),
        ));
    } else if app.queue_paused {
        match app.pause_reason.as_deref() {
            Some("stopped") | Some("failed") => {
                spans.push(Span::styled("Paused · ", theme.text(theme.warning)));
                spans.push(Span::styled(
                    "send a message to continue",
                    theme.muted_style(),
                ));
            }
            Some("uncertain") => {
                spans.push(Span::styled("Paused · ", theme.text(theme.warning)));
                spans.push(Span::styled(
                    "verify the outcome, then ",
                    theme.muted_style(),
                ));
                spans.push(Span::styled(
                    "/resume",
                    theme.primary_style().add_modifier(Modifier::BOLD),
                ));
            }
            _ => {
                spans.push(Span::styled("Queue paused · ", theme.text(theme.warning)));
                spans.push(Span::styled(
                    "/resume",
                    theme.primary_style().add_modifier(Modifier::BOLD),
                ));
            }
        }
    }
    if !app.queued.is_empty() && !app.reconnecting {
        let n = app.queued.len();
        spans.push(Span::styled(
            format!("   » {n} queued"),
            theme
                .text(theme.brand)
                .add_modifier(Modifier::BOLD | Modifier::ITALIC),
        ));
    }
    frame.render_widget(Paragraph::new(Line::from(clip_spans(spans, w))), area);
}

// Composer --------------------------------------------------------------------

fn mode_color(theme: &Theme, mode: &str) -> ratatui::style::Color {
    match mode {
        "strict" => theme.text_success,
        "auto" => theme.warning,
        _ => theme.muted,
    }
}

/// The right-aligned title: model shrinks before it disappears, mode stays.
fn composer_title(app: &App, w: usize) -> Vec<Span<'static>> {
    let theme = &app.theme;
    let room = w.saturating_sub(5);
    let mode = app.approval_mode.clone();
    let model = crate::sanitize::clean_line(&app.model);
    let sep = " · ";
    let mode_w = width(&mode);
    let full = width(&model) + width(sep) + mode_w;
    let model_shown = if full <= room {
        Some(model.clone())
    } else if 12 + width(sep) + mode_w <= room {
        Some(elide(&model, room - width(sep) - mode_w))
    } else {
        None
    };
    let mut spans = vec![Span::raw(" ")];
    if let Some(m) = model_shown {
        spans.push(Span::styled(m, theme.fg_style()));
        spans.push(Span::styled(sep, theme.muted_style()));
    }
    spans.push(Span::styled(
        mode.clone(),
        theme.text(mode_color(theme, &mode)),
    ));
    spans.push(Span::raw(" "));
    spans.push(Span::styled("─", Style::new().fg(theme.border)));
    spans
}

fn draw_composer(
    frame: &mut Frame,
    app: &mut App,
    area: Rect,
    marker: &str,
    rows: &[composer::Row],
    text_w: usize,
) {
    let theme = app.theme.clone();
    let border = Style::new().fg(theme.border);
    let block = Block::default()
        .borders(Borders::TOP | Borders::BOTTOM)
        .border_style(border)
        .title(Line::from(composer_title(app, area.width as usize)).right_aligned());
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let visible = inner.height as usize;
    let (caret_row, caret_col) = app.composer.caret(rows);
    let mut top = app.composer.top_row.min(rows.len().saturating_sub(1));
    if caret_row < top {
        top = caret_row;
    }
    if caret_row >= top + visible {
        top = caret_row + 1 - visible;
    }
    app.composer.top_row = top;
    let marker_w = text::width(marker);
    let marker_style = theme.text(theme.brand).add_modifier(Modifier::BOLD);
    let src = app.composer.text.clone();
    let mut lines: Vec<Line<'static>> = Vec::new();
    for (i, row) in rows.iter().enumerate().skip(top).take(visible) {
        let lead = if i == 0 {
            Span::styled(marker.to_string(), marker_style)
        } else {
            Span::raw(" ".repeat(marker_w))
        };
        let raw = &src[row.start..row.end];
        let shown = raw.replace('\t', "    ");
        let mut spans = vec![lead, Span::styled(shown, theme.fg_style())];
        if i == 0 && src.is_empty() {
            let hint = match app.mode {
                Mode::Revising(_) => "Say what to change instead…",
                Mode::Answering(_) => "Type your answer…",
                Mode::Editing(_) => "Edit the queued message…",
                _ => "Ask anything…",
            };
            spans.push(Span::styled(hint, theme.dim_style()));
        }
        lines.push(Line::from(clip_spans(spans, inner.width as usize)));
    }
    frame.render_widget(Paragraph::new(lines), inner);
    if caret_row >= top && caret_row < top + visible {
        let x = inner.x + (marker_w + caret_col).min(inner.width as usize - 1) as u16;
        let y = inner.y + (caret_row - top) as u16;
        let _ = text_w;
        let buf = frame.buffer_mut();
        if let Some(cell) = buf.cell_mut((x, y)) {
            cell.set_style(theme.caret());
        }
    }
}

// Slash menu --------------------------------------------------------------------

fn draw_menu(frame: &mut Frame, app: &App, composer: Rect) {
    let entries = app.menu_entries();
    if entries.is_empty() {
        return;
    }
    let theme = &app.theme;
    let rows = entries.len().min(MAX_MENU_ROWS).min(composer.y as usize);
    if rows == 0 {
        return;
    }
    let sel = app.menu_sel.min(entries.len() - 1);
    let top = if entries.len() > rows {
        sel.saturating_sub(rows / 2).min(entries.len() - rows)
    } else {
        0
    };
    let area = Rect {
        x: 0,
        y: composer.y - rows as u16,
        width: composer.width,
        height: rows as u16,
    };
    frame.render_widget(Clear, area);
    frame.render_widget(Block::default().style(Style::new().bg(theme.surface)), area);
    let w = area.width as usize;
    let label_w = entries
        .iter()
        .map(|c| width(c.name) + 1 + width(c.usage))
        .max()
        .unwrap_or(0)
        .min(w / 2 + 6);
    let mut lines = Vec::new();
    for (i, c) in entries.iter().enumerate().skip(top).take(rows) {
        let selected = i == sel;
        let label = format!("{} {}", c.name, c.usage);
        let label = elide(label.trim_end(), label_w);
        let pad = label_w.saturating_sub(width(&label));
        let mut spans = vec![
            Span::raw(" "),
            Span::styled(
                label,
                if selected {
                    theme.list_cursor()
                } else {
                    theme.fg_style().bg(theme.surface)
                },
            ),
        ];
        let tail_room = w.saturating_sub(1 + label_w + 3);
        spans.push(Span::styled(
            " ".repeat(pad + 2),
            if selected {
                theme.list_cursor()
            } else {
                Style::new().bg(theme.surface)
            },
        ));
        let desc = elide(c.desc, tail_room);
        spans.push(Span::styled(
            desc,
            if selected {
                theme.list_cursor()
            } else {
                theme.muted_style().bg(theme.surface)
            },
        ));
        let used = spans_width(&spans);
        if selected && used < w {
            spans.push(Span::styled(" ".repeat(w - used), theme.list_cursor()));
        }
        lines.push(Line::from(clip_spans(spans, w)));
    }
    frame.render_widget(Paragraph::new(lines), area);
}

// Footer ------------------------------------------------------------------------

fn status_word(app: &App) -> Option<Span<'static>> {
    let theme = &app.theme;
    if app.status == Status::Error {
        return Some(Span::styled("error", theme.text(theme.error)));
    }
    if app.pending.iter().any(|p| p.tool == "ask_user") {
        return Some(Span::styled("answer needed", theme.text(theme.brand)));
    }
    if !app.pending.is_empty() {
        return Some(Span::styled("approval needed", theme.text(theme.brand)));
    }
    if app.queue_paused || app.status == Status::Paused {
        return Some(Span::styled("paused", theme.text(theme.warning)));
    }
    match &app.status {
        Status::Thinking => Some(Span::styled("thinking", theme.text(theme.brand))),
        Status::Streaming => Some(Span::styled("streaming", theme.text(theme.brand))),
        Status::Tool(_) => Some(Span::styled("working", theme.text(theme.brand))),
        _ => None,
    }
}

/// Hints for the footer. A docked card carries its own hint line, so the footer stays quiet.
fn footer_hints(app: &App) -> Option<&'static [hints::Hint]> {
    match &app.mode {
        Mode::Prompt => {
            if !app.menu_entries().is_empty() {
                Some(hints::SLASH_MENU)
            } else if !app.pending.is_empty() {
                None
            } else if app.running() {
                Some(hints::RUNNING)
            } else {
                Some(hints::IDLE)
            }
        }
        _ => None,
    }
}

fn draw_footer(frame: &mut Frame, app: &App, area: Rect) {
    let theme = &app.theme;
    let dot = Span::styled(" · ", theme.dim_style());
    let target = (area.width as usize).saturating_sub(2).max(20);
    let mut left: Vec<Span<'static>> = Vec::new();
    let push = |left: &mut Vec<Span<'static>>, span: Span<'static>| {
        if !left.is_empty() {
            left.push(dot.clone());
        }
        left.push(span);
    };
    if let Some((text, _)) = &app.flash {
        push(
            &mut left,
            Span::styled(text.clone(), theme.text(theme.text_success)),
        );
    }
    if !app.context_label.is_empty() {
        push(
            &mut left,
            Span::styled(
                crate::sanitize::clean_line(&app.context_label),
                theme.dim_style(),
            ),
        );
    }
    if let Some(word) = status_word(app) {
        push(&mut left, word);
    }
    if !app.queued.is_empty() {
        push(
            &mut left,
            Span::styled(
                format!("{} queued", app.queued.len()),
                theme.text(theme.warning),
            ),
        );
    }
    let tokens = app.session_chars / 4;
    let mut optional: Vec<Span<'static>> = Vec::new();
    if tokens > 0 {
        let label = if tokens < 1000 {
            format!("~{tokens} tokens")
        } else {
            format!("~{:.1}k tokens", tokens as f64 / 1000.0)
        };
        optional.push(Span::styled(label, theme.dim_style()));
    }
    let pairs = footer_hints(app);
    let (full, tight): (Vec<Span<'static>>, Vec<Span<'static>>) = match pairs {
        Some(p) => (hints::spans(p, theme, None), hints::condensed(p, theme)),
        None if !app.pending.is_empty() && app.mode == Mode::Prompt => {
            let s = vec![Span::styled(
                "approval pending · Enter review",
                theme.text(theme.brand),
            )];
            (s.clone(), s)
        }
        None => (Vec::new(), Vec::new()),
    };
    let left_w = if left.is_empty() {
        0
    } else {
        spans_width(&left) + 2
    };
    let join = |opt: &[Span<'static>], hint: &[Span<'static>]| -> Vec<Span<'static>> {
        let mut v = opt.to_vec();
        if !v.is_empty() && !hint.is_empty() {
            v.push(dot.clone());
        }
        v.extend(hint.iter().cloned());
        v
    };
    let mut right = join(&optional, &full);
    if left_w + spans_width(&right) > target {
        right = full.clone();
    }
    if left_w + spans_width(&right) > target {
        right = tight.clone();
    }
    let mut spans: Vec<Span<'static>> = Vec::new();
    if !left.is_empty() {
        spans.push(Span::raw("  "));
        spans.extend(left.clone());
    }
    let used = spans_width(&spans);
    let rw = spans_width(&right);
    if used + 1 + rw <= target {
        spans.push(Span::raw(
            " ".repeat((target).saturating_sub(used + rw).max(1)),
        ));
        spans.extend(right);
    } else {
        spans = clip_spans(spans, target);
    }
    frame.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// Polarity helper for callers that pick glyph weights.
pub fn is_light(theme: &Theme) -> bool {
    theme.polarity == Polarity::Light
}
