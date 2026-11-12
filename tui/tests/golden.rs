//! Golden text frames: key screens rendered through ratatui's TestBackend at three sizes.
//! Regenerate with `UPDATE_GOLDENS=1 cargo test --test golden`; review the diff, never hand-edit.

use bud_tui::app::App;
use bud_tui::mascot::{Kind, State};
use bud_tui::protocol::Event;
use bud_tui::theme::{ColorDepth, Theme};
use bud_tui::ui;
use ratatui::backend::TestBackend;
use ratatui::Terminal;
use serde_json::json;
use std::path::PathBuf;

const SIZES: [(u16, u16); 3] = [(100, 32), (80, 24), (40, 20)];

fn ev(kind: &str, data: serde_json::Value) -> Event {
    Event {
        seq: 1,
        kind: kind.into(),
        data,
        live: false,
    }
}

fn live(kind: &str, data: serde_json::Value) -> Event {
    Event {
        seq: 1,
        kind: kind.into(),
        data,
        live: true,
    }
}

fn app(kind: Kind, theme: Theme) -> App {
    let mut app = App::new(theme, kind, "stub-fast");
    app.context_label = "bud:main".into();
    app.clock = Some(1_000);
    app
}

fn render(app: &mut App, size: (u16, u16)) -> String {
    let mut terminal = Terminal::new(TestBackend::new(size.0, size.1)).expect("backend");
    terminal.draw(|f| ui::draw(f, app)).expect("draw");
    let buffer = terminal.backend().buffer().clone();
    let mut rows = Vec::new();
    for y in 0..size.1 {
        let mut row = String::new();
        for x in 0..size.0 {
            row.push_str(buffer[(x, y)].symbol());
        }
        rows.push(row.trim_end().to_string());
    }
    rows.join("\n") + "\n"
}

fn check(name: &str, size: (u16, u16), frame: &str) {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/goldens");
    std::fs::create_dir_all(&dir).expect("goldens dir");
    let path = dir.join(format!("{name}-{}x{}.txt", size.0, size.1));
    if std::env::var_os("UPDATE_GOLDENS").is_some() || !path.exists() {
        std::fs::write(&path, frame).expect("write golden");
        return;
    }
    let want = std::fs::read_to_string(&path).expect("golden");
    assert_eq!(
        frame, want,
        "golden {name} {}x{} changed; review and regenerate",
        size.0, size.1
    );
}

fn run_started(app: &mut App) {
    app.on_event(&ev("run.started", json!({"run_id": "r1"})));
}

fn all_sizes(name: &str, build: impl Fn() -> App) {
    for size in SIZES {
        let mut app = build();
        let frame = render(&mut app, size);
        for line in frame.lines() {
            assert!(
                unicode_width_ok(line, size.0),
                "{name} {size:?}: row too wide: {line:?}"
            );
        }
        check(name, size, &frame);
    }
}

fn unicode_width_ok(line: &str, cols: u16) -> bool {
    line.chars().count() <= cols as usize
}

#[test]
fn greeting_dog_and_ant() {
    all_sizes("greeting-dog", || {
        app(Kind::Dog, Theme::dark(ColorDepth::TrueColor))
    });
    all_sizes("greeting-ant", || {
        app(Kind::Ant, Theme::dark(ColorDepth::TrueColor))
    });
    all_sizes("greeting-dog-light", || {
        app(Kind::Dog, Theme::light(ColorDepth::TrueColor))
    });
}

#[test]
fn mascot_states_render_distinct_poses() {
    for kind in [Kind::Dog, Kind::Ant] {
        let poses: Vec<Vec<String>> = State::ALL
            .iter()
            .map(|s| bud_tui::mascot::pose_rows(kind, *s))
            .collect();
        assert_ne!(poses[0], poses[3], "{kind:?}: error must differ from idle");
        assert_ne!(
            poses[0], poses[2],
            "{kind:?}: waiting must differ from idle"
        );
    }
}

fn markdown_stream() -> App {
    let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
    a.on_event(&ev(
        "user.message",
        json!({"message_id": "m1", "text": "Summarize the release notes."}),
    ));
    run_started(&mut a);
    let text = "# Release notes\n\nThe **parser** now handles `inline code` and *nested* lists.\n\n- first item\n  - nested item\n- second item\n\n| Check | Status |\n| --- | --- |\n| Tests | passing |\n\n```rust\nfn main() {}\n```\n\nTrailing paragraph still arriv";
    for chunk in text.as_bytes().chunks(9) {
        a.on_event(&live(
            "text.delta",
            json!({"run_id": "r1", "block": "r1:1:0", "text": String::from_utf8_lossy(chunk)}),
        ));
    }
    a
}

