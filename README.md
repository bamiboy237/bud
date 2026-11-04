# bud

bud is a personal AI agent you run yourself. It keeps every conversation in an append-only event log, so multiple clients can follow one session and reconnect after a disconnect. The server uses native DBOS durable execution to recover accepted work after a crash. Model calls go through a gateway that falls back to another provider on failure and records reported usage and cost.

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

`bud costs` shows subscription token usage separately from metered API dollars. The ChatGPT subscription fee is not allocated to individual requests. The server's [spend and execution limits](#spend-and-execution-limits) apply separately from the provider's subscription allowance.

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

## Durable server and recovery

`bud serve` uses Pydantic AI's native `DBOSDurability` integration with a local SQLite journal, `runtime.db`, beside the session database. The server saves each accepted message and its `run.queued` inputs in one transaction before launching work. Restart recovery can launch accepted work even if the process stopped before its DBOS workflow started. Each session permits one active execution and rejects new messages while work or an approval is pending.

DBOS records model and tool steps. During recovery, completed step receipts supply saved results and skip execution. A step without a completed receipt may execute again. This is not exactly-once delivery to Google or any other remote service. Real writes also use a persistent approval dispatch claim, described in [Connect real accounts](#connect-real-accounts).

The server saves Pydantic AI's `Conversation`, including state needed to continue deferred tools. Pending approvals and recorded decisions survive a restart. Once the pending decisions are complete, the runner queues an approval continuation using the saved conversation.

If recovery restarts an unfinished model request, `text.reset` replaces partial text for that request, not the entire turn. Session snapshots discard that request's old text blocks. The terminal cannot erase printed text, so it marks the partial response as discarded and prints its replacement.

Recovery refuses queued executions when their saved code version or run configuration differs from the current server. The version includes packaged code and configuration files, plus the DBOS, Pydantic AI, and Harness versions. Changed work fails before tools run rather than replaying across an upgrade. Interrupted runs from before durable execution have no recoverable inputs or journal and are marked failed.

Earlier live recovery after `SIGKILL` was verified with DeepSeek and synthetic tools. Successful ChatGPT execution under DBOS and recovery of real Google writes have not been verified. The server remains local and unauthenticated. Durable execution does not add scheduling or an always-on deployment.

## Spend and execution limits

The server uses a spend brake of **$0.50 per run** and **$5 per UTC day**, shared across sessions in the same account or simulated-world spend database. Spend counters persist across restarts. Each execution has a limit of **25 model requests** and **20 tool calls**. An approval continuation is a new execution with fresh per-run limits. The runner offsets the saved conversation's cumulative usage to enforce these request and tool limits per execution, not across the whole conversation. Recovery of the same execution keeps its run ID.

The spend brake checks recorded usage before the next model request. A request that crosses the dollar limit can finish and take spend above the threshold. Provider usage that bud never receives remains unknown, not proof of zero cost. These are metered API spend limits, not a guaranteed provider bill ceiling. ChatGPT subscription responses add no API dollars, but request and tool limits still apply.

The spend counters are separate from the request ledger shown by `bud costs`. Gateway-only `bud ask` does not use these runner limits. Eval attempts use temporary spend stores, not the server's persistent daily counter.

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

Every real-account write, including draft creation, waits for `/approve` or `/deny`. Before dispatch, bud saves a claim for that approval in `dispatches.db`. A completed DBOS step receipt skips another execution during recovery. If a write has a claim but no confirmed result, the claim blocks another dispatch under the same approval. This is not a guarantee of exactly-once delivery by Google. Inspect the account before you request a new action or give a new approval. The original action may already have completed.

With the default `DATA_DIR`, simulated conversations use `data/bud.db`, the durable journal uses `data/runtime.db`, and persistent spend counters use `data/world-spend.db`. Real-account state stays in `data/composio/sessions/<account-hash>/`, with separate `bud.db`, `runtime.db`, `spend.db`, and `dispatches.db` files. The request cost ledger remains in `data/bud.db` for both modes. Git ignores `data/`.

## Memory

Ask bud to remember a fact or preference in `bud chat`, for example:

```text
Remember that I prefer status updates in three bullet points.
```

bud requests approval for `write_memory`. After you approve, a new session can use that preference. Ask it to update or forget an entry when the preference changes. Every memory write and deletion waits for approval; reads and searches do not.

The notebook uses Pydantic AI Harness Memory with SQLite persistence. A bounded main summary enters the model context, and longer notes are read on demand. Search matches text; it is not semantic email or document search. Do not store passwords or API keys in memory.

Real memory stays with its account mapping in `data/composio/sessions/<account-hash>/memory.db`. Simulated memory uses `data/world-memory.db`, and every eval attempt gets a temporary store. `bud world reset` restores the fake inbox and calendar, not the memory notebook. Forgetting a note does not erase old conversation logs.

Memory content enters the model context, so local storage does not mean local-only processing. Real mode sends it to the selected model provider. Simulation must contain synthetic data only. Save, deny, update, forget, and fresh-session recall after server restarts have been verified in simulation with live model calls.

## Large tool results and cleanup

Read-only tool results of 10,000 characters or more use Pydantic AI Harness `ToolOutputLimits`. Bud stores their full serialized content in the session database's `tool_results` table and sends the model a preview of about 1,000 characters plus a handle. Real-account stores remain separated by account mapping. Write receipts are not reduced by this capability.

The model can use `read_tool_result` to read or search bounded line ranges. That native reader caps a response at 1,000 lines and 50,000 characters. For oversized single-line JSON, `read_tool_result_chars` returns at most 4,000 characters from an offset or from the end. Both readers pass through the action gateway. An unavailable handle produces a clear response rather than a file path or an automatic source-tool call.

Temporary results become eligible for cleanup 24 hours after their last write or read. Set `TOOL_RESULT_RETENTION_HOURS` in the environment or `.env` to change this positive, finite number of hours. Successful reads renew the lifetime. The server sweeps on startup and every 60 seconds while running. It protects every result belonging to a session with queued work, a running execution, or an approval pause. Once that protection ends, already-old results can expire on the next sweep. Approvals do not yet expire automatically, so an unresolved approval can protect results indefinitely.

The store checks session state and removes expired rows in one SQLite transaction, so cleanup cannot race an already-accepted message or a read renewal. Saved memory and the source Gmail messages or Calendar events are not deleted. Row deletion is not secure erasure: audit events, DBOS receipts, SQLite recovery files, and backups have separate retention concerns. This cleanup does not claim to remove all copies of a result.

If storage fails, the native capability warns and falls back to marked truncation at 4,000 characters. This slice bounds new read results; it does not yet summarize long conversations, impose a whole-request token budget, or add Code Mode. Local storage, readers, approval protection, and startup/periodic cleanup have been exercised with synthetic data. The new flow has not completed a live-model walkthrough.

## Layout

- `src/bud/gateway/`: the model list, fallback, and cost ledger. Prices are in `models.toml`.
- `src/bud/core/`: the event log, durable agent runner, recovery version checks, server, and terminal client.
- `src/bud/harness/`: the system prompt, tool policy, action gateway, memory configuration, temporary results, persistent spend counters, and execution limits.
- `src/bud/world/`: the simulated inbox, calendar, contacts, and their tools.
- `src/bud/composio/`: hosted account connections and real tools through SDK MCP.
- `src/bud/evals/`: the task suite and the runner that scores it.
- `src/bud/cli.py`: the `bud` command.

## License

MIT. See [LICENSE](LICENSE).
