//! Markdown to pre-wrapped styled lines, with an incremental document for streaming replies.
//!
//! A reply is split into blocks at blank lines outside fences. Settled blocks are rendered
//! once per width; only the unsettled tail is re-parsed when new text arrives.

use super::text::{atoms, elide, spans_of, spans_width, width, wrap, Atom};
use crate::theme::Theme;
use pulldown_cmark::{CodeBlockKind, Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use unicode_segmentation::UnicodeSegmentation;

const BULLETS: [&str; 5] = ["• ", "▪ ", "‣ ", "⭑ ", "◦ "];

struct RBlock {
    top: u8,
    bottom: u8,
    lines: Vec<Line<'static>>,
}

enum Container {
    Quote,
    Item { marker: String, first: bool },
}

struct ListState {
    next: Option<u64>,
    depth: usize,
    items: usize,
    loose: bool,
}

#[derive(Default)]
struct Table {
    rows: Vec<Vec<Vec<Atom>>>,
    header_rows: usize,
    cell: Vec<Atom>,
    row: Vec<Vec<Atom>>,
    in_head: bool,
}

struct Renderer<'a> {
    theme: &'a Theme,
    width: usize,
    pad: usize,
    blocks: Vec<RBlock>,
    cur: Vec<Line<'static>>,
    margins: (u8, u8),
    stack: Vec<Container>,
    inline: Vec<Atom>,
    styles: Vec<Modifier>,
    links: Vec<String>,
    lists: Vec<ListState>,
    code: Option<(String, String)>,
    table: Option<Table>,
    heading: Option<HeadingLevel>,
    quote_depth: usize,
}

impl<'a> Renderer<'a> {
    fn new(theme: &'a Theme, width: usize) -> Self {
        let pad = if width >= 60 { 2 } else { 1 };
        Renderer {
            theme,
            width: width.max(4),
            pad,
            blocks: Vec::new(),
            cur: Vec::new(),
            margins: (0, 1),
            stack: Vec::new(),
            inline: Vec::new(),
            styles: Vec::new(),
            links: Vec::new(),
            lists: Vec::new(),
            code: None,
            table: None,
            heading: None,
            quote_depth: 0,
        }
    }

    fn top_level(&self) -> bool {
        self.stack.is_empty() && self.lists.is_empty()
    }

    fn begin(&mut self, top: u8, bottom: u8) {
        if self.top_level() && self.cur.is_empty() {
            self.margins = (top, bottom);
        }
    }

    fn finish(&mut self) {
        while self.cur.last().is_some_and(|l| l.spans.is_empty()) {
            self.cur.pop();
        }
        if !self.cur.is_empty() {
            let (top, bottom) = self.margins;
            self.blocks.push(RBlock {
                top,
                bottom,
                lines: std::mem::take(&mut self.cur),
            });
        }
    }

    fn finish_if_top(&mut self) {
        if self.top_level() {
            self.finish();
        }
    }

    fn prefix_width(&self) -> usize {
        self.stack
            .iter()
            .map(|c| match c {
                Container::Quote => 2,
                Container::Item { marker, .. } => super::text::width(marker),
            })
            .sum()
    }

    fn avail(&self) -> usize {
        self.width
            .saturating_sub(self.pad * 2 + self.prefix_width())
            .max(1)
    }

    fn base_style(&self) -> Style {
        if self.quote_depth > 0 {
            self.theme.muted_style()
        } else {
            self.theme.fg_style()
        }
    }