#[test]
fn streaming_markdown_mid_reply() {
    all_sizes("stream-markdown", markdown_stream);
}

fn tools(expanded: bool) -> App {
    let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
    a.on_event(&ev(
        "user.message",
        json!({"message_id": "m1", "text": "Check the notes"}),
    ));
    run_started(&mut a);
    for (i, name) in ["notes.md", "plan.md", "todo.md"].iter().enumerate() {
        a.on_event(&ev("tool.call", json!({"run_id": "r1", "call_id": format!("c{i}"), "tool": "workspace_read", "args": {"path": name}})));
        a.on_event(&ev("tool.result", json!({"run_id": "r1", "call_id": format!("c{i}"), "tool": "workspace_read", "result": "{\"text\": \"line one\\nline two\"}", "outcome": "success"})));
    }
    a.on_event(&ev("tool.call", json!({"run_id": "r1", "call_id": "w", "tool": "workspace_update", "args": {"path": "locked.md"}})));
    a.on_event(&ev("tool.result", json!({"run_id": "r1", "call_id": "w", "tool": "workspace_update", "result": "read only", "outcome": "failure"})));
    a.on_event(&ev(
        "tool.call",
        json!({"run_id": "r1", "call_id": "s", "tool": "web_search", "args": {"query": "bud"}}),
    ));
    if expanded {
        a.toggle_tools();
    }
    a
}

#[test]
fn tool_calls_collapsed_and_expanded() {
    all_sizes("tools-collapsed", || tools(false));
    all_sizes("tools-expanded", || tools(true));
}

#[test]
fn approval_card() {
    all_sizes("approval", || {
        let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
        a.on_event(&ev(
            "approval.requested",
            json!({"approval_id": "ap1", "tool": "send_email", "args": {"to": "dana@example.com", "subject": "Quarterly numbers", "body": "Hi Dana,\n\nThe numbers are attached.\nBest,\nAlex"}}),
        ));
        a
    });
}

#[test]
fn question_card() {
    all_sizes("question", || {
        let mut a = app(Kind::Ant, Theme::dark(ColorDepth::TrueColor));
        a.on_event(&ev(
            "approval.requested",
            json!({"approval_id": "q1", "tool": "ask_user", "args": {"question": "Which calendar should I use for the meeting?", "choices": ["Work", "Personal", "Team shared"]}}),
        ));
        a.q_sel = 1;
        a
    });
}

#[test]
fn setup_card() {
    all_sizes("setup", || {
        let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
        a.on_event(&ev(
            "capability.setup",
            json!({"capability": "gmail", "label": "Gmail", "kind": "url", "url": "https://connect.example.com/oauth/start?session=abc123", "status": "waiting"}),
        ));
        a
    });
}

#[test]
fn run_failure_and_error_pose() {
    all_sizes("error", || {
        let mut a = app(Kind::Dog, Theme::light(ColorDepth::TrueColor));
        a.on_event(&ev(
            "user.message",
            json!({"message_id": "m1", "text": "do the thing"}),
        ));
        run_started(&mut a);
        a.on_event(&ev(
            "run.failed",
            json!({"run_id": "r1", "reason": "The model provider returned an error (HTTP 529)."}),
        ));
        a.sync_mascot();
        a
    });
}

#[test]
fn queued_messages_leave_on_dispatch_answer_or_cancel() {
    let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
    run_started(&mut a);
    for id in ["m1", "m2", "m3", "m4"] {
        a.on_event(&ev(
            "message.queued",
            json!({"message_id": id, "text": format!("text {id}")}),
        ));
    }
    assert_eq!(a.queued.len(), 4);
    a.on_event(&ev(
        "message.dispatched",
        json!({"message_id": "m1", "run_id": "r2"}),
    ));
    a.on_event(&ev(
        "user.message",
        json!({"message_id": "m2", "text": "text m2"}),
    ));
    a.on_event(&ev("message.cancelled", json!({"message_id": "m3"})));
    assert_eq!(a.queued.len(), 1);
    // A snapshot replaces the list; it never merges with stale local entries.
    let snap = bud_tui::protocol::Snapshot {
        queued_messages: vec![],
        ..Default::default()
    };
    a.apply_state(&snap);
    assert!(a.queued.is_empty());
}

