# bud

bud is a personal AI agent you run yourself. It keeps every conversation in an append-only event log, so any number of clients can follow one session live and pick up where they left off after a disconnect or a server crash. Model calls go through a gateway that falls back to another provider on failure and records what each request cost.

bud is under active development. It can chat, call tools, and hold write actions for your approval. By default, bud uses real Gmail and Google Calendar connections through the Composio SDK and MCP (Model Context Protocol). Use `bud serve --simulate` to work with a fictional person's inbox and calendar instead. Live email and event listing, approved email sending, and approval pause/denial have been checked. Draft creation and calendar writes are not yet live-verified.

## Install

You need [uv](https://docs.astral.sh/uv/) and an API key for DeepSeek or OpenAI.

```sh
git clone https://github.com/bamiboy237/bud.git
cd bud
uv sync
```

Create a `.env` file and add your provider key. A provider with no key is skipped. Git ignores `.env`, so your keys stay out of the repository. Real-account mode requires both `COMPOSIO_API_KEY` and `OPENAI_API_KEY`; see [Connect real accounts](#connect-real-accounts).

## Use

Send one prompt:

```sh
uv run bud ask "What is an event log?"
```

Run the server with simulated tools and chat with it. This mode needs a model-provider key but no connected Google accounts:

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

Real-account data crosses both Composio and OpenAI. Connect only accounts whose data you agree to share with these services. Model calls cost money.

1. In [Composio Platform](https://platform.composio.dev/), select your project and copy its project API key from Settings → Project Settings → API Keys. Set `COMPOSIO_API_KEY` to that key in `.env`, and set `OPENAI_API_KEY` too. Do not use a For You / Connect consumer key: it belongs to a separate service and cannot authenticate this SDK flow.
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

## Layout

- `src/bud/gateway/`: the model list, fallback, and cost ledger. Prices are in `models.toml`.
- `src/bud/core/`: the event log, agent runner, server, and terminal client.
- `src/bud/harness/`: the system prompt, tool policy, action gateway, and run limits.
- `src/bud/world/`: the simulated inbox, calendar, contacts, and their tools.
- `src/bud/composio/`: hosted account connections and real tools through SDK MCP.
- `src/bud/evals/`: the task suite and the runner that scores it.
- `src/bud/cli.py`: the `bud` command.

## License

MIT. See [LICENSE](LICENSE).
