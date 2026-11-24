# Security & Approvals

**Core invariant:** reads run automatically; risky writes wait for an explicit, single-use `approval_id` checked against SQLite claim ledgers to prevent double dispatch. Email sends include an immutable preview of recipients, subject, and body.

## Capability boundaries

| Surface | Allowed unasked | Always asks / reviewed | Hard blocks |
| :--- | :--- | :--- | :--- |
| **Workspace** (`~/.bud/workspace`) | Harness reads (`read_file`, `search_files`), `read_document` (25 MiB local AnyDoc, no OCR) | `workspace_create` / `update` auto-allowed in `auto`; ask in `strict` | Symlinks, `..`, abs paths, hidden/credential names (`.env`, keys, `AGENTS.md`), >128 KiB text |
| **Local files** | None | `read_local_file` and `list_local_folder` always ask; no "always allow" | Links, hidden/credential names, `~/.bud`, `~/.ssh`, `~/.gnupg`, `~/Library/Keychains`, `DATA_DIR` (inode-checked) |
| **Mac apps** | Reads (Notes, Reminders, Music, Calendar, Finder, Safari); Preview/TextEdit via `open_in_app` copy | Notes/Reminders/Music changes (reviewed in `auto`); Office/Pages/Numbers/RTF open | Free-form AppleScript, Mail, Messages, Chrome, `.app`/`.command`/`.webloc`/HTML |
| **Shell** (macOS Seatbelt) | Offline `run_local_command`; `run_command` to package hosts | `run_command` / `allow_network` proxy checks new hosts; `run_in_background` asks | Reads of `DATA_DIR`, repo root, `$HOME` outside workspace; Bud env/secrets; `osascript`/`security`/launchd |
| **Chrome** (`OpenCode Browse`, `~/.bud/browse:19989`) | `browser_status`, `snapshot`, `screenshot`, `scroll`, `wait_for`, `handoff`, list/close Bud tabs | `enable_browser` once (no standing grant); clicks/typing/nav reviewed in `auto`; uploads always ask | Arbitrary JS (`execute`/`eval`), settings, network/console capture; disabled via `BROWSER_TOOLS=false` |

Workspace updates require the latest content hash from `read_file` to prevent overwriting concurrent edits. `bud grants` / `bud grants revoke <tool>` manage standing permissions (`DATA_DIR/grants.json`); sends, real account writes, skill installs, `enable_browser`, local files, and `allow_network` never allow standing grants.

## Approval modes & reviewer

Bud runs in `auto` mode by default (`strict` remains available for `bud eval --mode strict`).

- In `auto` mode, the reviewer (OpenAI Decisions API, `gpt-6-luna`) sees only the tool call, target description, your prompt, and your answers, never untrusted email/web results. Any ambiguity, high risk, timeout, or malformed verdict falls back to asking you.
- Real account writes, sends, and skill installs skip the reviewer and always ask you directly.
- Set `APPROVAL_REVIEW=false` to disable API review; reviewed calls then ask you directly (workspace writes still stay allowed unless switched to `strict`). Verdicts audit to `action.decided`.

## Taint guard (content-derived targets)

Before auto-approving any outbound call (sends, account actions, shell/background hosts, browser navigation/uploads, reminders), code extracts and normalizes hosts, IPs, and email addresses (decoding HTML entities, percent-encoding, full-width chars, `x [at] y [dot] z`, and tracking links).

If a target appears in read content (emails, web pages, notes, recalled history, job reports) without appearing in your own messages or previously approved/emailed Gmail recipients, Bud ignores auto mode and requires explicit approval.
