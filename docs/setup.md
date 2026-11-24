# Setup & Integrations

## Standalone install & updates

Bud separates immutable releases from user state under `~/.bud/` (override with `BUD_HOME` or `--home`):

| Path | Contents |
| :--- | :--- |
| `config.env` | Env config and API keys (`OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `COMPOSIO_API_KEY`, `EXA_API_KEY`) |
| `data/` | Event log, DBOS runtime, spend ledger, preferences, connections, `auth/owner.token` |
| `workspace/` | Notes, saved reports, and chat attachments (or `WORKSPACE_DIR`) |
| `releases/` | Versioned venvs and `current` symlink (`bin/bud` links here) |
| `backups/` | Pre-update verified SQLite snapshots with SHA-256 `manifest.json` |

Config precedence: explicit `BUD_HOME` > current repo checkout (`.env` or `data/`) > `~/.bud/`.

### Safe update flow

`bud update` (or **Check for Updates...** in the menu bar):
1. Refuses to swap while a run is actively executing (unless `--force`).
2. Stops the `app.bud.server` LaunchAgent and waits for `live-server.lock` to clear.
3. Takes a consistent SQLite backup into `~/.bud/backups/`.
4. Verifies SHA-256 checksums, swaps `releases/current` and `/Applications/Bud.app`, and starts the service.
5. Polls `GET /runtime` for expected version; automatically restores the prior binary and backup if startup fails.

Create a local signing certificate named `bud companion (local)` in Keychain Access (Certificate Assistant > Create a Certificate > Code Signing) once so macOS Microphone, Contacts, and Automation permissions persist across updates and rebuilds.

### Prod vs Dev isolation

- **Daily driver:** `/Applications/Bud.app` (`app.bud.companion`), `app.bud.server` on `127.0.0.1:8420`, `~/.bud/data/`, `~/.bud/workspace/`, Chrome relay `~/.bud/browse` port `19989`.
- **Dev stack (`scripts/dev-stack up`):** `mac/build/bud-dev.app` (`app.bud.companion.dev`), free port, temporary `data/`, temporary `workspace/`, isolated Chrome relay (`BUD_BROWSE_HOME`, `BUD_BROWSE_PORT`), and no Photon/iMessage.

## Models & connected accounts

- **Models:** run `bud login chatgpt`, or set `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `MUSE_API_KEY`, `MISTRAL_API_KEY` in `~/.bud/config.env` or `.env`. Web search (`search_web` / `read_webpage`) uses `EXA_API_KEY`.
- **Gmail & Calendar (Composio):** set `COMPOSIO_API_KEY`, then run `bud connect gmail` / `bud connect calendar` or trigger `setup_capability` in chat. New connections hot-reload on the next message. Switching or disconnecting an account automatically denies any pending approvals scoped to the old account.

## iMessage (Photon v2)

Bud connects directly via Socket.IO inside `bud serve` (disabled in `--simulate`). Configure in `~/.bud/config.env`:

| Variable | Purpose |
| :--- | :--- |
| `PHOTON_API_KEY` | Project-scoped Photon v2 key |
| `PHOTON_PROJECT_ID` | Photon v2 project ID |
| `PHOTON_OWNER_HANDLE` | Your exact Messages send-from email or E.164 phone |
| `PHOTON_AGENT_HANDLE` | Assigned Photon v2 agent number |

iMessage shares the primary chat session (`bud chat`) and supports `/approve <id>`, `/deny <id>`, `/status`, and `/stop`.

## Paired desktop workers

Workers connect outbound over HTTPS/loopback to verify files (SHA-256) or fill configured upload forms without opening inbound ports:

```sh
bud desktop --url "$BUD_URL" pair "My Mac"                 # prints one-time worker token
bud desktop --url "$BUD_URL" run [--browser-local URL]     # start worker (optional local Browse relay)
bud desktop --url "$BUD_URL" dispatch CHAT FILE_ID NAME DESKTOP_ID [--form form.json]
bud desktop --url "$BUD_URL" browser CHAT JOB_ID approve|verify
bud desktop --url "$BUD_URL" retarget CHAT JOB_ID DESKTOP_ID
bud desktop --url "$BUD_URL" status CHAT JOB_ID
bud desktop --url "$BUD_URL" revoke DESKTOP_ID
```

Browser form specs require `url`, `file_input`, `reference_input`, `submit_button`, and `receipt` IDs on a single multipart HTML form. If submission status drops mid-request, Bud enters `Uncertain` and offers read-only receipt re-checks instead of resubmitting.
