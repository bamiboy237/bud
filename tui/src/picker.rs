//! Pickers: every command whose argument is a known set opens one of these when run bare.
//!
//! The current value is marked and the cursor starts on it. Arrows (or j/k) move, typing
//! filters, Enter applies, Esc goes back one level (field, then filter, then the draft).
//! Theme and mascot preview live as the cursor moves; Esc puts the old one back.

use crate::app::{App, Level, Mode, Outcome, PickItem, PickKind, Picker, Revert};
use crate::keys::DIGIT_ROWS;
use crate::mascot::Kind;
use crate::sanitize::clean_line;
use crate::theme::Theme;
use crate::ui::text::elide;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::Value;

const MODES: [(&str, &str); 2] = [
    ("strict", "every write asks: workspace, memory, skills"),
    (
        "auto",
        "a reviewer runs safe, requested writes; sends still ask",
    ),
];

fn item(label: &str, detail: &str, value: &str) -> PickItem {
    PickItem {
        label: label.to_string(),
        detail: detail.to_string(),
        value: value.to_string(),
    }
}

impl App {
    fn new_picker(
        &self,
        kind: PickKind,
        title: &str,
        items: Vec<PickItem>,
        current: Option<String>,
        empty: &str,
    ) -> Picker {
        let selected = current
            .as_ref()
            .and_then(|c| items.iter().position(|i| &i.value == c))
            .unwrap_or(0);
        Picker {
            kind,
            title: title.to_string(),
            items,
            selected,
            current,
            filter: String::new(),
            search: false,
            empty: empty.to_string(),
            draft: self.cmd_draft.clone(),
            revert: None,
        }
    }

    pub fn open_picker(&mut self, picker: Picker) {
        self.picker = Some(picker);
        self.mode = Mode::Picker;
    }

    pub fn open_theme_picker(&mut self) {
        let items = vec![
            item("dark", "zinc on near-black", "dark"),
            item("light", "zinc on near-white", "light"),
            item("auto", "follow the terminal background", "auto"),
        ];
        let mut p = self.new_picker(
            PickKind::Theme,
            "Select a theme",
            items,
            Some(self.theme_pref.clone()),
            "",
        );
        p.revert = Some(Revert::Theme(
            Box::new(self.theme.clone()),
            self.theme_pref.clone(),
        ));
        self.open_picker(p);
    }

    pub fn open_mascot_picker(&mut self) {
        let items = vec![
            item("dog", "a dog with a cigar", "dog"),
            item("ant", "an ant carrying a crumb", "ant"),
        ];
        let current = self.mascot.kind().name().to_string();
        let mut p = self.new_picker(
            PickKind::Mascot,
            "Select a mascot",
            items,
            Some(current),
            "",
        );
        p.revert = Some(Revert::Mascot(self.mascot.kind()));
        self.open_picker(p);
    }

    pub fn open_mode_picker(&mut self) {
        let items = MODES.iter().map(|(m, d)| item(m, d, m)).collect();
        let p = self.new_picker(
            PickKind::Mode,
            "Approval mode",
            items,
            Some(self.approval_mode.clone()),
            "",
        );
        self.open_picker(p);
    }

    pub fn open_connect_picker(&mut self, states: &Value) {
        let statuses = states.get("statuses");
        let state_of = |key: &str| -> (String, bool) {
            match statuses.and_then(|s| s.get(key)).and_then(Value::as_str) {
                Some("ACTIVE") => ("connected".into(), true),
                Some(other) => (clean_line(&other.to_lowercase().replace('_', " ")), false),
                None => ("not connected".into(), false),
            }
        };
        let mut items = Vec::new();
        let mut current = None;
        for (value, label, key) in [
            ("gmail", "Gmail", "gmail"),
            ("calendar", "Calendar", "calendar"),
        ] {
            let (detail, connected) = state_of(key);
            if connected && current.is_none() {
                current = Some(value.to_string());
            }
            items.push(item(label, &detail, value));
        }
        let mut p = self.new_picker(PickKind::Connect, "Connect an account", items, current, "");
        p.selected = 0;
        self.open_picker(p);
    }

