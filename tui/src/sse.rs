//! SSE follower: resumes from the last stored seq, reconnects forever, stops on 401/404.

use crate::client::Core;
use crate::protocol::Event;
use futures_util::StreamExt;
use std::time::Duration;
use tokio::sync::mpsc::UnboundedSender;

#[derive(Debug)]
pub enum Feed {
    Event(Event),
    Connected,
    Lost,
    Fatal(u16),
}

/// Accumulates bytes into complete `data:` payloads, one per SSE frame.
#[derive(Default)]
pub struct FrameParser {
    buffer: Vec<u8>,
    data: String,
}

impl FrameParser {
    pub fn push(&mut self, bytes: &[u8]) -> Vec<String> {
        self.buffer.extend_from_slice(bytes);
        let mut frames = Vec::new();
        while let Some(pos) = self.buffer.iter().position(|b| *b == b'\n') {
            let mut line: Vec<u8> = self.buffer.drain(..=pos).collect();
            line.pop();
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            let line = String::from_utf8_lossy(&line);
            if line.is_empty() {
                if !self.data.is_empty() {
                    frames.push(std::mem::take(&mut self.data));
                }
            } else if let Some(rest) = line.strip_prefix("data:") {
                if !self.data.is_empty() {
                    self.data.push('\n');
                }
                self.data.push_str(rest.strip_prefix(' ').unwrap_or(rest));
            }
        }
        frames
    }
}

pub async fn follow(core: Core, session: String, mut last_seq: u64, tx: UnboundedSender<Feed>) {
    loop {
        let response = core.stream_request(&session, last_seq).send().await;
        match response {
            Ok(response) if response.status().is_success() => {
                if tx.send(Feed::Connected).is_err() {
                    return;
                }
                let mut parser = FrameParser::default();
                let mut stream = response.bytes_stream();
                while let Some(chunk) = stream.next().await {
                    let Ok(chunk) = chunk else { break };
                    for frame in parser.push(&chunk) {
                        let Ok(event) = serde_json::from_str::<Event>(&frame) else {
                            continue;
                        };
                        if !event.live {
                            last_seq = last_seq.max(event.seq);
                        }
                        if tx.send(Feed::Event(event)).is_err() {
                            return;
                        }
                    }
                }
            }
            Ok(response) => {
                let code = response.status().as_u16();
                if code == 401 || code == 404 || code == 409 {
                    let _ = tx.send(Feed::Fatal(code));
                    return;
                }
            }
            Err(_) => {}
        }
        if tx.send(Feed::Lost).is_err() {
            return;
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}