#[test]
fn question_tabs_when_two_are_pending() {
    all_sizes("question-tabs", || {
        let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
        for (id, q) in [("q1", "Which calendar?"), ("q2", "Which time zone?")] {
            a.on_event(&ev(
                "approval.requested",
                json!({"approval_id": id, "tool": "ask_user", "args": {"question": q, "choices": ["Work", "Personal"]}}),
            ));
        }
        a.q_tab = 1;
        a
    });
}

fn long_draft() -> App {
    let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
    let body: Vec<String> = (1..=30)
        .map(|i| format!("Paragraph line {i} of the full draft the owner must read."))
        .collect();
    a.on_event(&ev(
        "approval.requested",
        json!({"approval_id": "ap1", "tool": "send_email", "args": {
            "to": ["dana@example.com", "lee@example.com", "a-very-long-address.for.testing@subdomain.example.org"],
            "cc": ["boss@example.com"], "bcc": ["audit@example.com"],
            "subject": "Quarterly numbers and the follow-up plan",
            "body": body.join("\n") + "\nEND-OF-DRAFT"}}),
    ));
    a
}

#[test]
fn send_approval_shows_the_whole_draft() {
    all_sizes("send-draft", long_draft);
    // Scrolling reaches the last line of the body.
    for size in SIZES {
        let mut a = long_draft();
        let _ = render(&mut a, size);
        a.card_scroll = a.card_max.get();
        let frame = render(&mut a, size);
        assert!(
            frame.contains("END-OF-DRAFT"),
            "{size:?}: last line must be reachable"
        );
        assert!(frame.contains("end of draft"));
        check("send-draft-end", size, &frame);
    }
    let frame = render(&mut long_draft(), (100, 32));
    for needle in [
        "dana@example.com",
        "lee@example.com",
        "Cc: boss@example.com",
        "Bcc: audit@example.com",
        "Subject: Quarterly",
    ] {
        assert!(frame.contains(needle), "missing {needle}");
    }
}

#[test]
fn memory_learned_is_a_quiet_line() {
    let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
    a.on_event(&ev(
        "memory.learned",
        json!({"run_id": "r1", "lessons": [
            {"kind": "preference", "fact": "prefers short replies", "replaced": null},
            {"kind": "person", "fact": "Dana is the finance lead", "replaced": "Dana is an analyst"}]}),
    ));
    a.on_event(&ev("totally.unknown", json!({})));
    let frame = render(&mut a, (100, 32));
    assert!(frame.contains("noted · prefers short replies"));
    assert!(frame.contains("updated · Dana is the finance lead  (say 'forget that' to undo)"));
    check("memory-learned", (100, 32), &frame);
}

#[test]
fn revising_keeps_the_composer_under_the_card() {
    all_sizes("revising", || {
        let mut a = long_draft();
        a.mode = bud_tui::app::Mode::Revising("ap1".into());
        a
    });
    let frame = render(
        &mut {
            let mut a = long_draft();
            a.mode = bud_tui::app::Mode::Revising("ap1".into());
            a
        },
        (40, 20),
    );
    assert!(
        frame.contains("revise ›"),
        "composer must stay visible at 40x20:\n{frame}"
    );
}

fn press(a: &mut App, code: crossterm::event::KeyCode) {
    a.on_key(crossterm::event::KeyEvent::new(
        code,
        crossterm::event::KeyModifiers::NONE,
    ));
}

fn typed(a: &mut App, text: &str) {
    for c in text.chars() {
        press(a, crossterm::event::KeyCode::Char(c));
    }
}

#[test]
fn slash_menu_executes_instead_of_pasting() {
    use crossterm::event::KeyCode;
    let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
    a.theme_pref = "dark".into();
    typed(&mut a, "/the");
    press(&mut a, KeyCode::Enter);
    assert_eq!(
        a.mode,
        bud_tui::app::Mode::Picker,
        "Enter on a menu row opens its picker"
    );
    assert!(
        a.composer.is_empty(),
        "the command name is not pasted into the composer"
    );
    // The cursor starts on the current value; moving previews the next theme live.
    press(&mut a, KeyCode::Down);
    assert_eq!(a.theme.name, "bud-light");
    // Esc reverts the preview and restores the draft the menu was opened from.
    press(&mut a, KeyCode::Esc);
    assert_eq!(a.theme.name, "bud-dark");
    assert_eq!(a.composer.text, "/the");
    assert_eq!(a.mode, bud_tui::app::Mode::Prompt);
    // Tab does the same; Enter on a row keeps the previewed theme.
    press(&mut a, KeyCode::Tab);
    press(&mut a, KeyCode::Down);
    press(&mut a, KeyCode::Enter);
    assert_eq!(a.theme.name, "bud-light");
    assert_eq!(a.theme_pref, "light");
    assert!(a.composer.is_empty());
    // The typed argument still works as a shortcut.
    typed(&mut a, "/theme dark");
    press(&mut a, KeyCode::Enter);
    assert_eq!(a.theme.name, "bud-dark");
}

