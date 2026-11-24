# bud

Local-first personal AI teammate. Connects to Gmail, Google Calendar, web search, local files, Chrome, and iMessage. Reads run automatically; risky writes wait for your approval.

## Daily-driver install (macOS)

Requires `uv` and Python 3.12+ (plus Apple Command Line Tools when building from source):

```sh
git clone https://github.com/bamiboy237/bud.git
cd bud
uv sync --locked
uv run bud login chatgpt
uv run bud install --from-repo . --migrate-from .
```

This installs Bud into `~/.bud/`, builds `/Applications/Bud.app`, starts the `app.bud.server` LaunchAgent on `127.0.0.1:8420`, and keeps your history separate from any repo checkout:

- `~/.bud/config.env`: API keys and settings.
- `~/.bud/data/`: chats, memory, routines, connections, spend ledger, and `auth/owner.token`.
- `~/.bud/workspace/`: Bud's working notes, reports, and chat attachments.
- `~/.bud/backups/`: automatic verified SQLite snapshots taken before every update.

Update later from GitHub Releases (or **Check for Updates...** in the menu bar) or from local `master` without touching your data:

```sh
bud update                   # pull latest verified GitHub release, back up data, swap, restart
bud update --from-repo .     # build and install straight from a local checkout
bud rollback                 # restore latest verified backup if an update ever misbehaves
```

## Development workflow

Daily-driver Bud (port `8420`, `app.bud.companion`, `~/.bud/`) stays isolated from development builds:

```sh
scripts/dev-stack up              # isolated Bud Dev (app.bud.companion.dev), free port, temp data/workspace
scripts/dev-stack up --simulate   # simulated inbox and calendar
scripts/dev-stack down            # stop dev stack and delete temp state
```

Or run the simulated CLI stack in two terminals:

```sh
uv run bud serve --simulate
uv run bud chat
```

Optional Rust terminal UI (`cargo build --release --manifest-path tui/Cargo.toml`): without it, `bud chat` uses the Python client.

## CLI reference

| Command | Purpose |
| :--- | :--- |
| `bud install` | Install standalone runtime, `Bud.app`, and LaunchAgent under `~/.bud/` |
| `bud update` | Back up data, install latest GitHub release (or `--from-repo`), health-check, or roll back |
| `bud rollback` | Restore `~/.bud/data/` from a verified backup and restart |
| `bud service <action>` | Run `install`, `start`, `stop`, `restart`, `status`, `uninstall`, or `logs` |
| `bud backup <action>` | Run `create`, `list`, or `restore` on `~/.bud/backups/` |
| `bud serve [--simulate]` | Start the Bud HTTP/SSE core server (`--host`, `--port`, `--home`) |
| `bud chat` | Open or resume the interactive terminal client (`--new`, `--url`) |
| `bud ask "<prompt>"` | Send one prompt through the model gateway |
| `bud login chatgpt` / `logout` | Sign in or clear local ChatGPT OAuth credentials |
| `bud models` / `bud costs` | List configured models or print daily token spend |
| `bud connect <app>` / `connections` | Link `gmail` or `calendar` via Composio, or check status |
| `bud grants [list\|revoke <tool>]` | List or revoke standing tool permissions |
| `bud desktop ...` | Pair and control remote/local desktop verification workers |
| `bud eval` | Run the offline-world model eval suite (spends real tokens) |

## Docs

- [Setup, accounts, updates, and integrations](docs/setup.md)
- [Architecture, jobs, routines, and skills](docs/architecture.md)
- [Security and approval boundaries](docs/security.md)
- [Testing and CI](docs/testing.md)

## License

[MIT](LICENSE)
