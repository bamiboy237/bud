//! Terminal lifecycle: raw mode, alternate screen, restore on every exit path including panics.

use anyhow::Result;
use crossterm::event::{
    DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
    KeyboardEnhancementFlags, PopKeyboardEnhancementFlags, PushKeyboardEnhancementFlags,
};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, BeginSynchronizedUpdate, EndSynchronizedUpdate,
    EnterAlternateScreen, LeaveAlternateScreen,
};
use crossterm::{cursor, execute};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;
use std::io::{stdout, Stdout, Write};
use std::sync::atomic::{AtomicBool, Ordering};

pub type Term = Terminal<CrosstermBackend<Stdout>>;

static ENTERED: AtomicBool = AtomicBool::new(false);
static ENHANCED: AtomicBool = AtomicBool::new(false);
static MOUSE: AtomicBool = AtomicBool::new(false);

/// Terminals known to speak the kitty keyboard protocol, so Shift+Enter is distinguishable.
fn kitty_keyboard() -> bool {
    let program = std::env::var("TERM_PROGRAM").unwrap_or_default();
    let term = std::env::var("TERM").unwrap_or_default();
    matches!(program.as_str(), "iTerm.app" | "WezTerm" | "ghostty")
        || term.contains("kitty")
        || term.contains("ghostty")
}

pub fn enter(mouse: bool) -> Result<Term> {
    enable_raw_mode()?;
    let mut out = stdout();
    execute!(
        out,
        EnterAlternateScreen,
        EnableBracketedPaste,
        cursor::Hide
    )?;
    if mouse {
        execute!(out, EnableMouseCapture)?;
        MOUSE.store(true, Ordering::SeqCst);
    }
    if kitty_keyboard() {
        execute!(
            out,
            PushKeyboardEnhancementFlags(KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES)
        )?;
        ENHANCED.store(true, Ordering::SeqCst);
    }
    ENTERED.store(true, Ordering::SeqCst);
    install_panic_hook();
    Ok(Terminal::new(CrosstermBackend::new(stdout()))?)
}

/// Idempotent: safe to call from a panic hook and again on the normal path.
pub fn leave() {
    if !ENTERED.swap(false, Ordering::SeqCst) {
        return;
    }
    let mut out = stdout();
    if ENHANCED.swap(false, Ordering::SeqCst) {
        let _ = execute!(out, PopKeyboardEnhancementFlags);
    }
    if MOUSE.swap(false, Ordering::SeqCst) {
        let _ = execute!(out, DisableMouseCapture);
    }
    let _ = execute!(
        out,
        DisableBracketedPaste,
        cursor::Show,
        LeaveAlternateScreen
    );
    let _ = disable_raw_mode();
    let _ = out.flush();
}

fn install_panic_hook() {
    let original = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        leave();
        original(info);
    }));
}

/// One synchronized-output frame: the terminal paints it atomically, so nothing tears.
pub fn draw_synced(term: &mut Term, f: impl FnOnce(&mut ratatui::Frame)) -> Result<()> {
    execute!(stdout(), BeginSynchronizedUpdate)?;
    let result = term.draw(f).map(|_| ());
    execute!(stdout(), EndSynchronizedUpdate)?;
    Ok(result?)
}

/// OSC 52: asks the terminal to copy `text` to the system clipboard.
pub fn copy_osc52(text: &str) {
    let mut out = stdout();
    let _ = write!(out, "\x1b]52;c;{}\x07", base64(text.as_bytes()));
    let _ = out.flush();
}

fn base64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let n = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        out.push(T[(n >> 18) as usize & 63] as char);
        out.push(T[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            T[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            T[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}