    fn prefix_spans(&mut self, first: bool) -> Vec<Span<'static>> {
        let mut out = vec![Span::raw(" ".repeat(self.pad))];
        for c in self.stack.iter_mut() {
            match c {
                Container::Quote => out.push(Span::styled("▌ ", self.theme.keyline_style())),
                Container::Item {
                    marker,
                    first: pending,
                } => {
                    if first && *pending {
                        out.push(Span::styled(marker.clone(), self.theme.fg_style()));
                    } else {
                        out.push(Span::raw(" ".repeat(super::text::width(marker))));
                    }
                }
            }
        }
        out
    }

    fn consume_markers(&mut self) {
        for c in self.stack.iter_mut() {
            if let Container::Item { first, .. } = c {
                *first = false;
            }
        }
    }

    fn emit(&mut self, rows: Vec<Vec<Span<'static>>>) {
        let first = self.prefix_spans(true);
        let rest = self.prefix_spans(false);
        for (i, row) in rows.into_iter().enumerate() {
            let mut spans = if i == 0 { first.clone() } else { rest.clone() };
            spans.extend(row);
            self.cur.push(Line::from(spans));
        }
        self.consume_markers();
    }

    fn blank(&mut self) {
        self.cur.push(Line::default());
    }

    fn current_style(&self) -> Style {
        let mut style = self.base_style();
        if let Some(level) = self.heading {
            style = heading_style(self.theme, level);
        }
        for m in &self.styles {
            style = style.add_modifier(*m);
        }
        if !self.links.is_empty() {
            style = style
                .fg(self.theme.md_link)
                .add_modifier(Modifier::UNDERLINED);
        }
        style
    }

    fn push_text(&mut self, text: &str) {
        let style = self.current_style();
        if let Some(table) = self.table.as_mut() {
            table.cell.extend(atoms(text, style));
        } else {
            self.inline.extend(atoms(text, style));
        }
    }

    fn flush_inline(&mut self) {
        if self.inline.is_empty() {
            return;
        }
        let atoms = std::mem::take(&mut self.inline);
        let avail = self.avail();
        let rows = wrap(&atoms, avail);
        let center = matches!(self.heading, Some(HeadingLevel::H1)) && self.top_level();
        let rows: Vec<Vec<Span<'static>>> = rows
            .iter()
            .map(|r| {
                let mut spans = spans_of(r);
                if center {
                    let w = spans_width(&spans);
                    let lead = avail.saturating_sub(w) / 2;
                    spans.insert(0, Span::raw(" ".repeat(lead)));
                }
                spans
            })
            .collect();
        self.emit(rows);
    }

    fn rule(&mut self) {
        let w = self.avail();
        self.emit(vec![vec![Span::styled(
            "─".repeat(w),
            self.theme.keyline_style(),
        )]]);
    }

    fn code_block(&mut self, lang: &str, code: &str) {
        let avail = self.avail();
        let border = self.theme.text(self.theme.fence_border);
        let body = self.theme.fg_style();
        let code = code.strip_suffix('\n').unwrap_or(code);
        let code = code.replace('\t', "    ");
        if avail < 9 {
            let rows = code
                .lines()
                .map(|l| vec![Span::styled(elide(l, avail), body)])
                .collect();
            self.emit(rows);
            return;
        }
        let inner = avail - 4;
        let label = format!("─ {lang} ");
        let label = if lang.is_empty() || width(&label) + 4 > avail {
            String::new()
        } else {
            label
        };
        let fill = avail.saturating_sub(2 + width(&label));
        let mut rows = vec![vec![Span::styled(
            format!("┌{label}{}┐", "─".repeat(fill)),
            border,
        )]];
        let mut lines: Vec<&str> = code.split('\n').collect();
        if lines.is_empty() {
            lines.push("");
        }
        for line in lines {
            for chunk in hard_wrap(line, inner) {
                let pad = inner.saturating_sub(width(&chunk));
                rows.push(vec![
                    Span::styled("│ ", border),
                    Span::styled(chunk, body),
                    Span::raw(" ".repeat(pad)),
                    Span::styled(" │", border),
                ]);
            }
        }
        rows.push(vec![Span::styled(
            format!("└{}┘", "─".repeat(avail - 2)),
            border,
        )]);
        self.emit(rows);
    }

    fn render_table(&mut self, table: Table) {
        let ncols = table.rows.iter().map(Vec::len).max().unwrap_or(0);
        if ncols == 0 {
            return;
        }
        let avail = self.avail();
        let key = self.theme.keyline_style();
        let cell_w = |cell: &Vec<Atom>| -> usize {
            let mut longest = 0;
            let mut cur = 0;
            for a in cell {
                if a.is_break() {
                    longest = longest.max(cur);
                    cur = 0;
                } else {
                    cur += a.w();
                }
            }
            longest.max(cur)
        };
        let mut cols: Vec<usize> = vec![3; ncols];
        for row in &table.rows {
            for (i, cell) in row.iter().enumerate() {
                cols[i] = cols[i].max(cell_w(cell));
            }
        }
        let budget = avail.saturating_sub(1 + 3 * ncols).max(ncols);
        while cols.iter().sum::<usize>() > budget {
            let Some((i, _)) = cols
                .iter()
                .enumerate()
                .filter(|(_, w)| **w > 1)
                .max_by_key(|(_, w)| **w)
            else {
                break;
            };
            cols[i] -= 1;
        }
        let rule = |l: &str, m: &str, r: &str| -> Vec<Span<'static>> {
            let mut s = String::from(l);
            for (i, w) in cols.iter().enumerate() {
                s.push_str(&"─".repeat(w + 2));
                s.push_str(if i + 1 == cols.len() { r } else { m });
            }
            vec![Span::styled(s, key)]
        };
        let mut out = vec![rule("┌", "┬", "┐")];
        for (ri, row) in table.rows.iter().enumerate() {
            let wrapped: Vec<Vec<Vec<Atom>>> = (0..ncols)
                .map(|ci| {
                    row.get(ci)
                        .map_or_else(|| vec![vec![]], |c| wrap(c, cols[ci]))
                })
                .collect();
            let height = wrapped.iter().map(Vec::len).max().unwrap_or(1);
            for line in 0..height {
                let mut spans = vec![Span::styled("│", key)];
                for (ci, cell) in wrapped.iter().enumerate() {
                    let content = cell.get(line).cloned().unwrap_or_default();
                    let w: usize = content.iter().map(Atom::w).sum();
                    spans.push(Span::raw(" "));
                    spans.extend(spans_of(&content));
                    spans.push(Span::raw(" ".repeat(cols[ci].saturating_sub(w) + 1)));
                    spans.push(Span::styled("│", key));
                }
                out.push(spans);
            }
            if ri + 1 == table.header_rows && ri + 1 < table.rows.len() {
                out.push(rule("├", "┼", "┤"));
            }
        }
        out.push(rule("└", "┴", "┘"));
        self.emit(out);
    }

    fn event(&mut self, event: Event<'_>) {
        match event {
            Event::Start(tag) => self.start(tag),
            Event::End(tag) => self.end(tag),
            Event::Text(t) => {
                if let Some((_, buf)) = self.code.as_mut() {
                    buf.push_str(&t);
                } else {
                    self.push_text(&t);
                }
            }
            Event::Code(t) => {
                let style = Style::new()
                    .fg(self.theme.md_code_inline)
                    .add_modifier(Modifier::BOLD);
                let a = atoms(&t, style);
                if let Some(table) = self.table.as_mut() {
                    table.cell.extend(a);
                } else {
                    self.inline.extend(a);
                }
            }
            Event::Html(t) | Event::InlineHtml(t) => {
                let style = self.theme.muted_style();
                let a = atoms(t.trim_end_matches('\n'), style);
                self.inline.extend(a);
            }
            Event::SoftBreak => self.push_text(" "),
            Event::HardBreak => {
                let style = self.current_style();
                let a = Atom {
                    g: "\n".into(),
                    style,
                };
                if let Some(table) = self.table.as_mut() {
                    table.cell.push(a);
                } else {
                    self.inline.push(a);
                }
            }
            Event::Rule => {
                self.begin(1, 1);
                self.rule();
                self.finish_if_top();
            }
            Event::TaskListMarker(done) => self.push_text(if done { "☑ " } else { "☐ " }),
            Event::InlineMath(t) | Event::DisplayMath(t) => self.push_text(&t),
            Event::FootnoteReference(t) => self.push_text(&format!("[{t}]")),
        }
    }

    fn start(&mut self, tag: Tag<'_>) {
        match tag {
            Tag::Paragraph => {
                if let Some(list) = self.lists.last_mut() {
                    list.loose = true;
                }
                self.begin(0, 1);
            }
            Tag::Heading { level, .. } => {
                let m = match level {
                    HeadingLevel::H1 | HeadingLevel::H2 => (2, 1),
                    _ => (1, 1),
                };
                self.begin(m.0, m.1);
                self.heading = Some(level);
            }
            Tag::BlockQuote(_) => {
                self.flush_inline();
                self.begin(1, 1);
                self.stack.push(Container::Quote);
                self.quote_depth += 1;
            }
            Tag::CodeBlock(kind) => {
                self.flush_inline();
                self.begin(1, 1);
                let lang = match kind {
                    CodeBlockKind::Fenced(info) => info
                        .split([',', ' ', '\t'])
                        .next()
                        .unwrap_or("")
                        .to_string(),
                    CodeBlockKind::Indented => String::new(),
                };
                self.code = Some((lang, String::new()));
            }
            Tag::HtmlBlock => self.begin(0, 1),
            Tag::List(start) => {
                self.flush_inline();
                self.begin(0, 1);
                let depth = self.lists.len();
                self.lists.push(ListState {
                    next: start,
                    depth,
                    items: 0,
                    loose: false,
                });
            }
            Tag::Item => {
                let (marker, gap) = {
                    let list = self.lists.last_mut().expect("item inside a list");
                    let gap = list.items > 0 && list.loose;
                    list.items += 1;
                    let marker = match list.next.as_mut() {
                        Some(n) => {
                            let m = format!("{n}. ");
                            *n += 1;
                            m
                        }
                        None => BULLETS[list.depth % BULLETS.len()].to_string(),
                    };
                    (marker, gap)
                };
                if gap {
                    self.blank();
                }
                self.stack.push(Container::Item {
                    marker,
                    first: true,
                });
            }
            Tag::Table(_) => {
                self.begin(0, 1);
                self.table = Some(Table::default());
            }
            Tag::TableHead => {
                if let Some(t) = self.table.as_mut() {
                    t.in_head = true;
                }
            }
            Tag::TableRow | Tag::TableCell => {}
            Tag::Emphasis => self.styles.push(Modifier::ITALIC),
            Tag::Strong => self.styles.push(Modifier::BOLD),
            Tag::Strikethrough => self.styles.push(Modifier::CROSSED_OUT),
            Tag::Link { dest_url, .. } => self.links.push(dest_url.to_string()),
            Tag::Image { .. } => self.push_text("[image: "),
            _ => {}
        }
    }

    fn end(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::Paragraph | TagEnd::HtmlBlock => {
                self.flush_inline();
                self.finish_if_top();
            }
            TagEnd::Heading(_) => {
                self.flush_inline();
                self.heading = None;
                self.finish_if_top();
            }
            TagEnd::BlockQuote(_) => {
                self.flush_inline();
                self.stack.pop();
                self.quote_depth = self.quote_depth.saturating_sub(1);
                self.finish_if_top();
            }
            TagEnd::CodeBlock => {
                if let Some((lang, code)) = self.code.take() {
                    self.code_block(&lang, &code);
                }
                self.finish_if_top();
            }
            TagEnd::List(_) => {
                self.flush_inline();
                self.lists.pop();
                self.finish_if_top();
            }
            TagEnd::Item => {
                self.flush_inline();
                self.stack.pop();
            }
            TagEnd::Table => {
                if let Some(table) = self.table.take() {
                    self.render_table(table);
                }
                self.finish_if_top();
            }
            TagEnd::TableHead => {
                if let Some(t) = self.table.as_mut() {
                    let row = std::mem::take(&mut t.row);
                    if !row.is_empty() {
                        t.rows.push(row);
                    }
                    t.header_rows = t.rows.len();
                    t.in_head = false;
                }
            }
            TagEnd::TableRow => {
                if let Some(t) = self.table.as_mut() {
                    let row = std::mem::take(&mut t.row);
                    t.rows.push(row);
                }
            }
            TagEnd::TableCell => {
                if let Some(t) = self.table.as_mut() {
                    let cell = std::mem::take(&mut t.cell);
                    t.row.push(cell);
                }
            }
            TagEnd::Emphasis => {
                self.styles.pop();
            }
            TagEnd::Strong => {
                self.styles.pop();
            }
            TagEnd::Strikethrough => {
                self.styles.pop();
            }
            TagEnd::Link => {
                if let Some(url) = self.links.pop() {
                    let plain = self.inline.iter().map(|a| a.g.as_str()).collect::<String>();
                    if !url.is_empty() && !plain.ends_with(&url) {
                        let style = self.theme.muted_style();
                        self.inline.extend(atoms(&format!(" ({url})"), style));
                    }
                }
            }
            TagEnd::Image => self.push_text("]"),
            _ => {}
        }
    }
}

