#!/usr/bin/env bash
set -euo pipefail

# Public macOS installer & non-destructive updater for Bud.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/bamiboy237/bud/master/scripts/install.sh | bash

DEFAULT_REPO="bamiboy237/bud"
DEFAULT_HOME="$HOME/.bud"
DEFAULT_APP_DIR="/Applications"
DEFAULT_PORT="8420"

REPO="${BUD_REPO:-$DEFAULT_REPO}"
VERSION="${BUD_VERSION:-}"
BUD_HOME_DIR="${BUD_HOME:-$DEFAULT_HOME}"
PORT="${BUD_PORT:-$DEFAULT_PORT}"
ARCHIVE="${BUD_ARCHIVE:-}"
CHECKSUMS="${BUD_CHECKSUMS:-}"
APP_DIR="${BUD_APP_DIR:-$DEFAULT_APP_DIR}"
APP_DIR_EXPLICIT=0
if [ -n "${BUD_APP_DIR:-}" ]; then
  APP_DIR_EXPLICIT=1
fi

NO_APP=0
NO_SERVICE=0
NO_MODIFY_PATH=0
FORCE=0

TMP_DIR=""
STAGED_RELEASE_DIR=""
PYTHON_BIN=""
UV_BIN=""
INSTALLED_UV=0

usage() {
  cat <<'EOF'
Bud macOS Installer

Installs or safely updates standalone Bud into ~/.bud, /Applications/Bud.app,
and the app.bud.server LaunchAgent without wiping existing data.

Usage:
  install.sh [options]

Options:
  --version <tag>       Install a specific GitHub release tag (e.g. v0.1.0)
  --home <dir>          Bud home directory (default: ~/.bud, or $BUD_HOME)
  --app-dir <dir>       Mac app install folder (default: /Applications, or ~/Applications if unwritable)
  --port <port>         Local server port (default: 8420)
  --repo <owner/repo>   GitHub repository (default: bamiboy237/bud)
  --archive <path>      Install from a local .tar.gz release archive instead of GitHub
  --checksums <path>    Path to SHA256SUMS.txt (required with --archive if not beside archive)
  --no-app              Skip installing Bud.app
  --no-service          Skip installing/starting the LaunchAgent service
  --no-modify-path      Do not edit shell profile files for PATH
  --force               Update even if a run appears active
  -h, --help            Show this help text
EOF
}

cleanup() {
  if [ -n "$STAGED_RELEASE_DIR" ] && [ -d "$STAGED_RELEASE_DIR" ] && [ -n "$PYTHON_BIN" ]; then
    "$PYTHON_BIN" - "$BUD_HOME_DIR" "$STAGED_RELEASE_DIR" <<'PY' >/dev/null 2>&1 || true
import shutil
import sys
from pathlib import Path

home = Path(sys.argv[1]).expanduser().resolve()
staged = Path(sys.argv[2]).resolve()
current = home / "releases" / "current"
points_here = False
try:
    points_here = current.exists() and current.resolve() == staged
except OSError:
    points_here = False
if not points_here:
    shutil.rmtree(staged, ignore_errors=True)
PY
  elif [ -n "$STAGED_RELEASE_DIR" ] && [ -d "$STAGED_RELEASE_DIR" ]; then
    rm -rf "$STAGED_RELEASE_DIR"
  fi
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

require_arg() {
  if [ "$#" -lt 2 ] || [ -z "$2" ]; then
    echo "Error: Option $1 requires a value." >&2
    exit 1
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version)
      require_arg "$@"
      VERSION="$2"
      shift 2
      ;;
    --version=*)
      VERSION="${1#*=}"
      shift
      ;;
    --home)
      require_arg "$@"
      BUD_HOME_DIR="$2"
      shift 2
      ;;
    --home=*)
      BUD_HOME_DIR="${1#*=}"
      shift
      ;;
    --app-dir)
      require_arg "$@"
      APP_DIR="$2"
      APP_DIR_EXPLICIT=1
      shift 2
      ;;
    --app-dir=*)
      APP_DIR="${1#*=}"
      APP_DIR_EXPLICIT=1
      shift
      ;;
    --port)
      require_arg "$@"
      PORT="$2"
      shift 2
      ;;
    --port=*)
      PORT="${1#*=}"
      shift
      ;;
    --repo)
      require_arg "$@"
      REPO="$2"
      shift 2
      ;;
    --repo=*)
      REPO="${1#*=}"
      shift
      ;;
    --archive)
      require_arg "$@"
      ARCHIVE="$2"
      shift 2
      ;;
    --archive=*)
      ARCHIVE="${1#*=}"
      shift
      ;;
    --checksums)
      require_arg "$@"
      CHECKSUMS="$2"
      shift 2
      ;;
    --checksums=*)
      CHECKSUMS="${1#*=}"
      shift
      ;;
    --no-app)
      NO_APP=1
      shift
      ;;
    --no-service)
      NO_SERVICE=1
      shift
      ;;
    --no-modify-path)
      NO_MODIFY_PATH=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: Unknown option '$1'. Run with --help for usage." >&2
      exit 1
      ;;
  esac
