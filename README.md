# bud

bud is a personal AI agent you run yourself. It keeps every conversation in an append-only event log, so any number of clients can follow one session live and pick up where they left off after a disconnect or a server crash. Model calls go through a gateway that falls back to another provider on failure and records what each request cost.

bud is under active development. Today it can chat, call tools, and hold write actions for your approval. Its tools work on a simulated inbox and calendar for a fictional person, and bud does not connect to real email, calendars, or other accounts yet.

## Install

You need [uv](https://docs.astral.sh/uv/) and an API key for DeepSeek or OpenAI.

```sh
git clone https://github.com/bamiboy237/bud.git
cd bud
uv sync
cp .env.example .env
```

Add your keys to `.env`. A provider with no key is skipped. Git ignores `.env`, so your keys stay out of the repository.

## Use

Send one prompt:

```sh
uv run bud ask "What is an event log?"
```

Run the server and chat with it:

```sh
uv run bud serve               # terminal 1
uv run bud chat                # terminal 2, prints a session id
uv run bud chat <session-id>   # terminal 3, attaches to the same session
```

Ask bud to check the inbox or move a meeting in `bud chat`. Writes wait for you to answer `/approve` or `/deny`. See what you've spent, inspect the simulated world, or restore it:

```sh
uv run bud costs
uv run bud world show
uv run bud world reset
```

The server listens on `127.0.0.1:8420` and has no authentication, so keep it on your own machine. Conversations and cost records are stored in `data/bud.db`, which git ignores.

## Layout

- `src/bud/gateway/`: the model list, fallback, and cost ledger. Prices are in `models.toml`.
- `src/bud/core/`: the event log, agent runner, server, and terminal client.
- `src/bud/harness/`: the system prompt, tool policy, action gateway, and run limits.
- `src/bud/world/`: the simulated inbox, calendar, contacts, and their tools.
- `src/bud/cli.py`: the `bud` command.

## License

MIT. See [LICENSE](LICENSE).