fn heading_style(theme: &Theme, level: HeadingLevel) -> Style {
    match level {
        HeadingLevel::H1 => Style::new()
            .fg(theme.md_accent)
            .add_modifier(Modifier::BOLD | Modifier::UNDERLINED),
        HeadingLevel::H2 => Style::new()
            .fg(theme.md_accent)
            .add_modifier(Modifier::BOLD),
        HeadingLevel::H3 => Style::new().fg(theme.fg).add_modifier(Modifier::BOLD),
        _ => Style::new().fg(theme.muted).add_modifier(Modifier::BOLD),
    }
}

fn hard_wrap(line: &str, max: usize) -> Vec<String> {
    let max = max.max(1);
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut used = 0;
    for g in line.graphemes(true) {
        let w = width(g);
        if used + w > max && used > 0 {
            out.push(std::mem::take(&mut cur));
            used = 0;
        }
        cur.push_str(g);
        used += w;
    }
    out.push(cur);
    out
}

fn render_chunk(src: &str, theme: &Theme, w: usize) -> Vec<RBlock> {
    let mut options = Options::empty();
    options.insert(Options::ENABLE_TABLES);
    options.insert(Options::ENABLE_STRIKETHROUGH);
    options.insert(Options::ENABLE_TASKLISTS);
    let mut renderer = Renderer::new(theme, w);
    for event in Parser::new_ext(src, options) {
        renderer.event(event);
    }
    renderer.flush_inline();
    renderer.finish();
    renderer.blocks
}