done

case "$PORT" in
  ''|*[!0-9]*)
    echo "Error: Invalid port '$PORT'." >&2
    exit 1
    ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "Error: Port '$PORT' must be between 1 and 65535." >&2
  exit 1
fi

OS_NAME="$(uname -s)"
if [ "$OS_NAME" != "Darwin" ]; then
  echo "Error: Bud installer currently supports macOS (Darwin) only (detected: $OS_NAME)." >&2
  exit 1
fi

RAW_ARCH="$(uname -m)"
case "$RAW_ARCH" in
  arm64|aarch64)
    ARCH="arm64"
    ;;
  x86_64|amd64)
    ARCH="x86_64"
    ;;
  *)
    echo "Error: Unsupported macOS architecture '$RAW_ARCH'." >&2
    exit 1
    ;;
esac

if [ "$NO_APP" -eq 0 ] && [ "$APP_DIR_EXPLICIT" -eq 0 ] && [ ! -w "$APP_DIR" ]; then
  APP_DIR="$HOME/Applications"
  echo "Note: /Applications is not writable; installing Bud.app into $APP_DIR."
fi

if [ -n "$ARCHIVE" ]; then
  if [ ! -f "$ARCHIVE" ]; then
    echo "Error: Release archive not found at '$ARCHIVE'." >&2
    exit 1
  fi
  if [ -z "$CHECKSUMS" ]; then
    CHECKSUMS="$(dirname "$ARCHIVE")/SHA256SUMS.txt"
  fi
  if [ ! -f "$CHECKSUMS" ]; then
    echo "Error: Checksum file missing at '$CHECKSUMS' (SHA-256 verification is required)." >&2
    exit 1
  fi
fi