    pub fn open_model_picker(&mut self) {
        let configured: Vec<_> = self.models.iter().filter(|m| m.configured).collect();
        let list = if configured.is_empty() {
            self.models.iter().collect()
        } else {
            configured
        };
        let items: Vec<PickItem> = list
            .iter()
            .map(|m| {
                item(
                    &clean_line(&m.id),
                    &format!("{} · {}", m.tier, m.billing),
                    &m.id,
                )
            })
            .collect();
        let p = self.new_picker(
            PickKind::Model,
            "Select a model",
            items,
            Some(self.model.clone()),
            "No models are configured on the server. Add a provider key or run bud login, then restart bud serve.",
        );
        self.open_picker(p);
    }

    pub fn open_queue_picker(&mut self, kind: PickKind) {
        let items: Vec<PickItem> = self
            .queued
            .iter()
            .map(|(id, text)| {
                item(
                    &elide(&clean_line(text), 60),
                    &id.chars().take(8).collect::<String>(),
                    id,
                )
            })
            .collect();
        let title = if kind == PickKind::Edit {
            "Edit which message?"
        } else {
            "Cancel which message?"
        };
        let p = self.new_picker(
            kind,
            title,
            items,
            None,
            "Nothing is queued. Messages you send while bud is busy wait here.",
        );
        self.open_picker(p);
    }

    pub fn open_decide_picker(&mut self, approve: bool) {
        let items: Vec<PickItem> = self
            .pending
            .iter()
            .filter(|p| p.tool != "ask_user")
            .map(|p| {
                item(
                    &clean_line(&crate::ui::tool::action_title(&p.tool, &p.args)),
                    &p.approval_id,
                    &p.approval_id,
                )
            })
            .collect();
        let (kind, title) = if approve {
            (PickKind::Approve, "Approve which action?")
        } else {
            (PickKind::Deny, "Deny which action?")
        };
        let p = self.new_picker(
            kind,
            title,
            items,
            None,
            "No action is waiting for a decision.",
        );
        self.open_picker(p);
    }

    /// Opens the composer on a queued message's text; Enter saves the edit.
    pub fn begin_edit(&mut self, id: &str) {
        let Some(text) = self
            .queued
            .iter()
            .find(|(q, _)| q == id)
            .map(|(_, t)| t.clone())
        else {
            return self.note(Level::Muted, "that message is no longer queued");
        };
        self.composer.set(&text);
        self.mode = Mode::Editing(id.to_string());
    }

    fn preview(&mut self) {
        let Some(p) = &self.picker else { return };
        let vis = p.visible();
        let Some(&idx) = vis.get(p.selected) else {
            return;
        };
        let (kind, value) = (p.kind, p.items[idx].value.clone());
        match kind {
            PickKind::Theme => {
                self.apply_theme_pref(&value);
            }
            PickKind::Mascot => {
                if let Some(k) = Kind::parse(&value) {
                    self.mascot.set_kind(k);
                    self.sync_mascot();
                }
            }
            _ => {}
        }
    }

    pub fn apply_theme_pref(&mut self, name: &str) -> bool {
        let theme = match name {
            "auto" => Some(Theme::auto(self.depth)),
            other => Theme::named(other, self.depth),
        };
        match theme {
            Some(t) => {
                self.set_theme(t);
                self.theme_pref = name.to_string();
                true
            }
            None => false,
        }
    }

