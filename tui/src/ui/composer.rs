//! The prompt editor: text, caret, word-wrapped rows, history. Rendering lives in `ui::mod`.

use super::text::width;
use unicode_segmentation::UnicodeSegmentation;

#[derive(Default)]
pub struct Composer {
    pub text: String,
    /// Byte offset of the caret, always on a grapheme boundary.
    pub cursor: usize,
    pub top_row: usize,
    history: Vec<String>,
    hist: Option<usize>,
    draft: String,
    want_col: Option<usize>,
}

/// One visual row: a byte range of `text`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Row {
    pub start: usize,
    pub end: usize,
}

impl Composer {
    pub fn is_empty(&self) -> bool {
        self.text.is_empty()
    }

    pub fn clear(&mut self) {
        self.text.clear();
        self.cursor = 0;
        self.top_row = 0;
        self.hist = None;
        self.want_col = None;
    }

    pub fn set(&mut self, text: &str) {
        self.text = text.to_string();
        self.cursor = self.text.len();
        self.want_col = None;
    }

    pub fn take(&mut self) -> String {
        let text = std::mem::take(&mut self.text);
        self.clear();
        text
    }

    pub fn remember(&mut self, text: &str) {
        let t = text.trim();
        if !t.is_empty() && self.history.last().is_none_or(|h| h != t) {
            self.history.push(t.to_string());
        }
        self.hist = None;
    }

    pub fn insert(&mut self, s: &str) {
        self.text.insert_str(self.cursor, s);
        self.cursor += s.len();
        self.hist = None;
        self.want_col = None;
    }

    fn prev_boundary(&self, from: usize) -> usize {
        self.text[..from]
            .grapheme_indices(true)
            .next_back()
            .map_or(0, |(i, _)| i)
    }

    fn next_boundary(&self, from: usize) -> usize {
        self.text[from..]
            .graphemes(true)
            .next()
            .map_or(from, |g| from + g.len())
    }

    pub fn backspace(&mut self) {
        if self.cursor > 0 {
            let start = self.prev_boundary(self.cursor);
            self.text.replace_range(start..self.cursor, "");
            self.cursor = start;
        }
        self.want_col = None;
    }

    pub fn delete(&mut self) {
        let end = self.next_boundary(self.cursor);
        self.text.replace_range(self.cursor..end, "");
        self.want_col = None;
    }

    pub fn left(&mut self) {
        self.cursor = self.prev_boundary(self.cursor);
        self.want_col = None;
    }

    pub fn right(&mut self) {
        self.cursor = self.next_boundary(self.cursor);
        self.want_col = None;
    }

    fn line_start(&self, from: usize) -> usize {
        self.text[..from].rfind('\n').map_or(0, |i| i + 1)
    }

    fn line_end(&self, from: usize) -> usize {
        self.text[from..]
            .find('\n')
            .map_or(self.text.len(), |i| from + i)
    }

    pub fn home(&mut self) {
        self.cursor = self.line_start(self.cursor);
        self.want_col = None;
    }

    pub fn end(&mut self) {
        self.cursor = self.line_end(self.cursor);
        self.want_col = None;
    }

    pub fn word_left(&mut self) {
        let mut i = self.cursor;
        while i > 0 {
            let p = self.prev_boundary(i);
            if self.text[p..i].chars().all(char::is_whitespace) {
                i = p;
            } else {
                break;
            }
        }
        while i > 0 {
            let p = self.prev_boundary(i);
            if self.text[p..i].chars().all(char::is_whitespace) {
                break;
            }
            i = p;
        }
        self.cursor = i;
        self.want_col = None;
    }

    pub fn word_right(&mut self) {
        let mut i = self.cursor;
        let len = self.text.len();
        while i < len {
            let n = self.next_boundary(i);
            if self.text[i..n].chars().all(char::is_whitespace) {
                i = n;
            } else {
                break;
            }
        }
        while i < len {
            let n = self.next_boundary(i);
            if self.text[i..n].chars().all(char::is_whitespace) {
                break;
            }
            i = n;
        }
        self.cursor = i;
        self.want_col = None;
    }

