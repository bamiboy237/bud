use bud_tui::app::{App, Entry, Status, UserState};
use bud_tui::mascot::Kind;
use bud_tui::protocol::{trailing_reply_text, Event};
use bud_tui::theme::{ColorDepth, Theme};
use serde_json::{json, Value};

fn ev(kind: &str, data: Value) -> Event {
    Event {
        kind: kind.to_string(),
        seq: 1,
        data,
        live: false,
    }
}

#[test]
fn trailing_reply_text_splits_on_steered_request() {
    let data = json!({
        "messages": [
            {"kind": "response", "parts": [{"part_kind": "text", "content": "First half"}]},
            {"kind": "request", "metadata": {"bud_steer": true}, "parts": [{"part_kind": "user-prompt", "content": "Pivot"}]},
            {"kind": "response", "parts": [{"part_kind": "text", "content": "Second half"}]}
        ]
    });
    assert_eq!(trailing_reply_text(&data), "Second half");
}

#[test]
fn mid_run_steer_preserves_pre_steer_assistant_and_reconciles_post_steer_segment() {
    let mut app = App::new(Theme::dark(ColorDepth::TrueColor), Kind::Dog, "gpt-5");
    app.on_event(&ev(
        "user.message",
        json!({"message_id": "m1", "text": "Initial"}),
    ));
    app.on_event(&ev("run.started", json!({"run_id": "r1"})));
    app.on_event(&ev(
        "text.delta",
        json!({"run_id": "r1", "block": "r1:0:0", "text": "Pre-steer text"}),
    ));
    assert_eq!(app.status, Status::Streaming);

    app.on_event(&ev(
        "message.queued",
        json!({"message_id": "m2", "text": "Steer now"}),
    ));
    assert_eq!(app.queued.len(), 1);

    app.on_event(&ev(
        "user.message",
        json!({"message_id": "m2", "text": "Steer now", "run_id": "r1", "steered": true}),
    ));
    assert!(app.queued.is_empty());
    assert_eq!(app.status, Status::Thinking);

    app.on_event(&ev(
        "text.delta",
        json!({"run_id": "r1", "block": "r1:1:0", "text": "Post-steer"}),
    ));
    app.on_event(&ev(
        "run.completed",
        json!({
            "run_id": "r1",
            "messages": [
                {"kind": "response", "parts": [{"part_kind": "text", "content": "Pre-steer text"}]},
                {"kind": "request", "metadata": {"bud_steer": true}, "parts": [{"part_kind": "user-prompt", "content": "Steer now"}]},
                {"kind": "response", "parts": [{"part_kind": "text", "content": "Post-steer final"}]}
            ]
        }),
    ));

    let summary: Vec<(&str, String)> = app
        .entries
        .iter()
        .filter_map(|e| match e {
            Entry::User(u) => {
                assert_eq!(u.state, UserState::Sent);
                Some(("user", u.text.clone()))
            }
            Entry::Assistant(a) => {
                assert!(!a.streaming);
                let txt: String = a.blocks.iter().map(|(_, t)| t.as_str()).collect();
                Some(("assistant", txt))
            }
            _ => None,
        })
        .collect();
    assert_eq!(
        summary,
        vec![
            ("user", "Initial".to_string()),
            ("assistant", "Pre-steer text".to_string()),
            ("user", "Steer now".to_string()),
            ("assistant", "Post-steer final".to_string()),
        ]
    );
}