    fn close_picker(&mut self, restore_draft: bool) {
        let Some(p) = self.picker.take() else { return };
        match p.revert {
            Some(Revert::Theme(theme, pref)) => {
                self.set_theme(*theme);
                self.theme_pref = pref;
            }
            Some(Revert::Mascot(kind)) => {
                self.mascot.set_kind(kind);
                self.sync_mascot();
            }
            None => {}
        }
        self.mode = Mode::Prompt;
        self.refresh_mode();
        if restore_draft && !p.draft.is_empty() {
            self.composer.set(&p.draft);
        }
    }

    pub fn key_picker(&mut self, key: KeyEvent) {
        if key.modifiers.contains(KeyModifiers::CONTROL) {
            if key.code == KeyCode::Char('c') {
                self.close_picker(false);
            }
            return;
        }
        let plain = !key.modifiers.contains(KeyModifiers::ALT);
        let Some(p) = self.picker.as_mut() else {
            self.mode = Mode::Prompt;
            return;
        };
        let n = p.visible().len();
        let step = |p: &mut Picker, down: bool| {
            if n > 0 {
                p.selected = if down {
                    (p.selected + 1) % n
                } else {
                    (p.selected + n - 1) % n
                };
            }
        };
        match key.code {
            KeyCode::Up => step(p, false),
            KeyCode::Down => step(p, true),
            KeyCode::Char('k') if plain && !p.search && p.filter.is_empty() => step(p, false),
            KeyCode::Char('j') if plain && !p.search && p.filter.is_empty() => step(p, true),
            KeyCode::Home => p.selected = 0,
            KeyCode::End => p.selected = n.saturating_sub(1),
            KeyCode::Enter => {
                if n > 0 {
                    return self.pick();
                }
            }
            KeyCode::Char('/') if plain && !p.search => p.search = true,
            KeyCode::Char(c @ '1'..='5') if plain && !p.search && p.filter.is_empty() => {
                let index = c as usize - '1' as usize;
                if index < n.min(DIGIT_ROWS) {
                    p.selected = index;
                    return self.pick();
                }
            }
            KeyCode::Char(c) if plain && !c.is_control() => {
                p.search = true;
                p.filter.push(c);
                p.selected = 0;
            }
            KeyCode::Backspace => {
                p.filter.pop();
                p.selected = 0;
            }
            KeyCode::Esc => {
                if p.search {
                    p.search = false;
                } else if !p.filter.is_empty() {
                    p.filter.clear();
                    p.selected = 0;
                } else {
                    return self.close_picker(true);
                }
            }
            _ => {}
        }
        self.preview();
    }

    fn pick(&mut self) {
        let Some(p) = self.picker.as_mut() else {
            return;
        };
        let vis = p.visible();
        let Some(&idx) = vis.get(p.selected) else {
            return;
        };
        let (kind, value) = (p.kind, p.items[idx].value.clone());
        let draft = p.draft.clone();
        // The chosen value is already live for theme and mascot; applying it keeps it.
        if let Some(p) = self.picker.as_mut() {
            p.revert = None;
        }
        self.picker = None;
        self.mode = Mode::Prompt;
        self.refresh_mode();
        self.cmd_draft = draft;
        match kind {
            PickKind::Model => self.switch_model(value),
            PickKind::Theme => self.run_command(&format!("/theme {value}")),
            PickKind::Mascot => self.run_command(&format!("/mascot {value}")),
            PickKind::Mode => self.run_command(&format!("/mode {value}")),
            PickKind::Connect => self.run_command(&format!("/connect {value}")),
            PickKind::Cancel => self.run_command(&format!("/cancel {value}")),
            PickKind::Edit => self.begin_edit(&value),
            PickKind::Approve => self.decide_id(value, true),
            PickKind::Deny => self.decide_id(value, false),
        }
    }

    pub fn decide_id(&mut self, id: String, approved: bool) {
        self.act(move |core, session| async move {
            match core.decide(&session, &id, approved).await {
                Ok(_) => vec![Outcome::Decided { id, approved }],
                Err(e) => vec![Outcome::Note(Level::Error, format!("decision failed: {e}"))],
            }
        });
    }
}
