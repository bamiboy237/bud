//! Color values are spelled only in `src/theme.rs`; nothing else may name a color.

use std::fs;
use std::path::{Path, PathBuf};

const BANNED: [&str; 19] = [
    "Rgb(",
    "Indexed(",
    "Black",
    "White",
    "Red",
    "Green",
    "Yellow",
    "Blue",
    "Magenta",
    "Cyan",
    "Gray",
    "DarkGray",
    "LightRed",
    "LightGreen",
    "LightYellow",
    "LightBlue",
    "LightMagenta",
    "LightCyan",
    "Rgb ",
];

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).expect("src directory") {
        let path = entry.expect("entry").path();
        if path.is_dir() {
            rust_files(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

/// A raw SGR color escape: `\x1b[` whose parameters, minus leading 1s and `;`s, start with 3 or 9.
fn spells_sgr_color(line: &str) -> bool {
    let mut rest = line;
    while let Some(i) = rest.find("\\x1b[") {
        let params = &rest[i + 5..];
        let trimmed = params.trim_start_matches(['1', ';']);
        if trimmed.starts_with('3') || trimmed.starts_with('9') {
            return true;
        }
        rest = &rest[i + 5..];
    }
    false
}

fn spells_hex(line: &str) -> bool {
    let bytes = line.as_bytes();
    for i in 0..bytes.len() {
        if bytes[i] == b'#' && i + 7 <= bytes.len() {
            let run = &bytes[i + 1..i + 7];
            let tail_ok = bytes.get(i + 7).is_none_or(|b| !b.is_ascii_hexdigit());
            if run.iter().all(u8::is_ascii_hexdigit) && tail_ok {
                return true;
            }
        }
    }
    false
}

#[test]
fn colors_are_defined_only_in_the_theme_module() {
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = Vec::new();
    rust_files(&src, &mut files);
    let mut offenders = Vec::new();
    for file in files {
        if file.file_name().is_some_and(|n| n == "theme.rs") {
            continue;
        }
        let text = fs::read_to_string(&file).expect("source file");
        for (n, line) in text.lines().enumerate() {
            let trimmed = line.trim_start();
            if trimmed.starts_with("//") {
                continue;
            }
            let named = BANNED
                .iter()
                .any(|b| line.contains(&format!("Color::{b}")) && !b.ends_with(' '));
            if named || spells_sgr_color(line) || spells_hex(line) {
                offenders.push(format!("{}:{}: {}", file.display(), n + 1, trimmed));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "define these colors in src/theme.rs and name them from there:\n{}",
        offenders.join("\n")
    );
}

#[test]
fn list_cursor_colors_go_through_the_theme_helper() {
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = Vec::new();
    rust_files(&src, &mut files);
    let mut offenders = Vec::new();
    for file in files {
        if file.file_name().is_some_and(|n| n == "theme.rs") {
            continue;
        }
        let text = fs::read_to_string(&file).expect("source file");
        if text.contains("block_cursor_bg") || text.contains("block_cursor_fg") {
            offenders.push(file.display().to_string());
        }
    }
    assert!(
        offenders.is_empty(),
        "use Theme::list_cursor(): {offenders:?}"
    );
}
