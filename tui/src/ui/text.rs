//! Display-width helpers: grapheme atoms, wrapping, eliding. All widths are terminal cells.

use ratatui::style::Style;
use ratatui::text::{Line, Span};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

pub fn width(s: &str) -> usize {
    UnicodeWidthStr::width(s)
}

/// One grapheme cluster with its style.
#[derive(Clone, Debug)]
pub struct Atom {
    pub g: String,
    pub style: Style,
}

impl Atom {
    pub fn w(&self) -> usize {
        width(&self.g)
    }

    pub fn is_space(&self) -> bool {
        self.g == " "
    }

    pub fn is_break(&self) -> bool {
        self.g == "\n"
    }
}

pub fn atoms(text: &str, style: Style) -> Vec<Atom> {
    let mut out = Vec::new();
    for g in text.graphemes(true) {
        if g == "\t" {
            out.extend((0..4).map(|_| Atom {
                g: " ".into(),
                style,
            }));
        } else {
            out.push(Atom {
                g: g.to_string(),
                style,
            });
        }
    }
    out
}

/// Merges consecutive same-style atoms into spans.
pub fn spans_of(atoms: &[Atom]) -> Vec<Span<'static>> {
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut buf = String::new();
    let mut style = Style::default();
    for atom in atoms {
        if !buf.is_empty() && atom.style != style {
            spans.push(Span::styled(std::mem::take(&mut buf), style));
        }
        style = atom.style;
        buf.push_str(&atom.g);
    }
    if !buf.is_empty() {
        spans.push(Span::styled(buf, style));
    }
    spans
}

pub fn line_of(atoms: &[Atom]) -> Line<'static> {
    Line::from(spans_of(atoms))
}

/// Greedy word wrap. `\n` atoms force a break; a word longer than the row breaks at the row.
pub fn wrap(atoms: &[Atom], width: usize) -> Vec<Vec<Atom>> {
    let width = width.max(1);
    let mut rows: Vec<Vec<Atom>> = Vec::new();
    let mut row: Vec<Atom> = Vec::new();
    let mut used = 0usize;
    let mut i = 0;
    while i < atoms.len() {
        let atom = &atoms[i];
        if atom.is_break() {
            rows.push(std::mem::take(&mut row));
            used = 0;
            i += 1;
            continue;
        }
        if atom.is_space() {
            if used > 0 && used < width {
                row.push(atom.clone());
                used += 1;
            }
            i += 1;
            continue;
        }
        let start = i;
        let mut word_w = 0;
        while i < atoms.len() && !atoms[i].is_space() && !atoms[i].is_break() {
            word_w += atoms[i].w();
            i += 1;
        }
        let word = &atoms[start..i];
        if used + word_w > width && used > 0 {
            while row.last().is_some_and(Atom::is_space) {
                row.pop();
            }
            rows.push(std::mem::take(&mut row));
            used = 0;
        }
        if word_w <= width {
            row.extend(word.iter().cloned());
            used += word_w;
        } else {
            for atom in word {
                let w = atom.w();
                if used + w > width && used > 0 {
                    rows.push(std::mem::take(&mut row));
                    used = 0;
                }
                row.push(atom.clone());
                used += w;
            }
        }
    }
    while row.last().is_some_and(Atom::is_space) {
        row.pop();
    }
    if !row.is_empty() || rows.is_empty() {
        rows.push(row);
    }
    rows
}

/// Truncates to `max` cells, ending in `…` when anything was cut.
pub fn elide(s: &str, max: usize) -> String {
    if width(s) <= max {
        return s.to_string();
    }
    if max == 0 {
        return String::new();
    }
    let mut out = String::new();
    let mut used = 0;
    for g in s.graphemes(true) {
        let w = width(g);
        if used + w + 1 > max {
            break;
        }
        out.push_str(g);
        used += w;
    }
    out.push('…');
    out
}

/// Cuts at word boundaries when it can, so a message never ends mid-word.
pub fn elide_words(s: &str, max: usize) -> String {
    if width(s) <= max {
        return s.to_string();
    }
    let mut kept = String::new();
    for word in s.split(' ') {
        let candidate = if kept.is_empty() {
            word.to_string()
        } else {
            format!("{kept} {word}")
        };
        if width(&candidate) + 1 > max {
            break;
        }
        kept = candidate;
    }
    if kept.is_empty() {
        elide(s, max)
    } else {
        format!("{kept}…")
    }
}

/// Clips a styled line to `max` cells without adding an ellipsis.
pub fn clip_spans(spans: Vec<Span<'static>>, max: usize) -> Vec<Span<'static>> {
    let mut out = Vec::new();
    let mut used = 0;
    for span in spans {
        let w = width(&span.content);
        if used + w <= max {
            used += w;
            out.push(span);
            continue;
        }
        let mut cut = String::new();
        for g in span.content.graphemes(true) {
            let gw = width(g);
            if used + gw > max {
                break;
            }
            cut.push_str(g);
            used += gw;
        }
        if !cut.is_empty() {
            out.push(Span::styled(cut, span.style));
        }
        break;
    }
    out
}

pub fn spans_width(spans: &[Span<'_>]) -> usize {
    spans.iter().map(|s| width(&s.content)).sum()
}