    pub fn delete_word_back(&mut self) {
        let end = self.cursor;
        self.word_left();
        self.text.replace_range(self.cursor..end, "");
    }

    pub fn kill_to_line_start(&mut self) {
        let start = self.line_start(self.cursor);
        if start == self.cursor && start > 0 {
            self.backspace();
            return;
        }
        self.text.replace_range(start..self.cursor, "");
        self.cursor = start;
    }

    pub fn kill_to_line_end(&mut self) {
        let end = self.line_end(self.cursor);
        if end == self.cursor {
            self.delete();
        } else {
            self.text.replace_range(self.cursor..end, "");
        }
    }

    /// Word-wrapped rows at `w` cells; one or more per logical line.
    pub fn rows(&self, w: usize) -> Vec<Row> {
        let w = w.max(1);
        let mut rows = Vec::new();
        let mut offset = 0;
        for line in self.text.split('\n') {
            wrap_line(line, offset, w, &mut rows);
            offset += line.len() + 1;
        }
        rows
    }

    /// (row, column in cells) of the caret.
    pub fn caret(&self, rows: &[Row]) -> (usize, usize) {
        let row = rows
            .iter()
            .rposition(|r| r.start <= self.cursor)
            .unwrap_or(0);
        let r = rows[row];
        let upto = self.cursor.min(r.end).max(r.start);
        (row, width(&self.text[r.start..upto]))
    }

    pub fn cursor_row(&self, w: usize) -> (usize, usize) {
        let rows = self.rows(w);
        self.caret(&rows)
    }

    fn at_col(&self, row: Row, col: usize) -> usize {
        let mut used = 0;
        let mut idx = row.start;
        for (i, g) in self.text[row.start..row.end].grapheme_indices(true) {
            let gw = width(g);
            if used + gw > col {
                return row.start + i;
            }
            used += gw;
            idx = row.start + i + g.len();
        }
        idx
    }

    /// Moves the caret one visual row. Returns false when already at the edge.
    pub fn move_vertical(&mut self, down: bool, w: usize) -> bool {
        let rows = self.rows(w);
        let (row, col) = self.caret(&rows);
        let target = if down {
            if row + 1 >= rows.len() {
                return false;
            }
            row + 1
        } else {
            if row == 0 {
                return false;
            }
            row - 1
        };
        let want = *self.want_col.get_or_insert(col);
        self.cursor = self.at_col(rows[target], want);
        true
    }

    pub fn history_prev(&mut self) -> bool {
        if self.history.is_empty() {
            return false;
        }
        let next = match self.hist {
            None => {
                self.draft = self.text.clone();
                self.history.len() - 1
            }
            Some(0) => return true,
            Some(i) => i - 1,
        };
        self.hist = Some(next);
        let text = self.history[next].clone();
        self.set(&text);
        true
    }

    pub fn history_next(&mut self) -> bool {
        let Some(i) = self.hist else { return false };
        if i + 1 < self.history.len() {
            self.hist = Some(i + 1);
            let text = self.history[i + 1].clone();
            self.set(&text);
        } else {
            self.hist = None;
            let draft = std::mem::take(&mut self.draft);
            self.set(&draft);
        }
        true
    }
}

fn wrap_line(line: &str, offset: usize, w: usize, rows: &mut Vec<Row>) {
    let mut start = 0;
    let mut used = 0;
    let mut last_break: Option<usize> = None;
    for (i, g) in line.grapheme_indices(true) {
        let gw = width(g);
        let is_space = g == " ";
        if used + gw > w && !(is_space && used <= w) {
            let cut = match last_break {
                Some(b) if b > start => b,
                _ => i,
            };
            rows.push(Row {
                start: offset + start,
                end: offset + cut,
            });
            start = cut;
            used = width(&line[cut..i]);
            last_break = None;
        }
        used += gw;
        if is_space {
            last_break = Some(i + g.len());
        }
    }
    rows.push(Row {
        start: offset + start,
        end: offset + line.len(),
    });
}
