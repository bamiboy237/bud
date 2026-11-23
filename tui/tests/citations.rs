//! Replies cite earlier messages by tags like `[S1]`; the terminal shows each as its date.

use bud_tui::protocol::{plain_citations, reply_text};
use serde_json::json;

#[test]
fn known_tags_read_as_dates_and_invented_ones_disappear() {
    let sources = json!({"S1": {"label": "Nov 18"}});
    assert_eq!(
        plain_citations("We chose SQLite [S1], not Postgres [S7].", Some(&sources)),
        "We chose SQLite (Nov 18), not Postgres."
    );
    assert_eq!(
        plain_citations("No tags [Sx] or [S1234567] here.", Some(&sources)),
        "No tags [Sx] or [S1234567] here."
    );
    assert_eq!(plain_citations("Lost [S1].", None), "Lost.");
}

#[test]
fn a_finished_reply_uses_its_runs_sources() {
    let data = json!({
        "sources": {"S1": {"label": "Mar 2, 2025"}},
        "messages": [{"kind": "response", "parts": [
            {"part_kind": "text", "content": "Tuesday [S1]."}
        ]}],
    });
    assert_eq!(reply_text(&data), "Tuesday (Mar 2, 2025).");
}

#[test]
fn workspace_sources_render_their_path_label() {
    let data = json!({
        "sources": {
            "S1": {
                "kind": "workspace",
                "path": "reports/weekly.md",
                "date": "2026-11-18",
                "label": "reports/weekly.md",
            }
        },
        "messages": [{"kind": "response", "parts": [
            {"part_kind": "text", "content": "See the weekly summary [S1]."}
        ]}],
    });
    assert_eq!(
        reply_text(&data),
        "See the weekly summary (reports/weekly.md)."
    );
}
