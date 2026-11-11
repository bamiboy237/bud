# bud

> A personal AI teammate you run yourself. Fast, durable, and safe.

Bud is an open-source, local-first personal AI assistant. It connects to Gmail,
Google Calendar, web search, and a private local workspace. You approve each
proposed write before it runs.

---

## Highlights

* **Crash recovery:** DBOS and SQLite retain execution state across restarts.
* **Write approvals:** Reads run automatically. Changes pause for your exact approval.
* **Connected tools:** Gmail and Google Calendar use Composio; public web research uses Exa.
* **Progressive skills:** Bud loads task instructions when it needs them.
* **Simulation mode:** Experiment with synthetic accounts and files using your model access.
* **Independent jobs:** Keep chatting while jobs work or wait for approval.

---

## Quickstart

### 1. Install

Requires [uv](https://docs.astral.sh/uv/) and Python >= 3.12:

```bash
git clone https://github.com/bamiboy237/bud.git
cd bud
uv sync
```

The terminal client is a Rust program. Build it once with [Rust](https://rustup.rs):

```bash
cargo build --release --manifest-path tui/Cargo.toml
```

Without it, `bud chat` falls back to a plain streaming client.

### 2. Connect a model

Sign in with your ChatGPT subscription:

```bash
uv run bud login chatgpt
```

Or set `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, or `MUSE_API_KEY` privately in your
environment or ignored `.env` file. Simulation also needs model access.

### 3. Start chatting

Start the server and interactive terminal client in separate terminals:

```bash
# Terminal 1: Use synthetic accounts and files
uv run bud serve --simulate

# Terminal 2: Resume the primary conversation
uv run bud chat
```

Or ask a one-off question directly:

```bash
uv run bud ask "What are the core principles of event sourcing?"
```

### Jobs and approvals

Quick questions stay in the chat. Longer work runs as a separate job with its
own history and approvals. Ask another question while a job waits, or name the
job to revise it.

Each proposal shows exact `/approve <reference>` and `/deny <reference>`
commands. A correction replaces the affected job's proposal with a fresh
reference. `/status` shows jobs and pending proposals. `/stop` stops the
conversation and its open jobs.

See [Jobs in the conversation](docs/jobs.md) for an example and API details.

---

## CLI reference

| Command | Description |
| :--- | :--- |
| `bud serve` | Start the persistent Bud server |
| `bud serve --simulate` | Start with synthetic accounts and files |
| `bud chat` | Open the interactive terminal conversation |
| `bud ask "<prompt>"` | Send one prompt through the model gateway |
| `bud models` | List available models |
| `bud costs` | Inspect subscription token usage and metered API spend |
| `bud eval` | Run evaluations against the simulated world |
| `bud connect <app>` | Connect Gmail or Calendar through Composio |

---

## Guides

* [Architecture and durability](docs/architecture.md)
* [Jobs in the conversation](docs/jobs.md)
* [Connect real accounts](docs/accounts.md)
* [iMessage through Photon v2](docs/photon.md)
* [Skills and tool extensibility](docs/skills.md)
* [Security and approvals](docs/security.md)

---

## License

[MIT](LICENSE)
