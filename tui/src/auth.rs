//! Owner token discovery, matching the Python client: env, `.env`, then `DATA_DIR/auth/owner.token`.

use anyhow::{anyhow, bail, Context, Result};
use std::path::{Path, PathBuf};

fn valid(token: &str) -> bool {
    (43..=256).contains(&token.len())
        && token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn dotenv_value_in(path: &Path, key: &str) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
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

fn expand_tilde(raw: &str, home: Option<&Path>) -> PathBuf {
    if let Some(rest) = raw.strip_prefix("~/") {
        if let Some(h) = home {
            return h.join(rest);
        }
    } else if raw == "~" {
        if let Some(h) = home {
            return h.to_path_buf();
        }
    }
    PathBuf::from(raw)
}

#[derive(Debug, PartialEq, Eq)]
struct AuthLayout {
    env_file: Option<PathBuf>,
    default_data_dir: PathBuf,
    relative_base: Option<PathBuf>,
}

fn resolve_auth_layout(cwd: &Path, home: Option<&Path>, bud_home: Option<&str>) -> AuthLayout {
    if let Some(raw_home) = bud_home.map(str::trim).filter(|v| !v.is_empty()) {
        let root = expand_tilde(raw_home, home);
        let env_file = [root.join("config.env"), root.join(".env")]
            .into_iter()
            .find(|p| p.is_file());
        return AuthLayout {
            env_file,
            default_data_dir: root.join("data"),
            relative_base: Some(root),
        };
    }
    if cwd.join(".env").is_file() || cwd.join("data").is_dir() {
        return AuthLayout {
            env_file: Some(cwd.join(".env")).filter(|p| p.is_file()),
            default_data_dir: PathBuf::from("data"),
            relative_base: None,
        };
    }
    if let Some(root) = home.map(|h| h.join(".bud")) {
        if root.join("config.env").is_file() || root.join("data").is_dir() {
            let env_file = [root.join("config.env"), root.join(".env")]
                .into_iter()
                .find(|p| p.is_file());
            return AuthLayout {
                env_file,
                default_data_dir: root.join("data"),
                relative_base: Some(root),
            };
        }
    }
    AuthLayout {
        env_file: Some(cwd.join(".env")).filter(|p| p.is_file()),
        default_data_dir: PathBuf::from("data"),
        relative_base: None,
    }
}

fn current_layout() -> AuthLayout {
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let home = std::env::var_os("HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from);
    let bud_home = std::env::var("BUD_HOME").ok();
    resolve_auth_layout(&cwd, home.as_deref(), bud_home.as_deref())
}

fn setting_in(layout: &AuthLayout, key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .filter(|v| !v.is_empty())
        .or_else(|| {
            layout
                .env_file
                .as_deref()
                .and_then(|p| dotenv_value_in(p, key))
        })
}

fn setting(key: &str) -> Option<String> {
    setting_in(&current_layout(), key)
}

pub fn data_dir(flag: Option<&Path>) -> PathBuf {
    if let Some(explicit) = flag {
        return explicit.to_path_buf();
    }
    let layout = current_layout();
    if let Ok(env_val) = std::env::var("DATA_DIR") {
        if !env_val.is_empty() {
            return PathBuf::from(env_val);
        }
    }
    if let Some(dotenv_val) = layout
        .env_file
        .as_deref()
        .and_then(|p| dotenv_value_in(p, "DATA_DIR"))
        .filter(|v| !v.is_empty())
    {
        let parsed = PathBuf::from(&dotenv_val);
        if parsed.is_relative() {
            if let Some(base) = &layout.relative_base {
                return base.join(parsed);
            }
        }
        return parsed;
    }
    layout.default_data_dir
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    struct TempTree(PathBuf);

    impl TempTree {
        fn new(name: &str) -> Self {
            let stamp = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock")
                .as_nanos();
            let path = std::env::temp_dir().join(format!("bud-tui-{name}-{stamp}"));
            std::fs::create_dir_all(&path).expect("create temp dir");
            Self(path)
        }
    }

    impl Drop for TempTree {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn resolve_auth_layout_prefers_bud_home_then_cwd_then_home_dot_bud() {
        let tree = TempTree::new("layout");
        let home = tree.0.join("home");
        let dot_bud = home.join(".bud");
        let cwd = tree.0.join("repo");
        let empty_cwd = tree.0.join("empty");
        let custom_home = tree.0.join("custom");
        std::fs::create_dir_all(&dot_bud).unwrap();
        std::fs::create_dir_all(&cwd).unwrap();
        std::fs::create_dir_all(&empty_cwd).unwrap();
        std::fs::create_dir_all(&custom_home).unwrap();
        std::fs::write(dot_bud.join("config.env"), "OPENAI_API_KEY=k\n").unwrap();
        std::fs::write(cwd.join(".env"), "OPENAI_API_KEY=repo\n").unwrap();
        std::fs::write(custom_home.join("config.env"), "OPENAI_API_KEY=custom\n").unwrap();

        let explicit = resolve_auth_layout(&cwd, Some(&home), Some(custom_home.to_str().unwrap()));
        assert_eq!(explicit.env_file, Some(custom_home.join("config.env")));
        assert_eq!(explicit.default_data_dir, custom_home.join("data"));
        assert_eq!(explicit.relative_base, Some(custom_home.clone()));

        let tilde = resolve_auth_layout(&cwd, Some(&home), Some("~/.bud"));
        assert_eq!(tilde.env_file, Some(dot_bud.join("config.env")));
        assert_eq!(tilde.default_data_dir, dot_bud.join("data"));

        let repo = resolve_auth_layout(&cwd, Some(&home), None);
        assert_eq!(repo.env_file, Some(cwd.join(".env")));
        assert_eq!(repo.default_data_dir, PathBuf::from("data"));
        assert_eq!(repo.relative_base, None);

        let fallback = resolve_auth_layout(&empty_cwd, Some(&home), None);
        assert_eq!(fallback.env_file, Some(dot_bud.join("config.env")));
        assert_eq!(fallback.default_data_dir, dot_bud.join("data"));
        assert_eq!(fallback.relative_base, Some(dot_bud));
    }
}
