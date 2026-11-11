//! Neutralizes terminal control sequences and bidi tricks in model- and remote-originated text.

#[derive(Clone, Copy, Default, PartialEq, Eq)]
enum Mode {
    #[default]
    Text,
    Escape,
    Csi,
    /// OSC, DCS, SOS, PM, APC: runs until BEL or ST.
    String,
    StringEscape,
}

/// Streaming filter: an escape sequence split across two chunks is still removed whole.
#[derive(Default)]
pub struct Filter {
    mode: Mode,
}

impl Filter {
    /// Cleans one chunk. Keeps `\n` and `\t`; drops every other control character.
    pub fn push(&mut self, input: &str) -> String {
        let mut out = String::with_capacity(input.len());
        for c in input.chars() {
            match self.mode {
                Mode::Text => match c {
                    '\x1b' => self.mode = Mode::Escape,
                    '\n' | '\t' => out.push(c),
                    '\r' => {}
                    c if c.is_control() => {}
                    '\u{200e}' | '\u{200f}' | '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}' => {
                    }
                    '\u{feff}' | '\u{200b}' | '\u{2060}' => {}
                    c => out.push(c),
                },
                Mode::Escape => {
                    self.mode = match c {
                        '[' => Mode::Csi,
                        ']' | 'P' | 'X' | '^' | '_' => Mode::String,
                        _ => Mode::Text,
                    };
                }
                Mode::Csi => {
                    if ('\u{40}'..='\u{7e}').contains(&c) {
                        self.mode = Mode::Text;
                    }
                }
                Mode::String => match c {
                    '\u{7}' => self.mode = Mode::Text,
                    '\x1b' => self.mode = Mode::StringEscape,
                    _ => {}
                },
                Mode::StringEscape => {
                    self.mode = if c == '\\' { Mode::Text } else { Mode::String };
                }
            }
        }
        out
    }
}

/// Cleans a complete string.
pub fn clean(input: &str) -> String {
    Filter::default().push(input)
}

/// One line: controls removed and every run of whitespace collapsed to one space.
pub fn clean_line(input: &str) -> String {
    clean(input)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}
