# Setup & Integrations

## Standalone install & updates

Install or update safely on macOS with one command:

```sh
curl -fsSL https://raw.githubusercontent.com/bamiboy237/bud/master/scripts/install.sh | bash
```

Flags: `--version <tag>`, `--home <dir>`, `--app-dir <dir>`, `--no-app`, `--no-service`, `--no-modify-path`. The installer downloads over HTTPS only, verifies `SHA256SUMS.txt` before extracting, avoids `sudo`, and delegates activation to Bud's non-destructive updater with automatic backup and rollback.

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

- **In `Bud.app`:** select **Add a model** in the conversation. To change your model credentials later, open **Settings → Models**.
- **In the CLI:** run `bud login chatgpt`, or set `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `MUSE_API_KEY`, `MISTRAL_API_KEY` in `~/.bud/config.env` or `.env`. Web search (`search_web` / `read_webpage`) uses `EXA_API_KEY`.
- **Gmail & Calendar (Composio):** set `COMPOSIO_API_KEY`, then run `bud connect gmail` / `bud connect calendar` or trigger `setup_capability` in chat. New connections hot-reload on the next message. Switching or disconnecting an account automatically denies any pending approvals scoped to the old account.

## Connect to your Bud server

Open **Settings → General → Connect to a server**. Enter the server's HTTPS address and owner access token. Bud checks the connection before switching and stores the token in Keychain. To open the on-device conversation again, select **Use This Mac**.

### Run Bud on a Linux server

Use a Linux VM, such as an Azure VM, with Docker Compose. Point a DNS name at it and allow HTTPS on port 443 and certificate validation on port 80. Run this from the Bud checkout on the VM:

```sh
BUD_DOMAIN=bud.example.com docker compose up -d
```

Caddy issues the TLS certificate. Only Caddy publishes ports. Bud stores its database, workspace, settings, and access token in the `bud-data` volume. To connect the Mac app, get the token in a private terminal on the VM:

```sh
BUD_DOMAIN=bud.example.com docker compose exec bud cat /home/bud/.bud/data/auth/owner.token
```

Enter the address and token in Mac Settings. Add a model API key in **Models**. This server does not need the Mac app to stay open.

### Host Bud on Azure

The [`ops/azure/main.bicep`](../ops/azure/main.bicep) template creates an Ubuntu
VM, a DNS name, and a firewall. It starts Bud and Caddy on reboot and keeps their
volumes on the managed OS disk.

1. Use a public Linux container image for this Bud build, pinned by digest.
2. Compile the template with `az bicep build --file ops/azure/main.bicep`.
3. In Azure, open **Deploy a custom template** and load the generated `main.json`.
   Enter your SSH public key, your SSH source IP with `/32`, and the image reference.
4. Leave `receiveMove` on to move your existing Bud. Turn it off to start with a fresh Bud.
5. Review Azure's resources and costs before you deploy. Use the deployment's `budURL`
   and `sshCommand` outputs to connect and retrieve the access token:

   ```sh
   cd /opt/bud
   sudo docker compose exec bud cat /home/bud/.bud/data/auth/owner.token
   ```

### Move Bud to a server

Start a fresh Linux server in move mode. For a server you host with Compose:

```sh
BUD_DOMAIN=bud.example.com BUD_RECEIVE_MOVE=true docker compose up -d
```

Then move your local Bud:

1. Open **Settings → General → Move Bud to a server**.
2. Select the local Bud folder. If its workspace is separate, select that folder too.
3. Enter the address and access token for the server you prepared.
4. Select **Move Bud**. Bud stops the local service, stages its data on the server, and switches the app after Bud starts on the server.

Chats, memory, workspace files, skills, routines, portable model keys, and
supported sign-ins move together. Finish pending work and approvals before the
move. Pair browsers and desktops again on the new server.

If the connection drops, reopen **Move Bud** and select **Continue move**.
The local runtime stays stopped. Select **Cancel move** to start it on this Mac
again. The server must confirm that it did not activate the move before Bud can
resume on this Mac.

### Pair this Mac for app actions

When Bud runs on a Linux server, open **Settings → Computers → Add a computer →
Pair this Mac**. Keep the Bud Mac app open. The local assistant service can stay off.
Bud can find and create notes, list and add reminders, and control Music. macOS asks
for Automation permission the first time Bud uses each app. Bud can also open a
workspace document on that Mac. App changes and document opens require approval
in chat. Select **Remove** to block future actions from this server. This does
not stop an action the Mac already accepted.

### Add a Daytona browser

Put the Bud server and the Daytona computer on the same private network. For Docker Compose, enable the Browse port on the server's private IPv4 address:

```sh
BUD_DOMAIN=bud.example.com BUD_BROWSE_PEER_IP=100.64.1.2 \
  docker compose -f compose.yaml -f ops/compose.browse.yaml up -d
```

Open **Settings → Computers → Add a computer → Daytona**. In the Daytona desktop, open Chromium with the Browse extension and choose **Pair with a code**. Enter the relay address and pairing code from Settings. Name that browser **Daytona** in the extension to distinguish it from Chrome in Bud's Browser panel.

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
