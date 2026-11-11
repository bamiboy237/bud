//! HTTP half of bud's protocol: handshake, sessions, messages, decisions, controls.

use crate::protocol::{ModelInfo, Runtime, Snapshot};
use anyhow::{anyhow, Result};
use reqwest::{header, Client, Method, RequestBuilder, StatusCode};
use serde_json::{json, Value};
use std::time::Duration;
use url::Url;

#[derive(Debug, Clone)]
pub struct CoreError {
    pub status: Option<u16>,
    pub detail: String,
}

impl std::fmt::Display for CoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.status {
            Some(code) => write!(f, "HTTP {code}: {}", self.detail),
            None => write!(f, "{}", self.detail),
        }
    }
}

impl std::error::Error for CoreError {}

type Res<T> = std::result::Result<T, CoreError>;

fn transport(error: reqwest::Error) -> CoreError {
    let detail = if error.is_connect() {
        "cannot reach the bud server".to_string()
    } else if error.is_timeout() {
        "the bud server timed out".to_string()
    } else {
        "request failed".to_string()
    };
    CoreError {
        status: None,
        detail,
    }
}

/// An authenticated handle on one bud server. Cheap to clone.
#[derive(Clone)]
pub struct Core {
    http: Client,
    base: Url,
}

fn origin() -> Value {
    json!({"channel": "terminal", "client_id": "terminal"})
}

