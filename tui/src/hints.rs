//! Shortcut-hint vocabulary: every hint line names its keys and actions from here.
//!
//! A hint is an unpadded `(key, action)` pair; the renderer owns every space.
//! Keys keep canonical casing, actions are lowercase verbs, and each line ends with
//! the Esc pair that says what Esc does at that level.

use crate::theme::Theme;
use ratatui::style::Modifier;
use ratatui::text::Span;
use unicode_width::UnicodeWidthStr;

pub type Hint = (&'static str, &'static str);

pub mod key {
    pub const ENTER: &str = "Enter";
    pub const ESC: &str = "Esc";
    pub const CTRL_C: &str = "Ctrl+C";
    pub const CTRL_D: &str = "Ctrl+D";
    pub const CTRL_O: &str = "Ctrl+O";
    pub const TAB: &str = "Tab";
    pub const SLASH: &str = "/";
    pub const NAV: &str = "↑↓/jk";
    pub const ARROWS: &str = "↑↓";
    pub const LEFT_RIGHT: &str = "←→";
    pub const PICK: &str = "1-5";
    pub const ESC_CTRL_C: &str = "Esc/Ctrl+C";
}

pub mod action {
    pub const NAVIGATE: &str = "navigate";
    pub const SELECT: &str = "select";
    pub const SEND: &str = "send";
    pub const COPY_URL: &str = "copy url";
    pub const APPROVE: &str = "approve";
    pub const DENY: &str = "deny";
    pub const REVISE: &str = "revise";
    pub const PICK: &str = "pick";
    pub const SCROLL: &str = "scroll";
    pub const FILTER: &str = "filter";
    pub const TABS: &str = "switch";
    pub const TOOLS: &str = "tools";
    pub const COMMANDS: &str = "commands";
    pub const EXIT: &str = "exit";
    pub const BACK: &str = "back";
    pub const CANCEL: &str = "cancel";
    pub const INTERRUPT: &str = "interrupt";
    pub const COMPLETE: &str = "complete";
}

pub const NAVIGATE: Hint = (key::NAV, action::NAVIGATE);
pub const ARROWS: Hint = (key::ARROWS, action::NAVIGATE);
pub const SELECT: Hint = (key::ENTER, action::SELECT);
pub const SEND: Hint = (key::ENTER, action::SEND);
pub const COPY_URL: Hint = (key::ENTER, action::COPY_URL);
pub const APPROVE: Hint = ("y", action::APPROVE);
pub const DENY: Hint = ("n", action::DENY);
pub const REVISE: Hint = ("r", action::REVISE);
pub const PICK: Hint = (key::PICK, action::PICK);

/// The digit hint for `rows` numbered rows: a digit acts only on a row that shows it.
pub fn pick(rows: usize) -> Hint {
    let key = match rows.min(5) {
        0 | 1 => "1",
        2 => "1-2",
        3 => "1-3",
        4 => "1-4",
        _ => "1-5",
    };
    (key, action::PICK)
}
pub const TABS: Hint = (key::LEFT_RIGHT, action::TABS);
pub const FILTER: Hint = (key::SLASH, action::FILTER);
pub const SCROLL: Hint = (key::NAV, action::SCROLL);
pub const TOOLS: Hint = (key::CTRL_O, action::TOOLS);
pub const COMMANDS: Hint = (key::SLASH, action::COMMANDS);
pub const EXIT: Hint = (key::CTRL_D, action::EXIT);
pub const BACK: Hint = (key::ESC, action::BACK);
pub const CANCEL: Hint = (key::ESC, action::CANCEL);
pub const INTERRUPT: Hint = (key::ESC_CTRL_C, action::INTERRUPT);
pub const COMPLETE: Hint = (key::TAB, action::COMPLETE);

pub const APPROVAL: &[Hint] = &[APPROVE, DENY, REVISE, BACK];
pub const REVISING: &[Hint] = &[SEND, BACK];
pub const QUESTION: &[Hint] = &[NAVIGATE, SELECT, PICK, BACK];
pub const QUESTION_TABS: &[Hint] = &[TABS, NAVIGATE, SELECT, PICK, BACK];
pub const PICKER: &[Hint] = &[NAVIGATE, SELECT, PICK, CANCEL];
pub const RUNNING: &[Hint] = &[TOOLS, COMMANDS, INTERRUPT];
pub const IDLE: &[Hint] = &[TOOLS, COMMANDS, EXIT, INTERRUPT];
pub const SLASH_MENU: &[Hint] = &[ARROWS, COMPLETE, CANCEL];
pub const SETUP_URL: &[Hint] = &[COPY_URL, BACK];
pub const SETUP_PLAIN: &[Hint] = &[BACK];

/// Pairs dropped first from a keys-only fallback line; the Esc pair never drops.
const DROPPED_WHEN_TIGHT: &[Hint] = &[NAVIGATE, ARROWS, TOOLS, TABS];

/// Plain width of the composed hint line.
pub fn width(pairs: &[Hint]) -> usize {
    line(pairs).width()
}

pub fn line(pairs: &[Hint]) -> String {
    pairs
        .iter()
        .map(|(k, a)| format!("{k} {a}"))
        .collect::<Vec<_>>()
        .join("  ")
}

/// Styled spans for a hint line. With `max_width`, pairs drop from the front until it
/// fits; the last pair never drops.
pub fn spans(pairs: &[Hint], theme: &Theme, max_width: Option<usize>) -> Vec<Span<'static>> {
    let mut fitted: Vec<Hint> = pairs.to_vec();
    if let Some(max) = max_width {
        while fitted.len() > 1 && width(&fitted) > max {
            fitted.remove(0);
        }
    }
    let mut out = Vec::new();
    for (i, (k, a)) in fitted.iter().enumerate() {
        if i > 0 {
            out.push(Span::raw("  "));
        }
        out.push(Span::styled(
            (*k).to_string(),
            theme.primary_style().add_modifier(Modifier::BOLD),
        ));
        out.push(Span::styled(format!(" {a}"), theme.muted_style()));
    }
    out
}

/// Keys-only fallback for tight footers; still ends with the Esc key.
pub fn condensed(pairs: &[Hint], theme: &Theme) -> Vec<Span<'static>> {
    let mut kept: Vec<Hint> = pairs
        .iter()
        .copied()
        .filter(|p| !DROPPED_WHEN_TIGHT.contains(p))
        .collect();
    if kept.is_empty() {
        kept = pairs.to_vec();
    }
    let mut out = Vec::new();
    for (i, (k, _)) in kept.iter().enumerate() {
        if i > 0 {
            out.push(Span::styled(" · ", theme.muted_style()));
        }
        out.push(Span::styled(
            (*k).to_string(),
            theme.primary_style().add_modifier(Modifier::BOLD),
        ));
    }
    out
}

pub fn condensed_width(pairs: &[Hint]) -> usize {
    let kept: Vec<&Hint> = pairs
        .iter()
        .filter(|p| !DROPPED_WHEN_TIGHT.contains(p))
        .collect();
    let kept: Vec<&Hint> = if kept.is_empty() {
        pairs.iter().collect()
    } else {
        kept
    };
    kept.iter().map(|(k, _)| k.width()).sum::<usize>() + 3 * kept.len().saturating_sub(1)
}