fn compose(blocks: &[RBlock], out: &mut Vec<Line<'static>>, prev_bottom: &mut Option<u8>) {
    for block in blocks {
        if let Some(bottom) = *prev_bottom {
            for _ in 0..bottom.max(block.top) {
                out.push(Line::default());
            }
        }
        out.extend(block.lines.iter().cloned());
        *prev_bottom = Some(block.bottom);
    }
}

/// Renders a whole document at once.
pub fn render(text: &str, theme: &Theme, w: usize) -> Vec<Line<'static>> {
    let mut out = Vec::new();
    let mut prev = None;
    compose(&render_chunk(text, theme, w), &mut out, &mut prev);
    out
}

fn fence_marker(line: &str) -> Option<char> {
    let t = line.trim_start_matches(' ');
    if line.len() - t.len() > 3 {
        return None;
    }
    ['`', '~']
        .into_iter()
        .find(|&c| t.starts_with(&c.to_string().repeat(3)))
}

fn list_marker(line: &str) -> bool {
    let t = line.trim_start();
    if t.starts_with("- ") || t.starts_with("* ") || t.starts_with("+ ") {
        return true;
    }
    let digits = t.chars().take_while(char::is_ascii_digit).count();
    digits > 0 && digits < 10 && (t[digits..].starts_with(". ") || t[digits..].starts_with(") "))
}

