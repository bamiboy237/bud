//! Slash command execution. Core controls go through the HTTP client; the rest is local.

use crate::app::{App, Level, Outcome};
use crate::commands;
use crate::mascot::Kind;
use crate::protocol::Snapshot;
use crate::sanitize::clean_line;
use serde_json::Value;

fn status_text(s: &Snapshot) -> String {
    let queue = if s.queue_paused { "paused" } else { "open" };
    let active = if s.run_active { "yes" } else { "no" };
    let mut out = format!(
        "status {} · queue {queue} · run active: {active}",
        clean_line(&s.status)
    );
    out.push_str("\nqueued");
    if s.queued_messages.is_empty() {
        out.push_str("\n  empty");
    }
    for m in &s.queued_messages {
        out.push_str(&format!(
            "\n  {}: {}",
            clean_line(&m.message_id),
            crate::ui::text::elide(&clean_line(&m.text), 80)
        ));
    }
    out.push_str("\npending approvals");
    if s.pending_approvals.is_empty() {
        out.push_str("\n  none");
    }
    for a in &s.pending_approvals {
        out.push_str(&format!(
            "\n  {}: {}",
            clean_line(&a.approval_id),
            crate::ui::tool::action_title(&a.tool, &a.args)
        ));
    }
    out
}

fn connection_text(v: &Value) -> String {
    let mut lines = Vec::new();
    if let Some(map) = v.get("statuses").and_then(Value::as_object) {
        for (k, val) in map {
            lines.push(format!(
                "{} {}",
                clean_line(k),
                clean_line(val.as_str().unwrap_or(""))
            ));
        }
    }
    if v.get("restart_required")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        lines.push(
            "restart required · stop and restart bud serve, then start a new conversation".into(),
        );
    } else {
        lines.push("connections current · tool refresh requires a full server restart".into());
    }
    lines.join("\n")
}

impl App {
    pub fn run_command(&mut self, text: &str) {
        let mut parts = text.split_whitespace();
        let name = parts.next().unwrap_or("").to_lowercase();
        let rest: Vec<&str> = parts.collect();
        self.follow_bottom();
        match name.as_str() {
            "/help" | "/?" => self.note(Level::Info, commands::help_text()),
            "/exit" | "/quit" | "/q" => self.quit = true,
            "/clear" => {
                self.hide_before = self.entries.len();
                self.scroll = 0;
            }
            "/theme" => self.cmd_theme(rest.first().copied()),
            "/mascot" => self.cmd_mascot(rest.first().copied()),
            "/mode" => self.cmd_mode(rest.first().copied()),
            "/cost" => {
                let tokens = self.session_chars / 4;
                self.note(
                    Level::Info,
                    format!("~{tokens} output tokens this session (estimated from text length)"),
                );
            }
            "/model" => match rest.first() {
                Some(id) => self.switch_model((*id).to_string()),
                None => {
                    self.act(|core, _| async move {
                        match core.models().await {
                            Ok(m) => vec![Outcome::Models(m)],
                            Err(e) => vec![Outcome::Note(
                                Level::Error,
                                format!("could not list models: {e}"),
                            )],
                        }
                    });
                }
            },
            "/status" => self.act(|core, session| async move {
                match core.snapshot(&session).await {
                    Ok(s) => vec![Outcome::Note(Level::Info, status_text(&s))],
                    Err(e) => vec![Outcome::Note(Level::Error, format!("status failed: {e}"))],
                }
            }),
            "/pause" | "/resume" => {
                let action = name.trim_start_matches('/').to_string();
                self.act(move |core, session| async move {
                    match core.queue(&session, &action).await {
                        Ok(()) => vec![Outcome::Note(
                            Level::Muted,
                            if action == "pause" {
                                "queue paused"
                            } else {
                                "queue resumed"
                            }
                            .into(),
                        )],
                        Err(e) => {
                            vec![Outcome::Note(Level::Error, format!("{action} failed: {e}"))]
                        }
                    }
                });
            }
            "/stop" => self.stop_run(),
            "/cancel" => match rest.first() {
                Some(id) => {
                    let id = (*id).to_string();
                    self.act(move |core, session| async move {
                        match core.cancel_queued(&session, &id).await {
                            Ok(true) => vec![],
                            Ok(false) => vec![Outcome::Note(
                                Level::Muted,
                                "message is no longer queued".into(),
                            )],
                            Err(e) => {
                                vec![Outcome::Note(Level::Error, format!("cancel failed: {e}"))]
                            }
                        }
                    });
                }
                None => self.open_queue_picker(crate::app::PickKind::Cancel),
            },
            "/edit" => {
                if rest.is_empty() {
                    return self.open_queue_picker(crate::app::PickKind::Edit);
                }
                if rest.len() == 1 {
                    return self.begin_edit(rest[0]);
                }
                let id = rest[0].to_string();
                let body = rest[1..].join(" ");
                self.act(move |core, session| async move {
                    match core.edit_queued(&session, &id, &body).await {
                        Ok(()) => vec![],
                        Err(e) => vec![Outcome::Note(Level::Error, format!("edit failed: {e}"))],
                    }
                });
            }
            "/approve" | "/deny" => self.cmd_decide(name == "/approve", rest.first().copied()),
            "/answer" => {
                if rest.is_empty() {
                    if self.questions().is_empty() {
                        return self.note(Level::Muted, "no question is waiting");
                    }
                    self.mode = crate::app::Mode::Prompt;
                    return self.refresh_mode();
                }
                if rest.len() < 2 {
                    return self.note(Level::Muted, "usage: /answer [id] [reply]");
                }
                let key = rest[0];
                let found = self
                    .questions()
                    .into_iter()
                    .find(|q| q.approval_id == key || q.reference.as_deref() == Some(key))
                    .map(|q| q.approval_id.clone());
                match found {
                    Some(id) => self.submit_answer(id, rest[1..].join(" ")),
                    None => self.note(Level::Muted, "not a pending question"),
                }
            }
            "/connect" => match rest.first().copied() {
                Some(t @ ("gmail" | "calendar")) => {
                    let toolkit = t.to_string();
                    self.act(move |core, _| async move {
                        match core.connect(&toolkit).await {
                            Ok(url) => vec![Outcome::SetupLink {
                                label: toolkit,
                                url,
                            }],
                            Err(e) => {
                                vec![Outcome::Note(Level::Error, format!("connect failed: {e}"))]
                            }
                        }
                    });
                }
                None => {
                    self.act(|core, _| async move {
                        match core.connections(false).await {
                            Ok(v) => vec![Outcome::ConnectStates(v)],
                            Err(e) => vec![Outcome::Note(
                                Level::Error,
                                format!("could not read connections: {e}"),
                            )],
                        }
                    });
                }
                _ => self.note(Level::Muted, "connect takes gmail or calendar"),
            },
            "/connections" | "/refresh" => {
                let refresh = name == "/refresh";
                self.act(move |core, _| async move {
                    match core.connections(refresh).await {
                        Ok(v) => vec![Outcome::Note(Level::Info, connection_text(&v))],
                        Err(e) => vec![Outcome::Note(
                            Level::Error,
                            format!("connections failed: {e}"),
                        )],
                    }
                });
            }
            _ => self.note(Level::Muted, "unknown command · type /help"),
        }
    }