find_uv() {
  local candidate
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  for candidate in \
    "$HOME/.local/bin/uv" \
    "$HOME/.cargo/bin/uv" \
    "/opt/homebrew/bin/uv" \
    "/usr/local/bin/uv"
  do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

ensure_uv() {
  if UV_BIN="$(find_uv)"; then
    return 0
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "Error: curl is required to install uv." >&2
    exit 1
  fi
  echo "Installing uv..."
  curl -fsSL --proto '=https' --tlsv1.2 https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  INSTALLED_UV=1
  if [ -x "$HOME/.local/bin/uv" ]; then
    UV_BIN="$HOME/.local/bin/uv"
    return 0
  fi
  if UV_BIN="$(find_uv)"; then
    return 0
  fi
  echo "Error: uv installation succeeded but uv binary was not found." >&2
  exit 1
}

ensure_python() {
  local found=""
  if found="$("$UV_BIN" python find --no-project --no-config --system 3.14 2>/dev/null)" && [ -n "$found" ]; then
    PYTHON_BIN="$found"
    return 0
  fi
  if found="$("$UV_BIN" python find --no-project --no-config --managed-python 3.14 2>/dev/null)" && [ -n "$found" ]; then
    PYTHON_BIN="$found"
    return 0
  fi
  echo "Installing Python 3.14 runtime via uv..."
  "$UV_BIN" python install 3.14
  if found="$("$UV_BIN" python find --no-project --no-config --managed-python 3.14 2>/dev/null)" && [ -n "$found" ]; then
    PYTHON_BIN="$found"
    return 0
  fi
  if found="$("$UV_BIN" python find --no-project --no-config --system 3.14 2>/dev/null)" && [ -n "$found" ]; then
    PYTHON_BIN="$found"
    return 0
  fi
  echo "Error: Could not find or install Python 3.14." >&2
  exit 1
}

ensure_uv
ensure_python

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bud-install.XXXXXX")"
chmod 700 "$TMP_DIR"

if [ -n "$ARCHIVE" ]; then
  ARCHIVE_PATH="$ARCHIVE"
  CHECKSUMS_PATH="$CHECKSUMS"
else
  if ! command -v curl >/dev/null 2>&1; then
    echo "Error: curl is required to download Bud releases." >&2
    exit 1
  fi

  RELEASE_META="$TMP_DIR/release_meta.txt"
  "$PYTHON_BIN" - "$REPO" "$ARCH" "$VERSION" >"$RELEASE_META" <<'PY'
import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

repo, arch, requested_version = sys.argv[1], sys.argv[2], sys.argv[3].strip()

candidates: list[str]
if requested_version:
    candidates = [requested_version]
    if not requested_version.startswith("v"):
        candidates.append(f"v{requested_version}")
else:
    candidates = [""]

payload = None
for idx, tag in enumerate(candidates):
    url = (
        f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
        if tag
        else f"https://api.github.com/repos/{repo}/releases/latest"
    )
    req = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "bud-installer",
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        break
    except HTTPError as exc:
        if exc.code == 404 and idx + 1 < len(candidates):
            continue
        if exc.code == 404:
            if requested_version:
                print(
                    f"Error: GitHub release '{requested_version}' not found in {repo}.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Error: No published GitHub releases found in {repo}.\n"
                    "If installing from a source checkout, see docs/setup.md "
                    "(uv run bud install --from-repo . --migrate-from .).",
                    file=sys.stderr,
                )
            sys.exit(1)
        print(f"Error: Failed to query GitHub Releases ({exc.code}): {exc.reason}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error: Failed to query GitHub Releases: {exc}", file=sys.stderr)
        sys.exit(1)

if not isinstance(payload, dict):
    print("Error: Invalid GitHub Release metadata.", file=sys.stderr)
    sys.exit(1)

tag_name = str(payload.get("tag_name") or "").strip()
assets = payload.get("assets") or []
tarball_url = ""
checksums_url = ""
for asset in assets:
    if not isinstance(asset, dict):
        continue
    name = str(asset.get("name") or "")
    asset_url = str(asset.get("browser_download_url") or "")
    if not asset_url:
        continue
    if name == "SHA256SUMS.txt":
        checksums_url = asset_url
    elif name.startswith("bud-") and name.endswith(".tar.gz") and f"darwin-{arch}" in name:
        tarball_url = asset_url

if not tag_name or not tarball_url or not checksums_url:
    print(
        f"Error: Release {tag_name or '(unknown)'} is missing macOS {arch} archive or SHA256SUMS.txt.",
        file=sys.stderr,
    )
    sys.exit(1)

if not tarball_url.startswith("https://") or not checksums_url.startswith("https://"):
    print("Error: Refusing non-HTTPS release asset URL.", file=sys.stderr)
    sys.exit(1)

print(tag_name)
print(tarball_url)
print(checksums_url)
PY

  {
    IFS= read -r RELEASE_TAG
    IFS= read -r TARBALL_URL
    IFS= read -r CHECKSUMS_URL
  } <"$RELEASE_META"

  ARCHIVE_NAME="$(basename "${TARBALL_URL%%\?*}")"
  if [ -z "$ARCHIVE_NAME" ]; then
    ARCHIVE_NAME="bud-${RELEASE_TAG#v}-darwin-${ARCH}.tar.gz"
  fi
  ARCHIVE_PATH="$TMP_DIR/$ARCHIVE_NAME"
  CHECKSUMS_PATH="$TMP_DIR/SHA256SUMS.txt"

  echo "Downloading Bud $RELEASE_TAG ($ARCH)..."
  curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$CHECKSUMS_PATH" "$CHECKSUMS_URL"
  curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$ARCHIVE_PATH" "$TARBALL_URL"
fi

EXTRACT_INFO="$TMP_DIR/extract_info.txt"
"$PYTHON_BIN" - "$ARCHIVE_PATH" "$CHECKSUMS_PATH" "$TMP_DIR/extracted" "$BUD_HOME_DIR" >"$EXTRACT_INFO" <<'PY'
import hashlib
import hmac
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path

archive_path = Path(sys.argv[1]).expanduser().resolve()
checksums_path = Path(sys.argv[2]).expanduser().resolve()
extract_dir = Path(sys.argv[3]).resolve()
bud_home_raw = sys.argv[4]

SHA256_LINE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")
VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-.](.+))?$")

