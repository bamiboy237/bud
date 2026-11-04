# bud

bud is a personal AI agent you run yourself. It keeps every conversation in an append-only event log, so any number of clients can follow one session live and pick up where they left off after a disconnect or a server crash. Model calls go through a gateway that falls back to another provider on failure and records what each request cost.

bud is under active development. It can chat, call tools, remember approved facts across sessions, and hold write actions for your approval. By default, bud uses real Gmail and Google Calendar connections through the Composio SDK and MCP (Model Context Protocol). Use `bud serve --simulate` to work with a fictional person's inbox and calendar instead. Live email and event listing, approved email sending, and approval pause/denial have been checked. Draft creation and calendar writes are not yet live-verified.

## Install

You need [uv](https://docs.astral.sh/uv/) and either ChatGPT plan sign-in or a model-provider API key.

```sh
git clone https://github.com/bamiboy237/bud.git
cd bud
uv sync
```

For API access, set `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, or `MUSE_API_KEY` in `.env`. Providers without a key are skipped. Git ignores `.env`. Real-account tools also require `COMPOSIO_API_KEY`; see [Connect real accounts](#connect-real-accounts).

## Sign in and select a model

To use your ChatGPT plan, run:

```sh
uv run bud login chatgpt
uv run bud models
```

The login command opens your browser and waits up to five minutes for consent. Add `--no-browser` to print a sign-in link instead. Pydantic AI handles authorization and token refresh. Bud reads your account's model catalog and selects its first listed model as the default.

After sign-in, the default route uses ChatGPT, with Muse Wonder as backup when `MUSE_API_KEY` is set. Fallback applies to availability errors before a response starts. A stream that fails after output starts ends the run rather than replaying its partial output. Without ChatGPT sign-in, API models follow the tier order in `src/bud/gateway/models.toml`.

Use a model ID from `bud models` to pin a request or server to that model. Pinned models do not fall back:

```sh
uv run bud ask "Explain an event log." --model deepseek:deepseek-flash
uv run bud serve --simulate --model deepseek:deepseek-flash
```

DeepSeek remains available for simulated work. Real-account mode permits ChatGPT, OpenAI API, and Meta. `--simulate` selects tools, not inference. `--tier` selects the API tier; the default ChatGPT model serves both tiers after sign-in.

`bud costs` shows subscription token usage separately from metered API dollars. Dollar budgets limit API spend; request and tool limits still apply to ChatGPT runs. The ChatGPT subscription fee is not allocated to individual requests.

Credentials live in `data/chatgpt/credentials.json`, with owner-only directory and file permissions. The installation ID and client registration survive logout. Only one bud process can use these credentials at a time because refresh tokens rotate. Stop a ChatGPT server before another ChatGPT command, including `bud models`, login, or logout.

To remove the local credentials, run:

```sh
uv run bud logout chatgpt
```

## Use

Send one prompt:

```sh
uv run bud ask "What is an event log?"
```

Run the server with simulated tools and chat with it. This mode needs ChatGPT sign-in or a provider key, but no connected Google accounts:

```sh
uv run bud serve --simulate   # terminal 1, simulated tools
uv run bud chat               # terminal 2, prints a session id
uv run bud chat <session-id>  # terminal 3, attaches to the same session
```

`uv run bud serve` uses real accounts. Complete [Connect real accounts](#connect-real-accounts) before starting it.

Ask bud to check the inbox or move a meeting in `bud chat`. Writes wait for you to answer `/approve` or `/deny`. See what you've spent, inspect the simulated world, or restore it:

```sh
uv run bud costs
uv run bud world show
uv run bud world reset
```

Run the eval suite, which spends a few cents on model calls:

```sh
uv run bud eval
```

The server listens on `127.0.0.1:8420` and has no authentication, so keep it on your own machine. `--simulate` is the only mode switch. The old `--backend` flag and `BACKEND` setting are no longer used. Missing credentials or connections stop real-account startup; bud never silently switches to simulation.

## Connect real accounts

Real-account data goes through Composio and the selected model provider. The default signed-in route uses ChatGPT and can use Meta as backup. API-key routes incur metered charges.

1. In [Composio Platform](https://platform.composio.dev/), select your project and copy its project API key from Settings → Project Settings → API Keys. Set `COMPOSIO_API_KEY` to that key in `.env`. Sign in to ChatGPT or configure `OPENAI_API_KEY` or `MUSE_API_KEY` for inference. Do not use a For You / Connect consumer key: it belongs to a separate service and cannot authenticate this SDK flow.
2. Request a hosted sign-in URL for each service:

```sh
uv run bud connect gmail
uv run bud connect calendar
```

Each command prints a URL and opens your browser. Add `--no-browser` to print the URL without opening it. Sign in on the hosted page, then run:

```sh
uv run bud connections
```

The connect commands do not wait or poll. `bud connections` reads only connection metadata, not mailbox or calendar data. It shows `gmail` and `googlecalendar` statuses; missing connections show `NOT_CONNECTED`.

Composio manages the hosted OAuth sign-in flow, so you do not need your own Google Cloud console setup. Managed OAuth availability depends on your Composio project. These commands are the current developer setup; in-chat connection links are not implemented yet.

3. After both connections are active, start real-account mode and open a chat in another terminal:

```sh
uv run bud serve  # terminal 1, real accounts
uv run bud chat   # terminal 2
```

Every real-account write, including draft creation, waits for `/approve` or `/deny`. The local approval record allows one dispatch per approval. This is not a guarantee of exactly-once delivery by the remote service. If a write fails with an uncertain result, inspect the account before you give a new approval. The action may already have completed.

World conversations stay in `data/bud.db`. Real-account conversations are separate, in `data/composio/sessions/<account-hash>/bud.db`. The cost ledger remains in `data/bud.db`. These paths use the default `DATA_DIR`, and git ignores `data/`.

## Memory

Ask bud to remember a fact or preference in `bud chat`, for example:

```text
Remember that I prefer status updates in three bullet points.
```

bud requests approval for `write_memory`. After you approve, a new session can use that preference. Ask it to update or forget an entry when the preference changes. Every memory write and deletion waits for approval; reads and searches do not.

The notebook uses Pydantic AI Harness Memory with SQLite persistence. A bounded main summary enters the model context, and longer notes are read on demand. Search matches text; it is not semantic email or document search. Do not store passwords or API keys in memory.

Real memory stays with its account mapping in `data/composio/sessions/<account-hash>/memory.db`. Simulated memory uses `data/world-memory.db`, and every eval attempt gets a temporary store. `bud world reset` restores the fake inbox and calendar, not the memory notebook. Forgetting a note does not erase old conversation logs.

Memory content enters the model context, so local storage does not mean local-only processing. Real mode sends it to the selected model provider. Simulation must contain synthetic data only. Save, deny, update, forget, and fresh-session recall after server restarts have been verified in simulation with live model calls.

## Layout

- `src/bud/gateway/`: the model list, fallback, and cost ledger. Prices are in `models.toml`.
- `src/bud/core/`: the event log, agent runner, server, and terminal client.
- `src/bud/harness/`: the system prompt, tool policy, action gateway, memory configuration, and run limits.
- `src/bud/world/`: the simulated inbox, calendar, contacts, and their tools.
- `src/bud/composio/`: hosted account connections and real tools through SDK MCP.
- `src/bud/evals/`: the task suite and the runner that scores it.
- `src/bud/cli.py`: the `bud` command.

## License

MIT. See [LICENSE](LICENSE).
