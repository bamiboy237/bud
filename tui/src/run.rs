//! The event loop: terminal input, server feed, and animation ticks, one draw per wake-up.

use crate::app::{App, AppMsg};
use crate::terminal::{self, Term};
use crate::ui;
use anyhow::Result;
use crossterm::event::{self, Event as TermEvent, KeyEventKind};
use std::time::Duration;
use tokio::sync::mpsc::UnboundedReceiver;

pub async fn run(app: &mut App, term: &mut Term, rx: &mut UnboundedReceiver<AppMsg>) -> Result<()> {
    // A dedicated blocking reader keeps key-to-frame latency flat; the async stream adds jitter.
    let (input_tx, mut input) = tokio::sync::mpsc::unbounded_channel::<TermEvent>();
    std::thread::spawn(move || {
        while let Ok(ev) = event::read() {
            if input_tx.send(ev).is_err() {
                break;
            }
        }
    });
    let mut tick = tokio::time::interval(Duration::from_millis(100));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut terminate = signal_terminate();
    let mut dirty = true;
    loop {
        if dirty {
            if app.redraw {
                term.clear()?;
                app.redraw = false;
            }
            terminal::draw_synced(term, |f| ui::draw(f, app))?;
            for text in app.clipboard_out.drain(..) {
                terminal::copy_osc52(&text);
            }
            dirty = false;
        }
        tokio::select! {
            ev = input.recv() => match ev {
                Some(event) => {
                    dirty |= handle_term(app, event);
                    // Drain whatever else is already queued so one frame covers a burst.
                    while let Ok(event) = input.try_recv() {
                        dirty |= handle_term(app, event);
                    }
                }
                None => break,
            },
            msg = rx.recv() => {
                match msg {
                    Some(msg) => app.on_msg(msg),
                    None => break,
                }
                let mut n = 0;
                while let Ok(msg) = rx.try_recv() {
                    app.on_msg(msg);
                    n += 1;
                    if n >= 512 {
                        break;
                    }
                }
                dirty = true;
            },
            _ = tick.tick() => {
                #[cfg(debug_assertions)]
                if std::env::var_os("BUD_TUI_TEST_PANIC").is_some() {
                    panic!("BUD_TUI_TEST_PANIC set: simulated failure");
                }
                if app.on_tick() {
                    dirty = true;
                }
            },
            _ = terminate.recv() => app.quit = true,
        }
        if app.quit {
            break;
        }
    }
    Ok(())
}

fn handle_term(app: &mut App, event: TermEvent) -> bool {
    match event {
        TermEvent::Key(key) if key.kind != KeyEventKind::Release => {
            app.on_key(key);
            true
        }
        TermEvent::Paste(text) => {
            app.on_paste(&text);
            true
        }
        TermEvent::Mouse(m) => {
            app.on_mouse(m);
            true
        }
        TermEvent::Resize(_, _) => true,
        TermEvent::FocusGained => true,
        _ => false,
    }
}

struct Terminate(Option<tokio::signal::unix::Signal>);

impl Terminate {
    async fn recv(&mut self) {
        match self.0.as_mut() {
            Some(s) => {
                s.recv().await;
            }
            None => std::future::pending::<()>().await,
        }
    }
}

fn signal_terminate() -> Terminate {
    Terminate(tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).ok())
}