sums_text = checksums_path.read_text(encoding="utf-8")
expected_digest = None
target_name = archive_path.name
for line in sums_text.splitlines():
    match = SHA256_LINE.match(line)
    if not match:
        continue
    digest, fname = match.groups()
    if Path(fname).name == target_name:
        expected_digest = digest.lower()
        break

if expected_digest is None:
    print(
        f"Error: Checksum mismatch: no SHA-256 entry for {target_name} in {checksums_path.name}.",
        file=sys.stderr,
    )
    sys.exit(1)

hasher = hashlib.sha256()
with archive_path.open("rb") as handle:
    while chunk := handle.read(65_536):
        hasher.update(chunk)
actual_digest = hasher.hexdigest().lower()

if not hmac.compare_digest(actual_digest, expected_digest):
    print(
        f"Error: Checksum mismatch for {target_name}: expected {expected_digest}, got {actual_digest}.",
        file=sys.stderr,
    )
    sys.exit(1)

extract_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
extract_resolved = extract_dir.resolve()
with tarfile.open(archive_path, "r:gz") as tar:
    members = tar.getmembers()
    for member in members:
        member_path = Path(member.name)
        if (
            not member.name
            or member_path.is_absolute()
            or ".." in member_path.parts
            or member.issym()
            or member.islnk()
            or not (member.isdir() or member.isreg())
        ):
            print(f"Error: Unsafe member in release archive: {member.name!r}.", file=sys.stderr)
            sys.exit(1)
        resolved = (extract_resolved / member_path).resolve()
        if resolved != extract_resolved and not resolved.is_relative_to(extract_resolved):
            print(
                f"Error: Archive member escapes target folder: {member.name!r}.",
                file=sys.stderr,
            )
            sys.exit(1)

    for member in members:
        target = extract_dir / member.name
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        extracted = tar.extractfile(member)
        if extracted is None:
            print(f"Error: Could not read archive entry: {member.name!r}.", file=sys.stderr)
            sys.exit(1)
        with extracted, target.open("wb") as out:
            shutil.copyfileobj(extracted, out)
        target.chmod(0o755 if member.mode & 0o111 else 0o644)

if (extract_dir / "manifest.json").is_file():
    bundle_dir = extract_dir
else:
    candidates = [
        entry
        for entry in extract_dir.iterdir()
        if entry.is_dir() and (entry / "manifest.json").is_file()
    ]
    if len(candidates) != 1:
        print("Error: Extracted release archive is missing manifest.json.", file=sys.stderr)
        sys.exit(1)
    bundle_dir = candidates[0]

manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
version = str(manifest.get("version") or "").strip().lstrip("v")
if not version or not VERSION_RE.match(version):
    print(f"Error: Invalid version in manifest.json: {version!r}.", file=sys.stderr)
    sys.exit(1)