#[test]
fn exact_command_name_wins_over_longer_prefix_in_the_menu() {
    use crossterm::event::KeyCode;
    let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
    typed(&mut a, "/mode");
    press(&mut a, KeyCode::Enter);
    let picker = a.picker.as_ref().expect("mode picker");
    assert_eq!(picker.kind, bud_tui::app::PickKind::Mode);
    assert_eq!(
        picker.items[picker.selected].value, "strict",
        "cursor starts on the current mode"
    );
}

#[test]
fn picker_filter_and_empty_states() {
    use crossterm::event::KeyCode;
    let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
    a.theme_pref = "dark".into();
    a.run_command("/theme");
    typed(&mut a, "li");
    assert_eq!(a.picker.as_ref().expect("picker").visible().len(), 1);
    press(&mut a, KeyCode::Esc); // leave the field, the filter stays
    assert_eq!(a.picker.as_ref().expect("picker").filter, "li");
    press(&mut a, KeyCode::Esc); // clear the filter
    assert!(a.picker.as_ref().expect("picker").filter.is_empty());
    press(&mut a, KeyCode::Esc); // close
    assert!(a.picker.is_none());
    a.run_command("/cancel");
    assert!(a.picker.as_ref().expect("picker").items.is_empty());
}

#[test]
fn queue_pickers_cancel_and_edit() {
    use crossterm::event::KeyCode;
    let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
    run_started(&mut a);
    a.on_event(&ev(
        "message.queued",
        json!({"message_id": "m1", "text": "first queued text"}),
    ));
    a.on_event(&ev(
        "message.queued",
        json!({"message_id": "m2", "text": "second queued text"}),
    ));
    a.run_command("/edit");
    press(&mut a, KeyCode::Down);
    press(&mut a, KeyCode::Enter);
    assert_eq!(a.mode, bud_tui::app::Mode::Editing("m2".into()));
    assert_eq!(
        a.composer.text, "second queued text",
        "edit opens the composer prefilled"
    );
    press(&mut a, KeyCode::Esc);
    assert_eq!(a.mode, bud_tui::app::Mode::Prompt);
}

fn picker_frames(name: &str, open: impl Fn(&mut App)) {
    all_sizes(name, || {
        let mut a = app(Kind::Dog, Theme::dark(ColorDepth::TrueColor));
        a.theme_pref = "dark".into();
        a.approval_mode = "auto".into();
        open(&mut a);
        a
    });
}

#[test]
fn picker_cards() {
    picker_frames("picker-theme", |a| a.run_command("/theme"));
    picker_frames("picker-mascot", |a| a.run_command("/mascot"));
    picker_frames("picker-mode", |a| a.run_command("/mode"));
    picker_frames("picker-connect", |a| {
        a.on_outcome(bud_tui::app::Outcome::ConnectStates(
            json!({"statuses": {"gmail": "ACTIVE", "calendar": "NOT_CONNECTED"}}),
        ));
    });
    picker_frames("picker-cancel", |a| {
        run_started(a);
        a.on_event(&ev("message.queued", json!({"message_id": "m1", "text": "remind me to call Dana about the quarterly numbers"})));
        a.on_event(&ev(
            "message.queued",
            json!({"message_id": "m2", "text": "and book the review"}),
        ));
        a.run_command("/cancel");
    });
    picker_frames("picker-empty", |a| a.run_command("/edit"));
    picker_frames("picker-filter", |a| {
        a.on_outcome(bud_tui::app::Outcome::Models(
            ["stub-fast", "stub-smart", "stub-local", "other-model"]
                .iter()
                .map(|id| bud_tui::protocol::ModelInfo {
                    id: (*id).into(),
                    tier: "fast".into(),
                    billing: "api".into(),
                    configured: true,
                })
                .collect(),
        ));
        typed(a, "stub");
    });
}