    fn cmd_decide(&mut self, approve: bool, key: Option<&str>) {
        let actions = self.pending.iter().filter(|p| p.tool != "ask_user").count();
        if key.is_none() && actions > 1 {
            return self.open_decide_picker(approve);
        }
        let target = match key {
            Some(k) => self
                .pending
                .iter()
                .find(|p| p.approval_id == k || p.reference.as_deref() == Some(k)),
            None => self.current_action(),
        };
        let Some(item) = target else {
            return self.note(Level::Muted, "no pending action · use /status");
        };
        if item.tool == "ask_user" {
            return self.note(Level::Muted, "that is a question · use /answer");
        }
        let id = item.approval_id.clone();
        self.decide_id(id, approve);
    }

    fn cmd_theme(&mut self, name: Option<&str>) {
        let Some(name) = name else {
            return self.open_theme_picker();
        };
        let name = name.to_lowercase();
        if self.apply_theme_pref(&name) {
            self.note(Level::Muted, format!("theme {}", self.theme.name));
        } else {
            self.note(Level::Muted, "themes: dark, light, auto");
        }
    }

    fn cmd_mascot(&mut self, name: Option<&str>) {
        let Some(name) = name else {
            return self.open_mascot_picker();
        };
        let Some(kind) = Kind::parse(name) else {
            return self.note(Level::Muted, "mascots: dog, ant");
        };
        self.mascot.set_kind(kind);
        self.sync_mascot();
        self.note(Level::Muted, format!("mascot {}", kind.name()));
    }

    fn cmd_mode(&mut self, mode: Option<&str>) {
        let Some(mode) = mode else {
            return self.open_mode_picker();
        };
        let target = mode.to_lowercase();
        self.act(move |core, _| async move {
            match core.set_mode(&target).await {
                Ok((mode, summary)) => vec![Outcome::ModeSet(mode, summary)],
                Err(e) if e.status == Some(400) => {
                    vec![Outcome::Note(
                        Level::Muted,
                        format!("unknown mode '{target}'"),
                    )]
                }
                Err(e) => vec![Outcome::Note(Level::Error, format!("mode failed: {e}"))],
            }
        });
    }
}