wheels = sorted((bundle_dir / "wheel").glob("bud-*.whl")) if (bundle_dir / "wheel").is_dir() else []
if not wheels:
    print("Error: Extracted release archive is missing wheel/bud-*.whl.", file=sys.stderr)
    sys.exit(1)

resolved_home = Path(bud_home_raw).expanduser().resolve()
print(resolved_home)
print(bundle_dir)
print(version)
PY

{
  IFS= read -r RESOLVED_HOME
  IFS= read -r BUNDLE_DIR
  IFS= read -r VERSION_STR
} <"$EXTRACT_INFO"

mkdir -p -m 700 "$RESOLVED_HOME"
mkdir -p -m 700 "$RESOLVED_HOME/releases"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RAND="$("$PYTHON_BIN" -c 'import uuid; print(uuid.uuid4().hex[:6])')"
RELEASE_DIR="$RESOLVED_HOME/releases/v${VERSION_STR}-${STAMP}-${RAND}"
mkdir -m 700 "$RELEASE_DIR"
STAGED_RELEASE_DIR="$RELEASE_DIR"
BUD_HOME_DIR="$RESOLVED_HOME"

echo "Installing Bud v$VERSION_STR into $RESOLVED_HOME..."
"$UV_BIN" venv --no-project --no-config --python "$PYTHON_BIN" "$RELEASE_DIR/venv"

WHEEL_PATH="$(find "$BUNDLE_DIR/wheel" -maxdepth 1 -name 'bud-*.whl' | sort | head -n 1)"
if [ -z "$WHEEL_PATH" ]; then
  echo "Error: Release bundle is missing wheel/bud-*.whl." >&2
  exit 1
fi

PIP_ARGS=(pip install --no-config --python "$RELEASE_DIR/venv/bin/python")
if [ -f "$BUNDLE_DIR/wheel/constraints.txt" ]; then
  PIP_ARGS+=(--constraint "$BUNDLE_DIR/wheel/constraints.txt")
fi
PIP_ARGS+=("$WHEEL_PATH")
"$UV_BIN" "${PIP_ARGS[@]}"

ACTIVATE_INFO="$TMP_DIR/activate_info.txt"
"$RELEASE_DIR/venv/bin/python" - \
  "$BUNDLE_DIR" \
  "$RELEASE_DIR" \
  "$RESOLVED_HOME" \
  "$APP_DIR" \
  "$PORT" \
  "$NO_APP" \
  "$NO_SERVICE" \
  "$FORCE" >"$ACTIVATE_INFO" <<'PY'
import sys
from pathlib import Path

from bud.release.updater import activate_staged_release

bundle_dir = Path(sys.argv[1])
release_dir = Path(sys.argv[2])
bud_home = Path(sys.argv[3])
app_dir = Path(sys.argv[4])
port = int(sys.argv[5])
install_app = sys.argv[6] != "1"
manage_service = sys.argv[7] != "1"
force = sys.argv[8] == "1"

result = activate_staged_release(
    bundle_dir,
    release_dir,
    bud_home,
    app_install_dir=app_dir,
    install_app=install_app,
    manage_service=manage_service,
    port=port,
    force=force,
)
print(result.previous_version or "")
print(result.new_version)
print("1" if result.app_updated else "0")
print("1" if result.service_restarted else "0")
print(result.backup_dir)
PY

STAGED_RELEASE_DIR=""

{
  IFS= read -r PREV_VER
  IFS= read -r NEW_VER
  IFS= read -r APP_UPDATED
  IFS= read -r SERVICE_STARTED
  IFS= read -r BACKUP_DIR
} <"$ACTIVATE_INFO"

