//! Application state and the event reducer: SSE events in, view state out.
//!
//! The reducer is synchronous. Anything that needs the network is spawned through
//! `act`, and its result comes back as an `Outcome` message.

use crate::client::{Core, CoreError};
use crate::mascot::{self, Mascot};
use crate::protocol::{reply_text, Event, ModelInfo, PendingApproval, PendingSetup, Snapshot};
use crate::sanitize::{self, Filter};
use crate::sse::Feed;
use crate::theme::{ColorDepth, Theme};
use crate::ui::composer::Composer;
use crate::ui::markdown::MdDoc;
use ratatui::text::Line;
use serde_json::{Map, Value};
use std::collections::HashSet;
use std::future::Future;
use std::time::{Duration, Instant};
use tokio::sync::mpsc::UnboundedSender;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Level {
    Info,
    Success,
    Warn,
    Error,
    Muted,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum UserState {
    Pending,
    Queued,
    Sent,
    Failed,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ToolStatus {
    Running,
    Done,
    Failed,
}

pub struct LineCache {
    pub width: usize,
    pub theme_gen: u64,
    pub lines: Vec<Line<'static>>,
}

pub struct UserEntry {
    pub id: Option<String>,
    pub text: String,
    pub state: UserState,
    pub cache: Option<LineCache>,
}

pub struct AssistantEntry {
    pub request: String,
    pub run_id: String,
    pub blocks: Vec<(String, String)>,
    pub doc: MdDoc,
    pub streaming: bool,
    pub filter: Filter,
}

pub struct ToolEntry {
    pub call_id: String,
    pub tool: String,
    pub args: Map<String, Value>,
    pub status: ToolStatus,
    pub result: Option<String>,
    pub interrupted: bool,
    pub expanded: bool,
}

pub struct NoteEntry {
    pub level: Level,
    pub text: String,
    pub cache: Option<LineCache>,
}

pub enum Entry {
    Banner,
    User(UserEntry),
    Assistant(Box<AssistantEntry>),
    Tool(ToolEntry),
    Note(NoteEntry),
}

#[derive(Clone, PartialEq, Eq, Debug)]
pub enum Status {
    Idle,
    Thinking,
    Streaming,
    Tool(String),
    Paused,
    Waiting,
    Error,
}

#[derive(Clone, PartialEq, Eq, Debug)]
pub enum Mode {
    Prompt,
    Approval,
    Revising(String),
    Question,
    Answering(String),
    Editing(String),
    Setup,
    Picker,
}

#[derive(Clone, Debug)]
pub struct SetupCard {
    pub capability: String,
    pub label: String,
    pub status: String,
    pub kind: Option<String>,
    pub url: Option<String>,
    pub steps: Vec<String>,
    pub reason: Option<String>,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum PickKind {
    Model,
    Theme,
    Mascot,
    Mode,
    Connect,
    Cancel,
    Edit,
    Approve,
    Deny,
}

pub struct PickItem {
    pub label: String,
    pub detail: String,
    pub value: String,
}

/// What Esc puts back after a live preview.
pub enum Revert {
    Theme(Box<Theme>, String),
    Mascot(mascot::Kind),
}

pub struct Picker {
    pub kind: PickKind,
    pub title: String,
    pub items: Vec<PickItem>,
    /// Index into the visible (filtered) rows.
    pub selected: usize,
    pub current: Option<String>,
    pub filter: String,
    pub search: bool,
    pub empty: String,
    /// The composer text the picker was opened from; Esc restores it.
    pub draft: String,
    pub revert: Option<Revert>,
}

impl Picker {
    /// Indices of the items that match the filter, in order.
    pub fn visible(&self) -> Vec<usize> {
        let f = self.filter.to_lowercase();
        (0..self.items.len())
            .filter(|&i| {
                f.is_empty()
                    || self.items[i].label.to_lowercase().contains(&f)
                    || self.items[i].detail.to_lowercase().contains(&f)
            })
            .collect()
    }
}

pub enum Outcome {
    Note(Level, String),
    Resync(Box<Snapshot>),
    Models(Vec<ModelInfo>),
    ConnectStates(Value),
    ModelSet(String),
    ModeSet(String, String),
    SetupLink { label: String, url: String },
    Decided { id: String, approved: bool },
    Answered(String),
    Sent { id: String, duplicate: bool },
    SendFailed { id: String, error: String },
    Stop(bool),
}

pub enum AppMsg {
    Feed(Feed),
    Outcome(Outcome),
}

pub struct App {
    pub theme: Theme,
    pub theme_gen: u64,
    pub depth: ColorDepth,
    pub core: Option<Core>,
    pub tx: Option<UnboundedSender<AppMsg>>,
    pub post_tx: Option<UnboundedSender<(String, String)>>,
    pub session: String,
    pub model: String,
    pub approval_mode: String,
    pub context_label: String,
    pub entries: Vec<Entry>,
    pub hide_before: usize,
    pub status: Status,
    pub active_run: Option<String>,
    pub run_start: Option<u64>,
    pub first_delta: Option<Instant>,
    pub stream_chars: usize,
    pub session_chars: usize,
    pub queue_paused: bool,
    pub pause_reason: Option<String>,
    pub queued: Vec<(String, String)>,
    pub pending: Vec<PendingApproval>,
    pub setups: Vec<SetupCard>,
    pub mode: Mode,
    pub composer: Composer,
    pub picker: Option<Picker>,
    pub cmd_draft: String,
    pub theme_pref: String,
    pub q_tab: usize,
    pub q_sel: usize,
    pub scroll: usize,
    pub last_total: usize,
    pub tools_open: bool,
    pub mascot: Mascot,
    pub reconnecting: bool,
    pub flash: Option<(String, Instant)>,
    pub seen_requests: HashSet<String>,
    pub menu_sel: usize,
    pub menu_open: bool,
    pub quit: bool,
    pub clock: Option<u64>,
    pub started: Instant,
    pub mouse: bool,
    pub clipboard_out: Vec<String>,
    pub hits: Vec<(u16, usize)>,
    pub sent_this_run: bool,
    pub models: Vec<ModelInfo>,
    pub view_h: usize,
    pub composer_width: usize,
    pub redraw: bool,
    pub card_scroll: usize,
    pub card_max: std::cell::Cell<usize>,
}

pub fn context_label() -> String {
    let cwd = std::env::current_dir().ok();
    let name = cwd
        .as_ref()
        .and_then(|p| p.file_name())
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let branch = cwd
        .as_ref()
        .and_then(|p| std::fs::read_to_string(p.join(".git/HEAD")).ok())
        .map(|h| h.trim().trim_start_matches("ref: refs/heads/").to_string())
        .filter(|b| !b.is_empty() && b.len() < 40 && !b.contains(' '));
    match branch {
        Some(b) => format!("{name}:{b}"),
        None => name,
    }
}

impl App {
    pub fn new(theme: Theme, kind: mascot::Kind, model: &str) -> Self {
        let depth = theme.depth;
        let seed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0x9e37_79b9);
        App {
            theme,
            theme_gen: 1,
            depth,
            core: None,
            tx: None,
            post_tx: None,
            session: String::new(),
            model: model.to_string(),
            approval_mode: "strict".into(),
            context_label: context_label(),
            entries: vec![Entry::Banner],
            hide_before: 0,
            status: Status::Idle,
            active_run: None,
            run_start: None,
            first_delta: None,
            stream_chars: 0,
            session_chars: 0,
            queue_paused: false,
            pause_reason: None,
            queued: Vec::new(),
            pending: Vec::new(),
            setups: Vec::new(),
            mode: Mode::Prompt,
            composer: Composer::default(),
            picker: None,
            cmd_draft: String::new(),
            theme_pref: "auto".into(),
            q_tab: 0,
            q_sel: 0,
            scroll: 0,
            last_total: 0,
            tools_open: false,
            mascot: Mascot::new(kind, seed, true),
            reconnecting: false,
            flash: None,
            seen_requests: HashSet::new(),
            menu_sel: 0,
            menu_open: true,
            quit: false,
            clock: None,
            started: Instant::now(),
            mouse: true,
            clipboard_out: Vec::new(),
            hits: Vec::new(),
            sent_this_run: false,
            models: Vec::new(),
            view_h: 20,
            composer_width: 40,
            redraw: false,
            card_scroll: 0,
            card_max: std::cell::Cell::new(0),
        }
    }

    pub fn attach(&mut self, core: Core, tx: UnboundedSender<AppMsg>, session: String) {
        // Messages go out through one ordered worker, so a burst reaches the core in send order.
        let (post_tx, mut post_rx) = tokio::sync::mpsc::unbounded_channel::<(String, String)>();
        let (worker_core, worker_session, worker_tx) = (core.clone(), session.clone(), tx.clone());
        tokio::spawn(async move {
            while let Some((id, text)) = post_rx.recv().await {
                let outcome = match worker_core.post_message(&worker_session, &text, &id).await {
                    Ok(duplicate) => Outcome::Sent { id, duplicate },
                    Err(e) => Outcome::SendFailed {
                        id,
                        error: e.to_string(),
                    },
                };
                if worker_tx.send(AppMsg::Outcome(outcome)).is_err() {
                    break;
                }
            }
        });
        self.post_tx = Some(post_tx);
        self.core = Some(core);
        self.tx = Some(tx);
        self.session = session;
    }

    /// Seconds since the run began; clears the run clock.
    fn take_elapsed(&mut self) -> Option<f32> {
        let now = self.now_ms();
        self.run_start
            .take()
            .map(|s| now.saturating_sub(s) as f32 / 1000.0)
    }

    pub fn now_ms(&self) -> u64 {
        self.clock
            .unwrap_or_else(|| self.started.elapsed().as_millis() as u64)
    }

    pub fn running(&self) -> bool {
        matches!(
            self.status,
            Status::Thinking | Status::Streaming | Status::Tool(_)
        )
    }

    pub fn mascot_state(&self) -> mascot::State {
        if self.status == Status::Error {
            mascot::State::Error
        } else if !self.pending.is_empty() {
            mascot::State::Waiting
        } else if self.running() {
            mascot::State::Working
        } else {
            mascot::State::Idle
        }
    }

    pub fn sync_mascot(&mut self) -> bool {
        self.mascot.set_state(self.mascot_state())
    }

    /// Advances animations; returns whether anything visible changed.
    pub fn on_tick(&mut self) -> bool {
        let mut dirty = self.sync_mascot();
        dirty |= self.mascot.tick(Instant::now());
        if self.running() || !self.pending.is_empty() && self.mode == Mode::Prompt {
            dirty = true;
        }
        if let Some((_, at)) = &self.flash {
            if at.elapsed() > Duration::from_secs(3) {
                self.flash = None;
                dirty = true;
            }
        }
        dirty
    }

    pub fn flash(&mut self, text: &str) {
        self.flash = Some((text.to_string(), Instant::now()));
    }

    pub fn set_theme(&mut self, theme: Theme) {
        self.theme = theme;
        self.theme_gen += 1;
    }

    // Spawning ---------------------------------------------------------------

    pub fn act<F, Fut>(&self, f: F)
    where
        F: FnOnce(Core, String) -> Fut,
        Fut: Future<Output = Vec<Outcome>> + Send + 'static,
    {
        let (Some(core), Some(tx)) = (self.core.clone(), self.tx.clone()) else {
            return;
        };
        let fut = f(core, self.session.clone());
        tokio::spawn(async move {
            for outcome in fut.await {
                if tx.send(AppMsg::Outcome(outcome)).is_err() {
                    return;
                }
            }
        });
    }

    // Transcript helpers -----------------------------------------------------

    pub fn note(&mut self, level: Level, text: impl Into<String>) {
        let text = sanitize::clean(&text.into());
        self.entries.push(Entry::Note(NoteEntry {
            level,
            text,
            cache: None,
        }));
        self.after_push();
    }

    fn after_push(&mut self) {
        if self.entries.len() > 4000 {
            let drop = self.entries.len() - 3000;
            self.entries.drain(1..=drop);
            self.hide_before = self.hide_before.saturating_sub(drop);
        }
    }

    pub fn follow_bottom(&mut self) {
        self.scroll = 0;
    }

    fn assistant_idx(&self, request: &str) -> Option<usize> {
        self.entries
            .iter()
            .rposition(|e| matches!(e, Entry::Assistant(a) if a.request == request))
    }

    fn finish_streaming(&mut self) {
        for entry in self.entries.iter_mut() {
            if let Entry::Assistant(a) = entry {
                a.streaming = false;
            }
        }
    }

    // Snapshot ---------------------------------------------------------------

    /// Seeds the transcript and pending cards from the startup snapshot.
    pub fn load_snapshot(&mut self, snap: &Snapshot) {
        for turn in &snap.transcript {
            let text = sanitize::clean(&turn.text);
            if turn.role == "user" {
                self.entries.push(Entry::User(UserEntry {
                    id: None,
                    text,
                    state: UserState::Sent,
                    cache: None,
                }));
            } else {
                let mut doc = MdDoc::new();
                doc.set(&text);
                self.entries.push(Entry::Assistant(Box::new(AssistantEntry {
                    request: String::new(),
                    run_id: String::new(),
                    blocks: vec![(String::new(), text)],
                    doc,
                    streaming: false,
                    filter: Filter::default(),
                })));
            }
        }
        self.apply_state(snap);
    }

    /// Reconciles the live state (cards, queue, run) with the server's view.
    pub fn apply_state(&mut self, snap: &Snapshot) {
        self.queue_paused = snap.queue_paused;
        // The snapshot is authoritative: replace the local queue, never merge.
        self.queued = snap
            .queued_messages
            .iter()
            .map(|m| (m.message_id.clone(), sanitize::clean(&m.text)))
            .collect();
        let live: HashSet<&str> = snap
            .queued_messages
            .iter()
            .map(|m| m.message_id.as_str())
            .collect();
        for entry in self.entries.iter_mut() {
            if let Entry::User(u) = entry {
                if u.state == UserState::Queued
                    && u.id.as_deref().is_some_and(|i| !live.contains(i))
                {
                    u.state = UserState::Sent;
                    u.cache = None;
                }
            }
        }
        self.queue_paused = snap.queue_paused;
        for item in &snap.pending_approvals {
            self.add_approval(item.clone());
        }
        self.pending.retain(|p| {
            snap.pending_approvals
                .iter()
                .any(|s| s.approval_id == p.approval_id)
        });
        for card in &snap.pending_setup {
            self.upsert_setup(card);
        }
        if snap.run_active && !self.running() {
            self.status = Status::Thinking;
            let now = self.now_ms();
            self.run_start.get_or_insert(now);
        }
        if !snap.run_active && self.running() {
            self.status = Status::Idle;
        }
        self.refresh_mode();
    }

    fn upsert_setup(&mut self, p: &PendingSetup) {
        let card = SetupCard {
            capability: sanitize::clean_line(&p.capability),
            label: sanitize::clean_line(p.label.as_deref().unwrap_or(&p.capability)),
            status: p.status.clone().unwrap_or_else(|| "waiting".into()),
            kind: p.kind.clone(),
            url: p
                .url
                .as_deref()
                .map(sanitize::clean_line)
                .filter(|u| !u.is_empty()),
            steps: p.steps.iter().map(|s| sanitize::clean_line(s)).collect(),
            reason: p
                .reason
                .as_deref()
                .map(sanitize::clean_line)
                .filter(|r| !r.is_empty()),
        };
        match self
            .setups
            .iter_mut()
            .find(|c| c.capability == card.capability)
        {
            Some(existing) => *existing = card,
            None => self.setups.push(card),
        }
    }

    fn add_approval(&mut self, item: PendingApproval) {
        self.card_scroll = 0;
        if !self
            .pending
            .iter()
            .any(|p| p.approval_id == item.approval_id)
        {
            self.pending.push(item);
        }
    }

    /// Chooses the bottom pane from pending work, unless the user is mid-input elsewhere.
    pub fn refresh_mode(&mut self) {
        match &self.mode {
            Mode::Revising(id) | Mode::Answering(id) => {
                if !self.pending.iter().any(|p| &p.approval_id == id) {
                    self.mode = Mode::Prompt;
                }
            }
            Mode::Picker => {}
            Mode::Editing(id) => {
                if !self.queued.iter().any(|(q, _)| q == id) {
                    self.mode = Mode::Prompt;
                }
            }
            _ => {
                let has_question = self.pending.iter().any(|p| p.tool == "ask_user");
                let has_action = self.pending.iter().any(|p| p.tool != "ask_user");
                let waiting_setup = self.setups.iter().any(|c| c.status == "waiting");
                self.mode = if has_question {
                    Mode::Question
                } else if has_action {
                    Mode::Approval
                } else if waiting_setup && self.mode == Mode::Setup {
                    Mode::Setup
                } else {
                    Mode::Prompt
                };
                if self.mode == Mode::Question {
                    let n = self.questions().len();
                    self.q_tab = self.q_tab.min(n.saturating_sub(1));
                }
            }
        }
    }

    pub fn questions(&self) -> Vec<&PendingApproval> {
        self.pending
            .iter()
            .filter(|p| p.tool == "ask_user")
            .collect()
    }

    pub fn current_action(&self) -> Option<&PendingApproval> {
        self.pending.iter().find(|p| p.tool != "ask_user")
    }

    pub fn current_setup(&self) -> Option<&SetupCard> {
        self.setups
            .iter()
            .find(|c| c.status == "waiting")
            .or_else(|| self.setups.last())
    }

    // Feed -------------------------------------------------------------------

    pub fn on_msg(&mut self, msg: AppMsg) {
        match msg {
            AppMsg::Feed(Feed::Event(event)) => self.on_event(&event),
            AppMsg::Feed(Feed::Connected) => {
                if self.reconnecting {
                    self.reconnecting = false;
                    self.resync();
                }
            }
            AppMsg::Feed(Feed::Lost) => self.reconnecting = true,
            AppMsg::Feed(Feed::Fatal(code)) => {
                self.reconnecting = false;
                self.note(
                    Level::Error,
                    format!("The session is no longer available (HTTP {code}). Restart bud chat."),
                );
            }
            AppMsg::Outcome(outcome) => self.on_outcome(outcome),
        }
    }

    fn resync(&mut self) {
        self.act(|core, session| async move {
            match core.snapshot(&session).await {
                Ok(s) => vec![Outcome::Resync(Box::new(s))],
                Err(_) => vec![],
            }
        });
    }

    pub fn on_outcome(&mut self, outcome: Outcome) {
        match outcome {
            Outcome::Note(level, text) => self.note(level, text),
            Outcome::Resync(snap) => self.apply_state(&snap),
            Outcome::ConnectStates(states) => self.open_connect_picker(&states),
            Outcome::Models(models) => {
                self.models = models;
                self.open_model_picker();
            }
            Outcome::ModelSet(model) => {
                self.model = sanitize::clean_line(&model);
                self.note(Level::Muted, format!("model pinned to {}", self.model));
            }
            Outcome::ModeSet(mode, summary) => {
                self.approval_mode = sanitize::clean_line(&mode);
                self.note(Level::Info, summary);
            }
            Outcome::SetupLink { label, url } => {
                self.setups.retain(|c| c.capability != "link");
                self.setups.push(SetupCard {
                    capability: "link".into(),
                    label: sanitize::clean_line(&label),
                    status: "waiting".into(),
                    kind: Some("url".into()),
                    url: Some(sanitize::clean_line(&url)),
                    steps: Vec::new(),
                    reason: None,
                });
                self.mode = Mode::Setup;
            }
            Outcome::Decided { id, approved } => {
                let title = self
                    .pending
                    .iter()
                    .find(|p| p.approval_id == id)
                    .map(|p| crate::ui::tool::action_title(&p.tool, &p.args));
                if self.pending.iter().any(|p| p.approval_id == id) {
                    self.pending.retain(|p| p.approval_id != id);
                    self.decision_note(approved, title.as_deref().unwrap_or(&id));
                    self.refresh_mode();
                }
            }
            Outcome::Answered(id) => {
                self.pending.retain(|p| p.approval_id != id);
                self.refresh_mode();
            }
            Outcome::Sent { id, duplicate } => {
                if duplicate {
                    self.note(Level::Muted, "duplicate message ignored");
                }
                let _ = id;
            }
            Outcome::SendFailed { id, error } => {
                for entry in self.entries.iter_mut() {
                    if let Entry::User(u) = entry {
                        if u.id.as_deref() == Some(&id) {
                            u.state = UserState::Failed;
                            u.cache = None;
                        }
                    }
                }
                self.note(Level::Error, format!("not sent: {error}"));
            }
            Outcome::Stop(requested) => {
                if !requested {
                    self.note(Level::Muted, "no active run to stop");
                }
            }
        }
    }

    fn decision_note(&mut self, approved: bool, title: &str) {
        let title = sanitize::clean_line(title);
        if approved {
            self.note(Level::Success, format!("✓ approved · {title}"));
        } else {
            self.note(Level::Error, format!("✗ denied · {title}"));
        }
    }

    pub fn on_event(&mut self, ev: &Event) {
        match ev.kind.as_str() {
            "user.message" => self.ev_user_message(ev),
            "message.queued" => self.ev_queued(ev),
            "message.edited" => {
                let id = ev.str("message_id").to_string();
                let text = sanitize::clean(ev.str("text"));
                for (qid, qtext) in self.queued.iter_mut() {
                    if *qid == id {
                        *qtext = text.clone();
                    }
                }
                for entry in self.entries.iter_mut() {
                    if let Entry::User(u) = entry {
                        if u.id.as_deref() == Some(&id) {
                            u.text = text.clone();
                            u.cache = None;
                        }
                    }
                }
            }
            "message.cancelled" => {
                let id = ev.str("message_id").to_string();
                self.queued.retain(|(qid, _)| *qid != id);
                self.entries
                    .retain(|e| !matches!(e, Entry::User(u) if u.id.as_deref() == Some(&id)));
                self.note(Level::Muted, "message cancelled");
            }
            "queue.paused" => {
                self.queue_paused = true;
                self.pause_reason = ev.opt_str("reason");
            }
            "queue.resumed" => {
                self.queue_paused = false;
                self.pause_reason = None;
                if ev.str("reason") == "new_message" {
                    self.note(Level::Muted, "resumed: picking up your new message");
                }
            }
            "message.dispatched" => {
                let id = ev.str("message_id").to_string();
                self.dequeue(&id);
            }
            "run.started" => self.ev_run_started(ev),
            "run.recovered" => {
                self.status = Status::Thinking;
                self.note(Level::Info, "recovering interrupted work");
            }
            "text.reset" => self.ev_text_reset(ev),
            "text.delta" => self.ev_text_delta(ev),
            "tool.call" => self.ev_tool_call(ev),
            "tool.result" => self.ev_tool_result(ev),
            "approval.requested" | "task.approval" => self.ev_approval(ev),
            "approval.granted" | "approval.denied" => self.ev_decided(ev),
            "question.answered" => {
                let id = ev.str("approval_id").to_string();
                self.pending.retain(|p| p.approval_id != id);
                let answer = sanitize::clean_line(ev.str("text"));
                self.note(
                    Level::Success,
                    format!("✓ answered · {}", crate::ui::text::elide(&answer, 60)),
                );
                self.status = Status::Thinking;
                self.refresh_mode();
            }
            "capability.setup" | "capability.status" => self.ev_capability(ev),
            "run.stop_requested" => {
                self.status = Status::Paused;
                self.note(
                    Level::Warn,
                    "stop requested · queue paused · authorized actions may still finish",
                );
            }
            "run.cancelled" => self.ev_run_cancelled(ev),
            "run.failed" => self.ev_run_failed(ev),
            "run.completed" | "run.waiting" => self.ev_run_end(ev),
            "task.created" => {
                let text = sanitize::clean_line(ev.str("text"));
                let text = if text.is_empty() {
                    sanitize::clean_line(ev.str("title"))
                } else {
                    text
                };
                self.note(Level::Muted, format!("task · {text}"));
            }
            "task.completed" => {
                self.note(
                    Level::Success,
                    format!("✓ task · {}", sanitize::clean_line(ev.str("text"))),
                );
            }
            "task.failed" => {
                self.note(
                    Level::Error,
                    format!("✗ task failed · {}", sanitize::clean_line(ev.str("text"))),
                );
            }
            "task.paused" => {
                self.note(
                    Level::Warn,
                    format!("⏸ task paused · {}", sanitize::clean_line(ev.str("text"))),
                );
            }
            "run.loop_detected" => {
                self.note(
                    Level::Warn,
                    format!(
                        "⏸ paused: going in circles ({}) · {}",
                        ev.str("kind"),
                        sanitize::clean_line(ev.str("detail"))
                    ),
                );
            }
            "task.cancelled" => self.note(Level::Muted, "task cancelled"),
            "context.compacted" => self.note(Level::Muted, "conversation compacted"),
            "memory.learned" => self.ev_memory_learned(ev),
            _ => {}
        }
    }

    /// A queued message leaves the queue when it dispatches, is answered, or is cancelled.
    fn dequeue(&mut self, id: &str) {
        self.queued.retain(|(qid, _)| qid != id);
        for entry in self.entries.iter_mut() {
            if let Entry::User(u) = entry {
                if u.id.as_deref() == Some(id)
                    && u.state != UserState::Sent
                    && u.state != UserState::Failed
                {
                    u.state = UserState::Sent;
                    u.cache = None;
                }
            }
        }
    }

    /// What bud learned, as one quiet line per lesson under the reply.
    fn ev_memory_learned(&mut self, ev: &Event) {
        let Some(lessons) = ev.data.get("lessons").and_then(Value::as_array) else {
            return;
        };
        let facts: Vec<(bool, String)> = lessons
            .iter()
            .filter_map(|l| {
                let fact = sanitize::clean_line(l.get("fact").and_then(Value::as_str)?);
                let replaced = l.get("replaced").is_some_and(|r| !r.is_null());
                (!fact.is_empty()).then_some((replaced, fact))
            })
            .collect();
        let n = facts.len();
        for (i, (replaced, fact)) in facts.into_iter().enumerate() {
            let verb = if replaced { "updated" } else { "noted" };
            let tail = if i + 1 == n {
                "  (say 'forget that' to undo)"
            } else {
                ""
            };
            self.note(Level::Muted, format!("{verb} · {fact}{tail}"));
        }
    }

    fn ev_user_message(&mut self, ev: &Event) {
        let id = ev.opt_str("message_id");
        let text = sanitize::clean(ev.str("text"));
        if let Some(id) = &id {
            self.queued.retain(|(qid, _)| qid != id);
            let at = self
                .entries
                .iter()
                .position(|e| matches!(e, Entry::User(u) if u.id.as_ref() == Some(id)));
            if let Some(at) = at {
                // The message sits directly above the reply it starts, not where it was typed.
                if let Entry::User(mut u) = self.entries.remove(at) {
                    u.state = UserState::Sent;
                    u.cache = None;
                    self.entries.push(Entry::User(u));
                }
                return;
            }
        }
        self.entries.push(Entry::User(UserEntry {
            id,
            text,
            state: UserState::Sent,
            cache: None,
        }));
        self.after_push();
    }

    fn ev_queued(&mut self, ev: &Event) {
        let id = ev.str("message_id").to_string();
        let text = sanitize::clean(ev.str("text"));
        if !self.queued.iter().any(|(q, _)| *q == id) {
            self.queued.push((id.clone(), text.clone()));
        }
        let busy = self.running() || self.status == Status::Waiting || self.queue_paused;
        let mut found = false;
        for entry in self.entries.iter_mut() {
            if let Entry::User(u) = entry {
                if u.id.as_ref() == Some(&id) {
                    u.state = if busy {
                        UserState::Queued
                    } else {
                        UserState::Pending
                    };
                    u.cache = None;
                    found = true;
                }
            }
        }
        if !found {
            self.entries.push(Entry::User(UserEntry {
                id: Some(id),
                text,
                state: if busy {
                    UserState::Queued
                } else {
                    UserState::Pending
                },
                cache: None,
            }));
            self.after_push();
        }
    }

    fn ev_run_started(&mut self, ev: &Event) {
        self.active_run = ev.opt_str("run_id");
        self.status = Status::Thinking;
        self.run_start = Some(self.now_ms());
        self.first_delta = None;
        self.stream_chars = 0;
        self.finish_streaming();
    }

    fn ev_text_reset(&mut self, ev: &Event) {
        let request = ev.str("request").to_string();
        if !self.seen_requests.insert(request.clone()) {
            if let Some(i) = self.assistant_idx(&request) {
                if let Entry::Assistant(a) = &mut self.entries[i] {
                    a.blocks.clear();
                    a.doc = MdDoc::new();
                    a.filter = Filter::default();
                }
            }
        }
    }

    fn ev_text_delta(&mut self, ev: &Event) {
        let block = ev.str("block").to_string();
        let request = block
            .rsplit_once(':')
            .map(|(r, _)| r.to_string())
            .unwrap_or_else(|| format!("legacy:{}", ev.str("run_id")));
        let run_id = ev.str("run_id").to_string();
        let idx = match self.assistant_idx(&request) {
            Some(i) => i,
            None => {
                self.entries.push(Entry::Assistant(Box::new(AssistantEntry {
                    request: request.clone(),
                    run_id,
                    blocks: Vec::new(),
                    doc: MdDoc::new(),
                    streaming: true,
                    filter: Filter::default(),
                })));
                self.after_push();
                self.entries.len() - 1
            }
        };
        if self.first_delta.is_none() {
            self.first_delta = Some(Instant::now());
        }
        if let Entry::Assistant(a) = &mut self.entries[idx] {
            let clean = a.filter.push(ev.str("text"));
            self.stream_chars += clean.chars().count();
            self.session_chars += clean.chars().count();
            a.streaming = true;
            match a.blocks.last_mut() {
                Some((key, text)) if *key == block => {
                    text.push_str(&clean);
                    a.doc.push(&clean);
                }
                _ => {
                    if let Some(slot) = a.blocks.iter_mut().find(|(k, _)| *k == block) {
                        slot.1.push_str(&clean);
                    } else {
                        a.blocks.push((block, clean));
                    }
                    let joined: String = a.blocks.iter().map(|(_, t)| t.as_str()).collect();
                    a.doc.set(&joined);
                }
            }
        }
        self.status = Status::Streaming;
    }

    fn ev_tool_call(&mut self, ev: &Event) {
        let tool = sanitize::clean_line(ev.str("tool"));
        let args = ev
            .data
            .get("args")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let call_id = ev
            .opt_str("call_id")
            .unwrap_or_else(|| format!("{tool}-{}", self.entries.len()));
        self.status = Status::Tool(crate::ui::text::elide(
            &sanitize::clean_line(&crate::ui::tool::humanize_action(&tool, &args, true)),
            60,
        ));
        self.entries.push(Entry::Tool(ToolEntry {
            call_id,
            tool,
            args,
            status: ToolStatus::Running,
            result: None,
            interrupted: false,
            expanded: self.tools_open,
        }));
        self.after_push();
    }

    fn ev_tool_result(&mut self, ev: &Event) {
        let call_id = ev.str("call_id").to_string();
        let tool = ev.str("tool").to_string();
        let result = sanitize::clean(&match ev.data.get("result") {
            Some(Value::String(s)) => s.clone(),
            Some(other) => other.to_string(),
            None => String::new(),
        });
        let failed = matches!(ev.str("outcome"), "failure" | "retried");
        let target = self.entries.iter_mut().rev().find_map(|e| match e {
            Entry::Tool(t)
                if (!call_id.is_empty() && t.call_id == call_id)
                    || (call_id.is_empty()
                        && t.tool == tool
                        && t.status == ToolStatus::Running) =>
            {
                Some(t)
            }
            _ => None,
        });
        if let Some(t) = target {
            t.status = if failed {
                ToolStatus::Failed
            } else {
                ToolStatus::Done
            };
            t.result = Some(result);
        }
        self.status = Status::Thinking;
    }

    fn ev_approval(&mut self, ev: &Event) {
        let args = ev
            .data
            .get("args")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        self.add_approval(PendingApproval {
            approval_id: ev.str("approval_id").to_string(),
            tool: sanitize::clean_line(ev.str("tool")),
            args,
            task_id: ev.data.get("task_id").and_then(Value::as_u64),
            reference: ev.opt_str("reference"),
        });
        self.status = Status::Waiting;
        if self.mode != Mode::Picker && !matches!(self.mode, Mode::Revising(_) | Mode::Answering(_))
        {
            self.mode = Mode::Prompt;
            self.refresh_mode();
        }
    }

    fn ev_decided(&mut self, ev: &Event) {
        let id = ev.str("approval_id").to_string();
        let approved = ev.kind == "approval.granted";
        let title = self
            .pending
            .iter()
            .find(|p| p.approval_id == id)
            .map(|p| crate::ui::tool::action_title(&p.tool, &p.args));
        let existed = self.pending.iter().any(|p| p.approval_id == id);
        self.pending.retain(|p| p.approval_id != id);
        self.card_scroll = 0;
        if existed {
            self.decision_note(approved, title.as_deref().unwrap_or(&id));
        }
        self.status = Status::Thinking;
        self.refresh_mode();
    }

    fn ev_capability(&mut self, ev: &Event) {
        let capability = sanitize::clean_line(ev.str("capability"));
        if capability.is_empty() {
            return;
        }
        if ev.kind == "capability.status" {
            let Some(card) = self.setups.iter_mut().find(|c| c.capability == capability) else {
                return;
            };
            if let Some(status) = ev.opt_str("status") {
                card.status = status;
            }
            if let Some(reason) = ev.opt_str("reason") {
                card.reason = Some(sanitize::clean_line(&reason)).filter(|r| !r.is_empty());
            }
        } else {
            let setup: PendingSetup = serde_json::from_value(ev.data.clone()).unwrap_or_default();
            self.upsert_setup(&setup);
        }
        let Some(card) = self
            .setups
            .iter()
            .find(|c| c.capability == capability)
            .cloned()
        else {
            return;
        };
        match card.status.as_str() {
            "connected" => {
                self.setups.retain(|c| c.capability != capability);
                self.note(Level::Success, format!("✓ {} connected", card.label));
                if self.mode == Mode::Setup {
                    self.mode = Mode::Prompt;
                    self.refresh_mode();
                }
            }
            "failed" => {
                let reason = card
                    .reason
                    .clone()
                    .unwrap_or_else(|| "unknown reason".into());
                self.note(Level::Error, format!("✗ {} · {reason}", card.label));
                if self.mode == Mode::Setup {
                    self.mode = Mode::Prompt;
                }
            }
            _ => {
                if self.mode == Mode::Prompt && self.pending.is_empty() {
                    self.mode = Mode::Setup;
                }
            }
        }
    }

    fn settle_tools(&mut self, uncertain: &HashSet<String>, fail_running: bool) {
        for entry in self.entries.iter_mut() {
            if let Entry::Tool(t) = entry {
                let hit = uncertain.contains(&t.tool.to_lowercase())
                    || uncertain.contains(&t.call_id.to_lowercase());
                if t.status == ToolStatus::Running {
                    if fail_running {
                        t.status = ToolStatus::Failed;
                    } else {
                        t.interrupted = true;
                        t.status = ToolStatus::Done;
                    }
                } else if hit {
                    t.interrupted = true;
                }
            }
        }
    }

    fn ev_run_cancelled(&mut self, ev: &Event) {
        let uncertain: Vec<String> = ev
            .data
            .get("uncertain_tools")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default();
        let set: HashSet<String> = uncertain.iter().map(|u| u.to_lowercase()).collect();
        self.settle_tools(&set, false);
        self.finish_streaming();
        self.status = Status::Idle;
        self.active_run = None;
        let elapsed = self.take_elapsed();
        if uncertain.is_empty() {
            self.note(Level::Muted, "run cancelled");
        } else {
            let names: Vec<String> = uncertain.iter().map(|u| sanitize::clean_line(u)).collect();
            self.note(
                Level::Warn,
                format!("stopped · verify the outcome of: {}", names.join(", ")),
            );
        }
        if let Some(secs) = elapsed {
            let model = self.model.clone();
            self.note(Level::Muted, format!("{model} · {secs:.1}s · interrupted"));
        }
        self.refresh_mode();
    }

    fn ev_run_failed(&mut self, ev: &Event) {
        let reason = sanitize::clean_line(ev.str("reason"));
        self.settle_tools(&HashSet::new(), true);
        self.finish_streaming();
        self.status = Status::Error;
        self.active_run = None;
        self.run_start = None;
        let reason = if reason.is_empty() {
            "unknown error".to_string()
        } else {
            reason
        };
        let next = sanitize::clean_line(ev.str("next"));
        if next.is_empty() {
            self.note(Level::Error, format!("✗ run failed · {reason}"));
        } else {
            self.note(Level::Error, format!("✗ run failed · {reason}\n{next}"));
        }
    }

    fn ev_run_end(&mut self, ev: &Event) {
        let run_id = ev.str("run_id").to_string();
        self.settle_tools(&HashSet::new(), false);
        let full = sanitize::clean(&reply_text(&ev.data));
        self.reconcile_reply(&run_id, &full);
        self.finish_streaming();
        let elapsed = self.take_elapsed();
        let chars = self.stream_chars;
        self.active_run = None;
        self.status = if self.pending.is_empty() {
            Status::Idle
        } else {
            Status::Waiting
        };
        if ev.kind == "run.completed" {
            self.run_start = None;
            if let Some(secs) = elapsed {
                let model = self.model.clone();
                let mut line = format!("{model} · {secs:.1}s");
                if chars > 0 {
                    line.push_str(&format!(" · ~{} tokens", chars / 4));
                }
                self.note(Level::Muted, line);
            }
        } else {
            self.run_start = None;
        }
        self.refresh_mode();
    }

    /// The stored reply is the truth: fill in or replace text the live stream lost.
    fn reconcile_reply(&mut self, run_id: &str, full: &str) {
        let streamed: String = self
            .entries
            .iter()
            .filter_map(|e| match e {
                Entry::Assistant(a) if a.run_id == run_id => {
                    Some(a.blocks.iter().map(|(_, t)| t.as_str()).collect::<String>())
                }
                _ => None,
            })
            .collect();
        if full.is_empty() || streamed == full {
            return;
        }
        let last = self
            .entries
            .iter()
            .rposition(|e| matches!(e, Entry::Assistant(a) if a.run_id == run_id));
        if full.starts_with(&streamed) && last.is_some() {
            let tail = &full[streamed.len()..];
            if let Some(Entry::Assistant(a)) = last.map(|i| &mut self.entries[i]) {
                a.blocks.push(("reconciled".into(), tail.to_string()));
                a.doc.push(tail);
            }
            return;
        }
        let first = self
            .entries
            .iter()
            .position(|e| matches!(e, Entry::Assistant(a) if a.run_id == run_id));
        self.entries
            .retain(|e| !matches!(e, Entry::Assistant(a) if a.run_id == run_id));
        let mut doc = MdDoc::new();
        doc.set(full);
        let entry = Entry::Assistant(Box::new(AssistantEntry {
            request: format!("{run_id}:full"),
            run_id: run_id.to_string(),
            blocks: vec![("full".into(), full.to_string())],
            doc,
            streaming: false,
            filter: Filter::default(),
        }));
        let at = first.unwrap_or(self.entries.len()).min(self.entries.len());
        self.entries.insert(at, entry);
    }

    pub fn toggle_tools(&mut self) {
        self.tools_open = !self.tools_open;
        for entry in self.entries.iter_mut() {
            if let Entry::Tool(t) = entry {
                t.expanded = self.tools_open;
            }
        }
    }

    pub fn sent_error(error: &CoreError) -> String {
        error.to_string()
    }
}
