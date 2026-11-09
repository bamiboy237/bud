# bud

> A personal AI teammate you run yourself. Fast, durable, and safe.

Bud is an open-source, local-first personal AI assistant. It integrates directly with your real-world tools—Gmail, Google Calendar, web search, and local files—while keeping you firmly in control through exact, human-in-the-loop write approvals.

---

## Highlights

* **Crash-Resilient Durability:** Built on DBOS and SQLite event logs. If your server or laptop restarts mid-task, Bud recovers its execution state and resumes.
* **Human-in-the-Loop Safety:** Read operations run automatically; state-modifying actions (sending emails, editing files, updating calendar events) pause for explicit user approval.
* **Real-World Connections:** Native integrations with Gmail and Google Calendar via Composio, web research via Exa, and a private local workspace.
* **Progressive Skills:** Capabilities and workflows load on-demand just-in-time, preventing system prompt bloat and keeping token consumption minimal.
* **Full Simulation Mode:** Develop and test with a completely sandboxed synthetic persona and database—zero API keys or live accounts required.

---

## Quickstart

### 1. Install

Requires [uv](https://docs.astral.sh/uv/) and Python >= 3.12:

```bash
git clone https://github.com/bamiboy237/bud.git
cd bud
uv sync
```

### 2. Connect a Model

Sign in with your ChatGPT subscription (no API key needed), or bring your own API keys:

```bash
# Option A: Sign in with ChatGPT subscription
uv run bud login chatgpt

# Option B: Or provide API keys in .env
echo "OPENAI_API_KEY=your-key" >> .env
# (Optional: DEEPSEEK_API_KEY, MUSE_API_KEY, EXA_API_KEY, COMPOSIO_API_KEY)
```

### 3. Start Chatting

Launch the background daemon and interactive terminal client:

```bash
# Terminal 1: Start the server (using safe simulation mode)
uv run bud serve --simulate

# Terminal 2: Attach the interactive client
uv run bud client
```

Or ask a one-off question directly:
```bash
uv run bud ask "What are the core principles of event sourcing?"
```

---

## CLI Reference

| Command | Description |
| :--- | :--- |
| `bud serve` | Start the persistent Bud server daemon |
| `bud serve --simulate` | Start server in safe simulation mode with synthetic data |
| `bud client` | Connect the interactive terminal chat interface |
| `bud ask "<prompt>"` | Send a single prompt through the gateway and print output |
| `bud models` | List available models, active routes, and fallbacks |
| `bud costs` | Inspect subscription token usage and metered API spend |
| `bud eval` | Run regression, safety, and tool-use evaluation benchmarks |
| `bud connect <app>` | Connect an external service (`gmail`, `calendar`) via Composio |

---

## Architecture & Deep Dives

To keep the root documentation focused, deep technical guides live in [`docs/`](docs/):

* **[Architecture & Durability](docs/architecture.md):** Append-only event streams, DBOS crash recovery, and state projection.
* **[Connecting Real Accounts](docs/accounts.md):** Setting up live Gmail and Google Calendar integrations via Composio MCP.
* **[iMessage Integration](docs/photon.md):** Bridging Bud to Apple Messages via Photon v2.
* **[Skills & Tool Extensibility](docs/skills.md):** Writing, testing, and progressively loading custom task skills.
* **[Security & Policy Model](docs/security.md):** Approval claims, memory boundaries, and workspace sandboxing.

---

## License

[MIT](LICENSE)