/// Largest byte offset at or after `from` where a settled block may end, or `from`.
/// A boundary needs a blank line, then a complete non-continuing line.
fn find_boundary(src: &str, from: usize) -> usize {
    let mut boundary = from;
    let mut fence: Option<char> = None;
    let mut blank_seen = false;
    let mut block_is_list = src[from..].lines().next().is_some_and(list_marker);
    let mut offset = from;
    for raw in src[from..].split_inclusive('\n') {
        let complete = raw.ends_with('\n');
        let line = raw.trim_end_matches('\n');
        let start = offset;
        offset += raw.len();
        if let Some(c) = fence {
            if fence_marker(line) == Some(c) {
                fence = None;
            }
            continue;
        }
        if line.trim().is_empty() {
            blank_seen = complete || blank_seen;
            continue;
        }
        if blank_seen && complete && start > from {
            let indented = line.starts_with(' ') || line.starts_with('\t');
            let continues = block_is_list && list_marker(line);
            if !indented && !continues {
                boundary = start;
                block_is_list = list_marker(line);
            }
        } else if blank_seen && start == from {
            block_is_list = list_marker(line);
        }
        blank_seen = false;
        if let Some(c) = fence_marker(line) {
            fence = Some(c);
        }
    }
    boundary
}

/// A reply being streamed: settled blocks cached, only the tail re-parsed per update.
pub struct MdDoc {
    src: String,
    settled_len: usize,
    settled_count: usize,
    prev_bottom: Option<u8>,
    lines: Vec<Line<'static>>,
    key: (usize, u64),
    tail_key: Option<(usize, usize, u64)>,
}

