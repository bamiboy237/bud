//! Owner token discovery, matching the Python client: env, `.env`, then `DATA_DIR/auth/owner.token`.

use anyhow::{anyhow, bail, Context, Result};
use std::path::{Path, PathBuf};

fn valid(token: &str) -> bool {
    (43..=256).contains(&token.len())
        && token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn dotenv_value(key: &str) -> Option<String> {
    let text = std::fs::read_to_string(".env").ok()?;
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with('#') {
            continue;
        }
        let Some((k, v)) = line.split_once('=') else {
            continue;
        };
        if k.trim().eq_ignore_ascii_case(key) {
            return Some(v.trim().trim_matches(['"', '\'']).to_string());
        }
    }
    None
}

fn setting(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .filter(|v| !v.is_empty())
        .or_else(|| dotenv_value(key))
}

pub fn data_dir(flag: Option<&Path>) -> PathBuf {
    flag.map(Path::to_path_buf)
        .or_else(|| setting("DATA_DIR").map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("data"))
}

pub fn owner_token(data_dir_flag: Option<&Path>) -> Result<String> {
    if let Some(token) = setting("OWNER_TOKEN") {
        if !valid(&token) {
            bail!("OWNER_TOKEN must be a strong base64url secret of 43 to 256 characters.");
        }
        return Ok(token);
    }
    let path = data_dir(data_dir_flag).join("auth").join("owner.token");
    let raw = std::fs::read_to_string(&path).with_context(|| {
        format!(
            "Cannot read the owner token at {}. Start `bud serve` once, or set OWNER_TOKEN / DATA_DIR.",
            path.display()
        )
    })?;
    let token = raw.trim().to_string();
    if !valid(&token) {
        return Err(anyhow!("Invalid owner token file at {}.", path.display()));
    }
    Ok(token)
}

/// Never send the owner secret over remote plaintext HTTP, a path, a query, or credentials.
pub fn check_origin(base: &str) -> Result<url::Url> {
    let url = url::Url::parse(base).context("The server URL is not valid.")?;
    let loopback = matches!(
        url.host_str(),
        Some("127.0.0.1" | "::1" | "[::1]" | "localhost")
    );
    let plain_path = matches!(url.path(), "" | "/");
    let ok = matches!(url.scheme(), "http" | "https")
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
        && url.fragment().is_none()
        && plain_path
        && (url.scheme() == "https" || loopback);
    if !ok {
        bail!(
            "Use a plain server origin: loopback HTTP or trusted HTTPS, without credentials, query parameters or a path."
        );
    }
    Ok(url)
}