UPDATED_RC=""
if [ "$NO_MODIFY_PATH" -eq 0 ]; then
  SHELL_NAME="$(basename "${SHELL:-zsh}")"
  case "$SHELL_NAME" in
    zsh)
      RC_FILE="$HOME/.zshrc"
      PATH_LINE="export PATH=\"$RESOLVED_HOME/bin:\$PATH\""
      LOCAL_BIN_LINE="export PATH=\"$HOME/.local/bin:\$PATH\""
      ;;
    bash)
      if [ -f "$HOME/.bashrc" ]; then
        RC_FILE="$HOME/.bashrc"
      else
        RC_FILE="$HOME/.bash_profile"
      fi
      PATH_LINE="export PATH=\"$RESOLVED_HOME/bin:\$PATH\""
      LOCAL_BIN_LINE="export PATH=\"$HOME/.local/bin:\$PATH\""
      ;;
    fish)
      RC_FILE="$HOME/.config/fish/config.fish"
      PATH_LINE="fish_add_path \"$RESOLVED_HOME/bin\""
      LOCAL_BIN_LINE="fish_add_path \"$HOME/.local/bin\""
      ;;
    *)
      RC_FILE="$HOME/.profile"
      PATH_LINE="export PATH=\"$RESOLVED_HOME/bin:\$PATH\""
      LOCAL_BIN_LINE="export PATH=\"$HOME/.local/bin:\$PATH\""
      ;;
  esac

  mkdir -p "$(dirname "$RC_FILE")"
  TOUCHED_RC=0
  HAS_BUD_BIN=0
  if [ -f "$RC_FILE" ]; then
    if grep -Fq "$RESOLVED_HOME/bin" "$RC_FILE"; then
      HAS_BUD_BIN=1
    elif [ "$RESOLVED_HOME" = "$HOME/.bud" ] && grep -Fq ".bud/bin" "$RC_FILE"; then
      HAS_BUD_BIN=1
    fi
  fi
  if [ "$HAS_BUD_BIN" -eq 0 ]; then
    {
      echo ""
      echo "# bud"
      echo "$PATH_LINE"
    } >>"$RC_FILE"
    TOUCHED_RC=1
  fi

  if [ "$INSTALLED_UV" -eq 1 ]; then
    HAS_LOCAL_BIN=0
    case ":$PATH:" in
      *":$HOME/.local/bin:"*) HAS_LOCAL_BIN=1 ;;
    esac
    if [ -f "$RC_FILE" ] && grep -Fq ".local/bin" "$RC_FILE"; then
      HAS_LOCAL_BIN=1
    fi
    if [ "$HAS_LOCAL_BIN" -eq 0 ]; then
      echo "$LOCAL_BIN_LINE" >>"$RC_FILE"
      TOUCHED_RC=1
    fi
  fi

  if [ "$TOUCHED_RC" -eq 1 ]; then
    UPDATED_RC="$RC_FILE"
  fi
fi

echo ""
if [ -n "$PREV_VER" ]; then
  echo "Updated Bud from v$PREV_VER to v$NEW_VER in $RESOLVED_HOME."
  echo "Backup: $BACKUP_DIR"
else
  echo "Installed Bud v$NEW_VER into $RESOLVED_HOME."
fi
echo "CLI: $RESOLVED_HOME/bin/bud"
if [ "$APP_UPDATED" = "1" ]; then
  echo "Mac app: $APP_DIR/Bud.app"
fi
if [ "$SERVICE_STARTED" = "1" ]; then
  echo "Service: running at http://127.0.0.1:$PORT"
fi
if [ -n "$UPDATED_RC" ]; then
  echo "Added $RESOLVED_HOME/bin to PATH in $UPDATED_RC."
fi

case ":$PATH:" in
  *":$RESOLVED_HOME/bin:"*)
    ;;
  *)
    if [ -n "$UPDATED_RC" ]; then
      echo "Restart your terminal (or run: source $UPDATED_RC), then run:"
    else
      echo "Run with full path or add $RESOLVED_HOME/bin to PATH, then run:"
    fi
    ;;
esac

if [ -n "$PREV_VER" ]; then
  echo "  bud chat"
elif [ "$APP_UPDATED" = "1" ]; then
  echo "Open Bud.app to sign in with ChatGPT or add API keys:"
  echo "  open \"$APP_DIR/Bud.app\""
  echo "Or from your terminal: bud login chatgpt && bud chat"
else
  echo "  bud login chatgpt"
  echo "  bud chat"
fi
echo "Update anytime with: bud update"