impl Default for MdDoc {
    fn default() -> Self {
        MdDoc::new()
    }
}

impl MdDoc {
    pub fn new() -> Self {
        MdDoc {
            src: String::new(),
            settled_len: 0,
            settled_count: 0,
            prev_bottom: None,
            lines: Vec::new(),
            key: (0, 0),
            tail_key: None,
        }
    }

    pub fn push(&mut self, delta: &str) {
        self.src.push_str(delta);
        self.tail_key = None;
    }

    pub fn set(&mut self, text: &str) {
        if text.starts_with(&self.src) {
            let extra = text[self.src.len()..].to_string();
            self.push(&extra);
        } else {
            *self = MdDoc::new();
            self.src = text.to_string();
        }
    }

    pub fn text(&self) -> &str {
        &self.src
    }

    fn reset_cache(&mut self) {
        self.settled_len = 0;
        self.settled_count = 0;
        self.prev_bottom = None;
        self.lines.clear();
        self.tail_key = None;
    }

    /// Lines at `w` cells. `finished` settles the tail too, so a complete reply caches whole.
    pub fn lines(
        &mut self,
        w: usize,
        theme: &Theme,
        theme_gen: u64,
        finished: bool,
    ) -> &[Line<'static>] {
        if self.key != (w, theme_gen) {
            self.key = (w, theme_gen);
            self.reset_cache();
        }
        let boundary = if finished {
            self.src.len()
        } else {
            find_boundary(&self.src, self.settled_len)
        };
        if boundary > self.settled_len {
            self.lines.truncate(self.settled_count);
            let chunk = &self.src[self.settled_len..boundary];
            let blocks = render_chunk(chunk, theme, w);
            compose(&blocks, &mut self.lines, &mut self.prev_bottom);
            self.settled_len = boundary;
            self.settled_count = self.lines.len();
            self.tail_key = None;
        }
        let tail_key = (self.src.len(), self.settled_len, theme_gen);
        if self.tail_key != Some(tail_key) {
            self.lines.truncate(self.settled_count);
            if self.settled_len < self.src.len() {
                let blocks = render_chunk(&self.src[self.settled_len..], theme, w);
                let mut prev = self.prev_bottom;
                compose(&blocks, &mut self.lines, &mut prev);
            }
            self.tail_key = Some(tail_key);
        }
        &self.lines
    }
}

pub fn plain(lines: &[Line<'_>]) -> Vec<String> {
    lines
        .iter()
        .map(|l| l.spans.iter().map(|s| s.content.as_ref()).collect())
        .collect()
}
