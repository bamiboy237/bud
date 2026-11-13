//! Key handling: one mode at a time, Esc backs out one level.

use crate::app::{App, Level, Mode, Outcome, UserEntry, UserState};
use crate::commands;
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};

pub const FREE_TEXT: &str = "Type your own answer";
/// Rows that display their digit; the digit acts if and only if the row shows it.
pub const DIGIT_ROWS: usize = 5;

fn plain(key: &KeyEvent) -> bool {
    !key.modifiers
        .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT | KeyModifiers::SUPER)
}

fn ctrl(key: &KeyEvent, c: char) -> bool {
    key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char(c)
}

impl App {
    pub fn on_key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        // Keys that work everywhere.
        if ctrl(&key, 'o') {
            self.toggle_tools();
            return;
        }
        let card_scrolls = self.mode == Mode::Approval && self.card_max.get() > 0;
        match key.code {
            KeyCode::PageUp if card_scrolls => {
                self.card_scroll = self.card_scroll.saturating_sub(self.page() as usize);
                return;
            }
            KeyCode::PageDown if card_scrolls => {
                self.card_scroll =
                    (self.card_scroll + self.page() as usize).min(self.card_max.get());
                return;
            }
            KeyCode::PageUp => {
                self.scroll_by(self.page());
                return;
            }
            KeyCode::PageDown => {
                self.scroll_by(-self.page());
                return;
            }
            _ => {}
        }
        match self.mode.clone() {
            Mode::Approval => self.key_approval(key),
            Mode::Question => self.key_question(key),
            Mode::Setup => self.key_setup(key),
            Mode::Picker => self.key_picker(key),
            Mode::Prompt | Mode::Revising(_) | Mode::Answering(_) | Mode::Editing(_) => {
                self.key_prompt(key)
            }
        }
    }

    fn page(&self) -> isize {
        (self.view_h as isize - 2).max(1)
    }

    pub fn scroll_by(&mut self, rows: isize) {
        let max = self.last_total.saturating_sub(self.view_h);
        let next = (self.scroll as isize + rows).clamp(0, max as isize);
        self.scroll = next as usize;
    }

    // Approval ---------------------------------------------------------------

    fn key_approval(&mut self, key: KeyEvent) {
        if ctrl(&key, 'c') || ctrl(&key, 'd') {
            return self.interrupt_or_quit(&key);
        }
        match key.code {
            KeyCode::Char('y') | KeyCode::Char('Y') if plain(&key) => self.decide(true),
            KeyCode::Char('n') | KeyCode::Char('N') if plain(&key) => self.decide(false),
            KeyCode::Char('r') | KeyCode::Char('R') if plain(&key) => {
                if let Some(item) = self.current_action() {
                    let id = item.approval_id.clone();
                    self.mode = Mode::Revising(id);
                }
            }
            KeyCode::Up | KeyCode::Char('k') if plain(&key) => {
                self.card_scroll = self.card_scroll.saturating_sub(1);
            }
            KeyCode::Down | KeyCode::Char('j') if plain(&key) => {
                self.card_scroll = (self.card_scroll + 1).min(self.card_max.get());
            }
            KeyCode::Esc => self.mode = Mode::Prompt,
            _ => {}
        }
    }

    pub fn decide(&mut self, approved: bool) {
        let Some(item) = self.current_action() else {
            return;
        };
        let id = item.approval_id.clone();
        self.follow_bottom();
        self.act(move |core, session| async move {
            match core.decide(&session, &id, approved).await {
                Ok(_) => vec![Outcome::Decided { id, approved }],
                Err(e) => vec![Outcome::Note(Level::Error, format!("decision failed: {e}"))],
            }
        });
    }

    // Question ---------------------------------------------------------------

    fn question_rows(&self) -> usize {
        self.questions()
            .get(self.q_tab)
            .map(|q| {
                q.args
                    .get("choices")
                    .and_then(|c| c.as_array())
                    .map_or(0, Vec::len)
                    + 1
            })
            .unwrap_or(0)
    }

    fn key_question(&mut self, key: KeyEvent) {
        if ctrl(&key, 'c') || ctrl(&key, 'd') {
            return self.interrupt_or_quit(&key);
        }
        let rows = self.question_rows();
        if rows == 0 {
            self.mode = Mode::Prompt;
            return;
        }
        match key.code {
            KeyCode::Up | KeyCode::Char('k') if plain(&key) => {
                self.q_sel = (self.q_sel + rows - 1) % rows;
            }
            KeyCode::Down | KeyCode::Char('j') if plain(&key) => {
                self.q_sel = (self.q_sel + 1) % rows
            }
            KeyCode::Left if plain(&key) => {
                let n = self.questions().len();
                if n > 1 {
                    self.q_tab = (self.q_tab + n - 1) % n;
                    self.q_sel = 0;
                }
            }
            KeyCode::Right | KeyCode::Tab if plain(&key) => {
                let n = self.questions().len();
                if n > 1 {
                    self.q_tab = (self.q_tab + 1) % n;
                    self.q_sel = 0;
                }
            }
            KeyCode::Enter => self.confirm_question(),
            KeyCode::Char(c @ '1'..='5') if plain(&key) => {
                let index = c as usize - '1' as usize;
                if index < rows {
                    self.q_sel = index;
                    self.confirm_question();
                }
            }
            KeyCode::Esc => self.mode = Mode::Prompt,
            _ => {}
        }
    }

    fn confirm_question(&mut self) {
        let Some(q) = self.questions().get(self.q_tab).map(|q| (*q).clone()) else {
            return;
        };
        let choices: Vec<String> = q
            .args
            .get("choices")
            .and_then(|c| c.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default();
        if self.q_sel >= choices.len() {
            self.mode = Mode::Answering(q.approval_id);
            return;
        }
        let answer = choices[self.q_sel].clone();
        self.submit_answer(q.approval_id, answer);
    }

    pub fn submit_answer(&mut self, id: String, text: String) {
        self.follow_bottom();
        self.q_sel = 0;
        self.act(move |core, session| async move {
            match core.answer(&session, &id, &text).await {
                Ok(true) => vec![Outcome::Answered(id)],
                Ok(false) => vec![Outcome::Note(
                    Level::Muted,
                    "question already answered".into(),
                )],
                Err(e) => vec![Outcome::Note(Level::Error, format!("answer failed: {e}"))],
            }
        });
        if self.mode == Mode::Question || matches!(self.mode, Mode::Answering(_)) {
            self.mode = Mode::Prompt;
        }
    }

    // Setup ------------------------------------------------------------------

    fn key_setup(&mut self, key: KeyEvent) {
        if ctrl(&key, 'c') || ctrl(&key, 'd') {
            return self.interrupt_or_quit(&key);
        }
        match key.code {
            KeyCode::Enter => {
                let url = self.current_setup().and_then(|c| c.url.clone());
                if let Some(url) = url {
                    self.clipboard_out.push(url);
                    self.flash("link copied");
                }
            }
            KeyCode::Esc => self.mode = Mode::Prompt,
            _ => {}
        }
    }

    pub fn switch_model(&mut self, model: String) {
        self.act(move |core, session| async move {
            match core.set_model(&session, &model).await {
                Ok(()) => vec![Outcome::ModelSet(model)],
                Err(e) => vec![Outcome::Note(Level::Error, format!("model rejected: {e}"))],
            }
        });
    }

    // Prompt -----------------------------------------------------------------

    pub fn menu_entries(&self) -> Vec<&'static commands::Command> {
        if self.mode != Mode::Prompt || !self.menu_open {
            return Vec::new();
        }
        let t = self.composer.text.as_str();
        if !t.starts_with('/') || t.contains(char::is_whitespace) {
            return Vec::new();
        }
        let list = commands::matching(t);
        if list.len() == 1 && list[0].name == t {
            return Vec::new();
        }
        list
    }

    pub fn interrupt_or_quit(&mut self, key: &KeyEvent) {
        if ctrl(key, 'd') {
            if self.composer.is_empty() {
                self.quit = true;
            }
            return;
        }
        if !self.composer.is_empty()
            && matches!(
                self.mode,
                Mode::Prompt | Mode::Revising(_) | Mode::Answering(_) | Mode::Editing(_)
            )
        {
            self.composer.clear();
        } else if self.running() || self.status == crate::app::Status::Waiting {
            self.stop_run();
        }
    }

    pub fn stop_run(&mut self) {
        self.act(|core, session| async move {
            match core.stop(&session).await {
                Ok(requested) => vec![Outcome::Stop(requested)],
                Err(e) => vec![Outcome::Note(Level::Error, format!("stop failed: {e}"))],
            }
        });
    }

    fn key_prompt(&mut self, key: KeyEvent) {
        let menu = self.menu_entries();
        let w = self.composer_width;
        if key.modifiers.contains(KeyModifiers::CONTROL) {
            match key.code {
                KeyCode::Char('c') | KeyCode::Char('d') => return self.interrupt_or_quit(&key),
                KeyCode::Char('l') => {
                    self.redraw = true;
                    return;
                }
                KeyCode::Char('a') => return self.composer.home(),
                KeyCode::Char('e') => return self.composer.end(),
                KeyCode::Char('b') => return self.composer.left(),
                KeyCode::Char('f') => return self.composer.right(),
                KeyCode::Char('w') => return self.composer.delete_word_back(),
                KeyCode::Char('u') => return self.composer.kill_to_line_start(),
                KeyCode::Char('k') => return self.composer.kill_to_line_end(),
                KeyCode::Char('j') => return self.composer.insert("\n"),
                KeyCode::Left => return self.composer.word_left(),
                KeyCode::Right => return self.composer.word_right(),
                _ => return,
            }
        }
        if key.modifiers.contains(KeyModifiers::ALT) {
            match key.code {
                KeyCode::Enter => return self.composer.insert("\n"),
                KeyCode::Char('b') | KeyCode::Left => return self.composer.word_left(),
                KeyCode::Char('f') | KeyCode::Right => return self.composer.word_right(),
                KeyCode::Backspace => return self.composer.delete_word_back(),
                KeyCode::Up => {
                    self.composer.history_prev();
                    return;
                }
                KeyCode::Down => {
                    self.composer.history_next();
                    return;
                }
                _ => {}
            }
        }
        match key.code {
            KeyCode::Enter => {
                if key.modifiers.contains(KeyModifiers::SHIFT) {
                    return self.composer.insert("\n");
                }
                if !menu.is_empty() {
                    return self.run_menu_choice(menu[self.menu_sel.min(menu.len() - 1)]);
                }
                if self.composer.text.ends_with('\\') {
                    self.composer.backspace();
                    return self.composer.insert("\n");
                }
                if self.composer.is_empty() {
                    self.reclaim_card();
                    return;
                }
                self.submit();
            }
            KeyCode::Tab => {
                if !menu.is_empty() {
                    self.run_menu_choice(menu[self.menu_sel.min(menu.len() - 1)]);
                }
            }
            KeyCode::Esc => {
                if !menu.is_empty() {
                    self.menu_open = false;
                } else if matches!(self.mode, Mode::Revising(_)) {
                    self.mode = Mode::Approval;
                } else if matches!(self.mode, Mode::Answering(_)) {
                    self.mode = Mode::Question;
                } else if matches!(self.mode, Mode::Editing(_)) {
                    self.mode = Mode::Prompt;
                    let draft = std::mem::take(&mut self.cmd_draft);
                    self.composer.set(&draft);
                } else if self.running()
                    || self.status == crate::app::Status::Waiting && self.pending.is_empty()
                {
                    self.stop_run();
                } else if self.scroll > 0 {
                    self.scroll = 0;
                }
            }
            KeyCode::Up => {
                if !menu.is_empty() {
                    self.menu_sel = (self.menu_sel + menu.len() - 1) % menu.len();
                } else if !self.composer.move_vertical(false, w) {
                    self.composer.history_prev();
                }
            }
            KeyCode::Down => {
                if !menu.is_empty() {
                    self.menu_sel = (self.menu_sel + 1) % menu.len();
                } else if !self.composer.move_vertical(true, w) {
                    self.composer.history_next();
                }
            }
            KeyCode::Left => self.composer.left(),
            KeyCode::Right => self.composer.right(),
            KeyCode::Home => self.composer.home(),
            KeyCode::End => self.composer.end(),
            KeyCode::Backspace => {
                self.composer.backspace();
                self.menu_open = true;
                self.menu_sel = 0;
            }
            KeyCode::Delete => self.composer.delete(),
            KeyCode::Char(c) => {
                self.composer.insert(&c.to_string());
                self.menu_open = true;
                self.menu_sel = 0;
                self.follow_bottom();
            }
            _ => {}
        }
    }

    /// Choosing a row of the slash menu runs the command; it never just pastes its name.
    fn run_menu_choice(&mut self, chosen: &commands::Command) {
        self.cmd_draft = self.composer.take();
        self.menu_sel = 0;
        self.menu_open = true;
        self.run_command(chosen.name);
    }

    /// A dismissed card comes back when the person asks for it with an empty Enter.
    fn reclaim_card(&mut self) {
        if !self.pending.is_empty() || self.setups.iter().any(|c| c.status == "waiting") {
            if self.pending.is_empty() {
                self.mode = Mode::Setup;
            } else {
                self.refresh_mode();
            }
        }
    }

    pub fn on_paste(&mut self, text: &str) {
        if matches!(
            self.mode,
            Mode::Prompt | Mode::Revising(_) | Mode::Answering(_) | Mode::Editing(_)
        ) {
            let clean = crate::sanitize::clean(&text.replace("\r\n", "\n"));
            self.composer.insert(&clean);
            self.menu_open = true;
            self.menu_sel = 0;
        }
    }

    pub fn on_mouse(&mut self, m: MouseEvent) {
        match m.kind {
            MouseEventKind::ScrollUp => self.scroll_by(3),
            MouseEventKind::ScrollDown => self.scroll_by(-3),
            MouseEventKind::Down(MouseButton::Left) => {
                if let Some((_, idx)) = self.hits.iter().find(|(row, _)| *row == m.row) {
                    let idx = *idx;
                    self.toggle_entry(idx);
                }
            }
            _ => {}
        }
    }

    fn toggle_entry(&mut self, idx: usize) {
        let Some(crate::app::Entry::Tool(first)) = self.entries.get(idx) else {
            return;
        };
        let target = !first.expanded;
        let name = first.tool.clone();
        // A collapsed group opens whole: every neighbouring identical success follows.
        let mut lo = idx;
        while lo > 0
            && matches!(&self.entries[lo - 1], crate::app::Entry::Tool(t) if t.tool == name && t.status == crate::app::ToolStatus::Done)
        {
            lo -= 1;
        }
        let mut hi = idx;
        while hi + 1 < self.entries.len()
            && matches!(&self.entries[hi + 1], crate::app::Entry::Tool(t) if t.tool == name && t.status == crate::app::ToolStatus::Done)
        {
            hi += 1;
        }
        for entry in &mut self.entries[lo..=hi] {
            if let crate::app::Entry::Tool(t) = entry {
                t.expanded = target;
            }
        }
    }

    // Submit -----------------------------------------------------------------

    pub fn submit(&mut self) {
        let text = self.composer.take();
        let text = text.trim().to_string();
        if text.is_empty() {
            return;
        }
        self.composer.remember(&text);
        self.menu_sel = 0;
        self.menu_open = true;
        match self.mode.clone() {
            Mode::Revising(id) => {
                self.mode = Mode::Prompt;
                let message_id = uuid::Uuid::new_v4().simple().to_string();
                self.follow_bottom();
                self.act(move |core, session| async move {
                    match core.revise(&session, &id, &text, &message_id).await {
                        Ok(true) => vec![Outcome::Note(Level::Muted, "revision sent".into())],
                        Ok(false) => vec![Outcome::Note(Level::Muted, "already decided".into())],
                        Err(e) => {
                            vec![Outcome::Note(Level::Error, format!("revision failed: {e}"))]
                        }
                    }
                });
            }
            Mode::Answering(id) => {
                self.entries.push(crate::app::Entry::User(UserEntry {
                    id: None,
                    text: text.clone(),
                    state: UserState::Sent,
                    cache: None,
                }));
                self.submit_answer(id, text);
            }
            Mode::Editing(id) => {
                self.mode = Mode::Prompt;
                self.cmd_draft.clear();
                self.act(move |core, session| async move {
                    match core.edit_queued(&session, &id, &text).await {
                        Ok(()) => vec![Outcome::Note(Level::Muted, "queued message edited".into())],
                        Err(e) => vec![Outcome::Note(Level::Error, format!("edit failed: {e}"))],
                    }
                });
            }
            _ => {
                if text.starts_with('/') {
                    self.cmd_draft = text.clone();
                    self.run_command(&text);
                } else {
                    self.send_message(text);
                }
            }
        }
    }

    fn send_message(&mut self, text: String) {
        let id = uuid::Uuid::new_v4().simple().to_string();
        self.entries.push(crate::app::Entry::User(UserEntry {
            id: Some(id.clone()),
            text: text.clone(),
            state: UserState::Pending,
            cache: None,
        }));
        if self.status == crate::app::Status::Error {
            self.status = crate::app::Status::Idle;
        }
        self.follow_bottom();
        if !self.pending.is_empty() {
            self.refresh_mode();
        }
        if let Some(post) = &self.post_tx {
            let _ = post.send((id, text));
        }
    }
}
