//! Wire types for bud's HTTP+SSE core. Only the fields the client reads.

use serde::Deserialize;
use serde_json::{Map, Value};

pub const DEFAULT_URL: &str = "http://127.0.0.1:8420";

#[derive(Debug, Clone, Deserialize)]
pub struct Event {
    #[serde(default)]
    pub seq: u64,
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub data: Value,
    #[serde(default)]
    pub live: bool,
}

impl Event {
    pub fn str(&self, key: &str) -> &str {
        self.data.get(key).and_then(Value::as_str).unwrap_or("")
    }

    pub fn opt_str(&self, key: &str) -> Option<String> {
        self.data
            .get(key)
            .and_then(Value::as_str)
            .map(str::to_owned)
    }
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct Runtime {
    #[serde(default)]
    pub mode: String,
    #[serde(default)]
    pub scope: String,
    #[serde(default)]
    pub restart_required: bool,
    #[serde(default)]
    pub messaging: String,
    #[serde(default)]
    pub approval_mode: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ModelInfo {
    pub id: String,
    #[serde(default)]
    pub tier: String,
    #[serde(default)]
    pub billing: String,
    #[serde(default)]
    pub configured: bool,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct PendingApproval {
    pub approval_id: String,
    #[serde(default)]
    pub tool: String,
    #[serde(default)]
    pub args: Map<String, Value>,
    #[serde(default)]
    pub task_id: Option<u64>,
    #[serde(default)]
    pub reference: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct PendingSetup {
    pub capability: String,
    #[serde(default)]
    pub label: Option<String>,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub steps: Vec<String>,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct Turn {
    #[serde(default)]
    pub role: String,
    #[serde(default)]
    pub text: String,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct QueuedMessage {
    pub message_id: String,
    #[serde(default)]
    pub text: String,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct Snapshot {
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub last_seq: u64,
    #[serde(default)]
    pub run_active: bool,
    #[serde(default)]
    pub pending_approvals: Vec<PendingApproval>,
    #[serde(default)]
    pub pending_setup: Vec<PendingSetup>,
    #[serde(default)]
    pub transcript: Vec<Turn>,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub queue_paused: bool,
    #[serde(default)]
    pub queued_messages: Vec<QueuedMessage>,
}

/// Reply text of a run-end event: every text part of every response message.
pub fn reply_text(data: &Value) -> String {
    let mut out = String::new();
    let Some(messages) = data.get("messages").and_then(Value::as_array) else {
        return out;
    };
    for message in messages {
        if message.get("kind").and_then(Value::as_str) != Some("response") {
            continue;
        }
        let Some(parts) = message.get("parts").and_then(Value::as_array) else {
            continue;
        };
        for part in parts {
            if part.get("part_kind").and_then(Value::as_str) == Some("text") {
                out.push_str(part.get("content").and_then(Value::as_str).unwrap_or(""));
            }
        }
    }
    out
}