impl Core {
    pub fn new(base: Url, token: &str) -> Result<Self> {
        let mut headers = header::HeaderMap::new();
        let mut auth = header::HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|_| anyhow!("The owner token contains invalid characters."))?;
        auth.set_sensitive(true);
        headers.insert(header::AUTHORIZATION, auth);
        let http = Client::builder()
            .default_headers(headers)
            .connect_timeout(Duration::from_secs(5))
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Core { http, base })
    }

    /// Pins every later request to the account scope the handshake returned.
    pub fn with_scope(mut self, scope: &str, token: &str) -> Result<Self> {
        let mut headers = header::HeaderMap::new();
        let mut auth = header::HeaderValue::from_str(&format!("Bearer {token}"))?;
        auth.set_sensitive(true);
        headers.insert(header::AUTHORIZATION, auth);
        if !scope.is_empty() {
            headers.insert("x-bud-scope", header::HeaderValue::from_str(scope)?);
        }
        self.http = Client::builder()
            .default_headers(headers)
            .connect_timeout(Duration::from_secs(5))
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(self)
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base.as_str().trim_end_matches('/'), path)
    }

    fn req(&self, method: Method, path: &str) -> RequestBuilder {
        self.http
            .request(method, self.url(path))
            .timeout(Duration::from_secs(10))
    }

    pub fn stream_request(&self, session: &str, last_seq: u64) -> RequestBuilder {
        self.http
            .get(self.url(&format!("/sessions/{session}/events")))
            .header("Last-Event-ID", last_seq.to_string())
    }

    async fn send(&self, request: RequestBuilder) -> Res<Value> {
        let response = request.send().await.map_err(transport)?;
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        if status.is_success() {
            return Ok(serde_json::from_str(&body).unwrap_or(Value::Null));
        }
        let detail = serde_json::from_str::<Value>(&body)
            .ok()
            .and_then(|v| match v.get("detail") {
                Some(Value::String(s)) => Some(s.clone()),
                Some(other) => Some(other.to_string()),
                None => None,
            })
            .unwrap_or_else(|| {
                status
                    .canonical_reason()
                    .unwrap_or("request failed")
                    .to_string()
            });
        Err(CoreError {
            status: Some(status.as_u16()),
            detail: crate::sanitize::clean_line(&detail),
        })
    }

    pub async fn runtime(&self) -> Res<Runtime> {
        let value = self.send(self.req(Method::GET, "/runtime")).await?;
        Ok(serde_json::from_value(value).unwrap_or_default())
    }

    pub async fn models(&self) -> Res<Vec<ModelInfo>> {
        let value = self.send(self.req(Method::GET, "/models")).await?;
        Ok(serde_json::from_value(value).unwrap_or_default())
    }

    pub async fn open_session(
        &self,
        session: Option<&str>,
        new: bool,
        model: Option<&str>,
    ) -> Res<String> {
        if let Some(id) = session {
            self.snapshot(id).await?;
            return Ok(id.to_string());
        }
        let body = json!({ "model": model });
        let value = if new {
            let body =
                json!({"session_id": uuid::Uuid::new_v4().simple().to_string(), "model": model});
            self.send(self.req(Method::POST, "/sessions").json(&body))
                .await?
        } else {
            self.send(self.req(Method::POST, "/sessions/primary").json(&body))
                .await?
        };
        value
            .get("session_id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or(CoreError {
                status: None,
                detail: "the server returned no session".into(),
            })
    }

    pub async fn snapshot(&self, session: &str) -> Res<Snapshot> {
        let value = self
            .send(self.req(Method::GET, &format!("/sessions/{session}")))
            .await?;
        serde_json::from_value(value).map_err(|_| CoreError {
            status: None,
            detail: "the server sent an unreadable snapshot".into(),
        })
    }

    /// Returns true when the server already had this message id.
    pub async fn post_message(&self, session: &str, text: &str, message_id: &str) -> Res<bool> {
        let body = json!({"text": text, "message_id": message_id, "origin": origin()});
        let value = self
            .send(
                self.req(Method::POST, &format!("/sessions/{session}/messages"))
                    .json(&body),
            )
            .await?;
        Ok(value
            .get("duplicate")
            .and_then(Value::as_bool)
            .unwrap_or(false))
    }

    pub async fn decide(&self, session: &str, approval: &str, approved: bool) -> Res<bool> {
        let body = json!({"approved": approved, "origin": origin()});
        let value = self
            .send(
                self.req(
                    Method::POST,
                    &format!("/sessions/{session}/approvals/{approval}"),
                )
                .json(&body),
            )
            .await?;
        Ok(value
            .get("applied")
            .and_then(Value::as_bool)
            .unwrap_or(false))
    }

    pub async fn revise(
        &self,
        session: &str,
        approval: &str,
        text: &str,
        message_id: &str,
    ) -> Res<bool> {
        let body = json!({"text": text, "message_id": message_id, "origin": origin()});
        let value = self
            .send(
                self.req(
                    Method::POST,
                    &format!("/sessions/{session}/approvals/{approval}/revise"),
                )
                .json(&body),
            )
            .await?;
        Ok(value
            .get("applied")
            .and_then(Value::as_bool)
            .unwrap_or(false))
    }

    pub async fn answer(&self, session: &str, approval: &str, text: &str) -> Res<bool> {
        let body = json!({"text": text, "origin": origin()});
        let value = self
            .send(
                self.req(
                    Method::POST,
                    &format!("/sessions/{session}/approvals/{approval}/answer"),
                )
                .json(&body),
            )
            .await?;
        Ok(value
            .get("answered")
            .and_then(Value::as_bool)
            .unwrap_or(false))
    }

    /// Returns whether a run was actually asked to stop.
    pub async fn stop(&self, session: &str) -> Res<bool> {
        let value = self
            .send(self.req(Method::POST, &format!("/sessions/{session}/stop")))
            .await?;
        Ok(value
            .get("stop_requested")
            .and_then(Value::as_bool)
            .unwrap_or(false))
    }

    pub async fn queue(&self, session: &str, action: &str) -> Res<()> {
        self.send(self.req(Method::POST, &format!("/sessions/{session}/queue/{action}")))
            .await
            .map(|_| ())
    }

    pub async fn edit_queued(&self, session: &str, message_id: &str, text: &str) -> Res<()> {
        self.send(
            self.req(
                Method::PATCH,
                &format!("/sessions/{session}/queue/{message_id}"),
            )
            .json(&json!({"text": text})),
        )
        .await
        .map(|_| ())
    }

    pub async fn cancel_queued(&self, session: &str, message_id: &str) -> Res<bool> {
        let value = self
            .send(self.req(
                Method::DELETE,
                &format!("/sessions/{session}/queue/{message_id}"),
            ))
            .await?;
        Ok(value
            .get("cancelled")
            .and_then(Value::as_bool)
            .unwrap_or(false))
    }

    /// Returns the mode summary sentence.
    pub async fn set_mode(&self, mode: &str) -> Res<(String, String)> {
        let value = self
            .send(self.req(Method::POST, "/mode").json(&json!({"mode": mode})))
            .await?;
        let get = |k: &str| {
            value
                .get(k)
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string()
        };
        Ok((get("mode"), get("summary")))
    }

    pub async fn set_model(&self, session: &str, model: &str) -> Res<()> {
        self.send(
            self.req(Method::POST, &format!("/sessions/{session}/model"))
                .json(&json!({"model": model})),
        )
        .await
        .map(|_| ())
    }

    pub async fn connections(&self, refresh: bool) -> Res<Value> {
        let request = if refresh {
            self.req(Method::POST, "/connections/refresh")
        } else {
            self.req(Method::GET, "/connections")
        };
        self.send(request).await
    }

    /// The sign-in link exists only in this response; it never enters the event log.
    pub async fn connect(&self, toolkit: &str) -> Res<String> {
        let value = self
            .send(self.req(Method::POST, &format!("/connections/{toolkit}/connect")))
            .await?;
        value
            .get("url")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or(CoreError {
                status: None,
                detail: "the server returned no sign-in link".into(),
            })
    }
}

pub fn is_fatal(status: Option<StatusCode>) -> bool {
    matches!(
        status,
        Some(StatusCode::UNAUTHORIZED | StatusCode::NOT_FOUND)
    )
}
