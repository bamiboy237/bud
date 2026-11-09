# bud

bud is a personal AI agent you run yourself. It keeps every conversation in an append-only event log, so multiple clients can follow one session and reconnect after a disconnect. Reply text streams live to connected clients and is not stored piece by piece; the finished reply is saved when the run ends. The server uses native DBOS durable execution to recover accepted work after a crash. Model calls go through a gateway that falls back to another provider on failure and records reported usage and cost.

bud is under active development. It can chat, call tools, remember approved facts across sessions, and hold write actions for your approval. By default, bud uses real Gmail and Google Calendar connections through the Composio SDK and MCP (Model Context Protocol). Use `bud serve --simulate` to work with a fictional person's inbox and calendar instead. Live email and event listing, approved email sending, approval pause/denial, one harmless unsent draft, and one approved Calendar create/time-only move have been checked. Each new write waits for its own approval; these focused checks do not establish broad daily-use reliability.

## Install

You need [uv](https://docs.astral.sh/uv/) and ChatGPT plan sign-in, a model-provider API key, or a configured local model endpoint.

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

### Custom model routes

Set `MODELS_FILE` to a TOML registry to replace the bundled API registry. Each
entry describes a route; entries in a tier are tried in order. For example,
save this as `data/models.toml`, replacing the model name with the ID served
by your endpoint:

```toml
[[models]]
provider = "local"
name = "your-tool-capable-model"
tier = "fast"
billing = "local"
base_url = "http://127.0.0.1:11434/v1"
protocol = "chat"
supports_tools = true
context_window = 32768
max_tokens = 4096
```

Then configure `.env`:

```dotenv
MODELS_FILE=data/models.toml
DEFAULT_MODEL=local:your-tool-capable-model
MODEL_FALLBACK=false
```

Start normally with `uv run bud serve`, or use `uv run bud serve --simulate`
for fictional tools. The endpoint must already be running. Its capabilities
are declared by the operator, not inferred from the model's name. The core
refuses a route declared without tool support before executing a task.
`protocol` is `chat` or `responses`; use the protocol your server implements.
`context_window`, `max_tokens` and `reasoning_effort` are optional. Native
provider profiles handle their supported protocol behavior.

For a custom metered API, use a distinct provider name, HTTPS, `billing = "api"`
and explicit `input`, `output` and `cached` prices per million tokens.
Set its secret separately through `MODEL_API_KEYS`, a JSON object mapping
provider names to keys, in `.env` or secure environment injection. Endpoint
URLs cannot contain credentials or query strings. Custom endpoints never
inherit `OPENAI_API_KEY`. A custom endpoint can use real-account tools only
when its registry entry explicitly sets `allow_accounts = true`.

`DEFAULT_MODEL` pins the default just like `--model`; neither falls back.
Otherwise `MODEL_FALLBACK` controls the tier's availability fallback, enabled
by default for compatibility. A local route never falls through to a different
endpoint. Local billing records token usage and zero metered API dollars,
not hardware or electricity costs. Subscription usage remains separate.
`uv run bud models --cached` shows saved/configured routes without refreshing
ChatGPT credentials, so it can be used while the server owns their lease.

## Task skills

Bud includes research, attention-briefing, meeting-preparation, message-writing
and document-writing methods. The model first sees short descriptions, then
loads relevant instructions with Pydantic's `load_capability` tool. Ask for
the work normally; there is no separate startup command.

Each task records its offered skill names and SHA-256 content versions in
`run.started`. Tool events show which methods were loaded. Approval
continuations retain the original versions, even after a method is disabled
or updated. A modified or missing saved package stops that task instead of
silently substituting instructions.

For a multi-part goal, Bud can combine methods within the same agent loop:
research can supply evidence for a document, or meeting preparation can supply
context for a message. It uses the methods relevant to the request rather than
a fixed workflow. Findings and source references carry into the deliverable;
corrections revise the retained artifact with a fresh approval.

Session snapshots expose `recent_runs[].loaded_skills`, mapping methods
successfully loaded in that execution to their pinned versions, and
`recent_runs[].artifacts`, listing confirmed workspace saves with path,
revision and operation. Proposed, denied and refused writes are not saved
artifacts. Methods loaded earlier can remain available through the native
conversation history without another load call. Older run records have empty
summary fields; their original tool events remain available.

Writing methods use saved preferences and approved examples. Corrections apply
to the current draft; ask Bud to remember a preference to propose its existing
approved-memory update. You can inspect, change or forget those preferences.
The methods are general and work with the available sources and document tools.
They do not add Slack, binary document conversion or a shell by themselves.

### Find and install a skill

Ask Bud to find a skill for a task. `find_skills` searches skills.sh;
`prepare_skill` fetches a selected public GitHub skill at an exact commit and
shows its instructions, source, version and supporting files.
`read_prepared_skill` lets Bud inspect those files before recommending it.
`install_skill` then waits for the normal exact user approval. It activates
only that prepared version. Updating requires another preview and approval.
`list_skills` shows installed versions; `disable_skill` removes a method from
future task catalogs while retaining it for existing tasks.

On first use, discovery/preview downloads a checksum-pinned Node 22.20 runtime
under the scope's private `skills/tooling` directory and runs the released
`skills@1.7.6` CLI. Its dependency tree is installed with the packaged npm lock
file and integrity hashes. No global Node or coding-agent installation is changed.
The subprocess gets an isolated home, no owner API keys or Git credentials,
telemetry disabled, and npm install scripts disabled. These operations need
network access and currently support macOS/Linux x64 and arm64.

Imported methods cannot replace bundled methods. Packages are limited to
64 files, 64 KiB per file and 512 KiB total; links and special files are
refused. Catalog descriptions are limited to 1,024 characters and instruction
bodies to 32 KiB. Manual-only skills are not supported. Names and versions are
validated, and source files are hashed before
activation and use. Instructions and supporting files are retained in the
private account/world scope. Scripts can be inspected but are not executed.
Skill instructions never grant tool permissions.

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
uv run bud chat               # terminal 2, resumes the primary conversation
uv run bud chat <session-id>  # terminal 3, attaches to the same session
uv run bud chat --new         # explicitly starts a separate conversation
```

`uv run bud serve` provides public web research and tools for your active connected accounts. It needs model access. Composio and Google connections are optional for public research. See [Connect real accounts](#connect-real-accounts) to add Gmail or Calendar.

Ask bud to check the inbox or move a meeting in `bud chat`. Each proposed write displays its tool, arguments, and exact approval ID. Answer `/approve <approval-id>` or `/deny <approval-id>` for that action. Bare approval commands never approve a batch; `/status` shows the current proposals. See what you've spent, inspect the simulated world, or restore it:

```sh
uv run bud costs
uv run bud world show
uv run bud world reset
```

Run the eval suite, which spends a few cents on model calls:

```sh
uv run bud eval
```

The server listens on `127.0.0.1:8420` and requires single-owner bearer authentication on every HTTP endpoint, including events, approvals, and account controls. Keep it on your own machine: authentication does not add TLS, remote hosting, or permission to expose it publicly. `--simulate` is the only mode switch. Missing model access stops normal startup. Account-free public research needs no Composio key; retained account references require the key to verify their binding. Bud never silently switches to simulation.

### Owner access

Run server and chat from the same working directory with the same `DATA_DIR`. Bud automatically creates one owner credential in `DATA_DIR/auth/owner.token` and the local CLI loads it; no token copying or printing is needed. It persists across restarts and account changes. The auth directory must be owned with mode `0700`, and the token file with mode `0600`. Unsafe or corrupt credentials fail closed instead of being replaced automatically.

A separate trusted client can use a strong `OWNER_TOKEN` supplied through secure environment injection. Never put it in URLs, query strings, command arguments, examples, logs, or shared files. The CLI refuses remote plaintext HTTP, credential-bearing URLs, proxies, and redirects. HTTPS still requires an appropriate trusted host; no deployment is established by these access checks.

### One conversation across interfaces

The server owns the conversation; an interface is not a separate agent or memory. `bud chat` and the server's iMessage connection use one persistent primary session in the current account scope. Leaving, switching interfaces, or restarting does not create a fresh primary. `bud chat --new` and `POST /sessions` still create explicit separate conversations; they do not replace the primary or merge old threads.

An authenticated client discovers or creates the primary with `POST /sessions/primary`. It can then submit to `/sessions/<id>/messages` with a stable message ID and origin metadata, for example:

```json
{
  "text": "What time did we choose?",
  "message_id": "example-message-1",
  "origin": {"channel": "web", "client_id": "browser-1"}
}
```

`web` is an API origin label, not a shipped web frontend. Origin describes where a reply belongs; it is not identity, authority, or a replacement for owner authentication and account-scope checks. Clients should pin `X-Bud-Scope` from the authenticated `/runtime` handshake.

All accepted text joins the selected session's authoritative history, independent of origin. Origin survives queue edits, dispatch, completion and restart. The iMessage adapter sends model replies and approval requests only when their origin matches that exact iMessage route. Web/API/terminal replies are not pushed into Messages, and old unattributed events are not guessed or backfilled there. The terminal remains a full-history viewer; a future web UI can likewise render the canonical history even though the native iMessage transcript contains only messages delivered through it.

An approval decision is still tied to an exact pending action. If it is explicitly answered from another interface, the continued run replies to that decision's interface; permission is not inferred from origin metadata or a context-free yes. Existing strict approval and dispatch-claim rules remain unchanged.

During server startup, configured iMessage adopts its existing conversation as primary when none is set, before accepting external clients. Adoption selects the existing session without copying history. If a different primary already exists, messaging reports failure rather than merging histories or silently moving pending approvals; the authenticated core remains available for inspection. Account generations keep separate primaries; context and approvals never follow a changed account binding automatically.

Work performed inside a conversation remains in its tool/action history. This is not yet model-facing search over old threads, routines, or past outgoing email; older-context retrieval is separate work. Keeping history does not guarantee perfect model recall or that all history fits in each request.

## Durable server and recovery

`bud serve` uses Pydantic AI's native `DBOSDurability` integration with a local SQLite journal, `runtime.db`, beside the session database. Each session runs one execution at a time. Messages received while bud is busy enter a persistent queue in arrival order. New conversational input makes unapproved proposals stale rather than freezing the conversation: bud closes those permissions and considers the new request. Corrected writes need fresh approval. Already granted permissions are not recalled by an ordinary follow-up; use Stop when you want active work stopped.

The queue holds up to 20 waiting messages. Waiting messages do not enter the conversation until their turn starts. At dispatch, the server saves the messages and their `run.queued` inputs in one transaction, using the latest completed conversation. iMessage gets a fixed one-second collection window when idle. Adjacent messages from the same iMessage route received within two seconds can become one model turn, with each original message and ID retained separately. Different interfaces, older messages and inputs exceeding the normal text budget are not combined. Restart recovery preserves waiting messages and can drain them without waiting for the in-memory timer. A completed approval batch continues before the next queued message.

DBOS records model and tool steps. During recovery, completed step receipts supply saved results and skip execution. A step without a completed receipt may execute again. This is not exactly-once delivery to Google or any other remote service. Real writes also use a persistent approval dispatch claim, described in [Connect real accounts](#connect-real-accounts).

The server saves Pydantic AI's `Conversation`, including state needed to continue deferred tools. Pending approvals and recorded decisions survive a restart. Once the pending decisions are complete, the runner queues an approval continuation using the saved conversation.

If recovery restarts an unfinished model request, `text.reset` replaces partial text for that request, not the entire turn. Session snapshots discard that request's old text blocks. The terminal cannot erase printed text, so it marks the partial response as discarded and prints its replacement.

Recovery refuses queued executions when their saved code version or run configuration differs from the current server. The version includes packaged code and configuration files, plus the DBOS, Pydantic AI, Harness, and Monty versions. Changed work fails before tools run rather than replaying across an upgrade. Interrupted runs from before durable execution have no recoverable inputs or journal and are marked failed.

Earlier live recovery after `SIGKILL` was verified with DeepSeek and synthetic tools. Successful ChatGPT execution under DBOS and recovery of real Google writes have not been verified. The server remains local with single-owner authentication. Durable execution does not add scheduled routines or an always-on deployment.

## Queue and Stop controls

Use these commands in `bud chat`:

| Command                     | Result                                                                          |
| --------------------------- | ------------------------------------------------------------------------------- |
| `/status`                   | Show the current state, queued message IDs, approvals, and recent runs.         |
| `/pause`                    | Hold waiting messages. The current run continues.                               |
| `/resume`                   | Allow queued messages to run. This does not restart a stopped run.              |
| `/edit <message-id> <text>` | Replace a message that is still waiting.                                        |
| `/cancel <message-id>`      | Remove a message that is still waiting.                                         |
| `/stop`                     | Pause the queue, close pending approvals, and request Stop for the current run. |

Stop prevents new tool permissions. A tool authorized before Stop may still finish, even if its network request starts later. Stop does not undo an action. The state remains `stopping` until the execution exits. If an approved tool has no recorded result, the run becomes `needs_review` and the queue stays paused. Check the external result before retrying the action. After a failed run, the queue also pauses instead of starting all remaining messages.

Session snapshots include `status`, `queue_paused`, `queued_messages`, and `recent_runs`. HTTP clients can edit or cancel `/sessions/{id}/queue/{message_id}`, post to `/sessions/{id}/queue/pause` or `/sessions/{id}/queue/resume`, and post to `/sessions/{id}/stop`. Stop returns HTTP 202. Message submissions return HTTP 429 when the waiting queue is full. Supply a stable `message_id` to deduplicate delivery retries. New IDs accept 1 to 128 ASCII letters, digits, hyphens, or underscores. Omit the ID to generate one.

## Spend and execution limits

The server uses a spend brake of **$0.50 per run** and **$5 per UTC day**, shared across sessions in the same account or simulated-world spend database. Spend counters persist across restarts. Each execution has a limit of **25 model requests** and **20 tool calls**. An approval continuation is a new execution with fresh per-run limits. The runner offsets the saved conversation's cumulative usage to enforce these request and tool limits per execution, not across the whole conversation. Recovery of the same execution keeps its run ID.

Code Mode's nested reads count toward the tool limit, as does `run_code`
itself. Bud supplies the remaining budget before every model request. With
three or fewer tool calls left, or one model request left, it disables further
tools and asks the model to deliver verified findings and identify unfinished
work or missing evidence. A partial answer is not confirmation that an
unfinished action succeeded. The hard execution limits remain enforced.

The spend brake checks recorded usage before the next model request. A request that crosses the dollar limit can finish and take spend above the threshold. Provider usage that bud never receives remains unknown, not proof of zero cost. These are metered API spend limits, not a guaranteed provider bill ceiling. ChatGPT subscription responses add no API dollars, but request and tool limits still apply.

The spend counters are separate from the request ledger shown by `bud costs`. Gateway-only `bud ask` does not use these runner limits. Eval attempts use temporary spend stores, not the server's persistent daily counter.

## Synthetic calendar scheduling

In `bud serve --simulate`, ask to find a meeting, move it to a free time, and verify it after approval. The fictional calendar uses `America/Chicago`, weekday 09:00–17:00 working hours, and protected Focus Block events. These are seed preferences, not inferred rules for your real calendar.

New calendar tool inputs require an explicit UTC offset consistent with their IANA timezone. For UTC inputs, specify `timezone="UTC"`. Missing offsets, nonexistent local times, inconsistent offsets, and unclear daylight-saving occurrences must be resolved rather than guessed. Existing stored naive timestamps are interpreted in the seed zone without rewriting them; ambiguous or invalid legacy rows fail closed.

Listing and conflict checks compare instants in UTC. An approved create or move checks conflicts and commits its replay key in the same SQLite transaction. Overlapping bookings are refused, Focus Blocks cannot move, and moves preserve titles and attendees. Use `read_event` after approval to inspect the saved event. Replaying the same approved operation returns `applied=false` and the event's current state; it does not undo a later approved move. Reusing that key for a different operation is refused.

The working-hour restrictions and transactional booking guarantees above are synthetic. A separately approved live test created one harmless event, moved that same event, and read it back in a fresh conversation. It used no attendees or conference link; nonvolatile event metadata stayed unchanged. Normal calendar reminders were left intact. Rich attendee/conference preservation and uncertain external-write outcomes were checked with constructed data, not live guest invitations or process kills. Do not treat simulated booking guarantees as atomic conflict prevention in Google Calendar.

The native Calendar path resolves an explicit calendar ID and timezone, checks all pages of one-calendar availability up to four pages/1,000 events, and refuses incomplete coverage. It supports ordinary, nonrecurring timed events of at most 24 hours; recurring, special, all-day conversions and attendee edits are outside scope. Creates require an explicit title, times, timezone, attendee list and notification policy, with Meet creation disabled and no automatic self-attendee insertion.

Moves use time-only `GOOGLECALENDAR_PATCH_EVENT`, not replacement UPDATE. Bud adds a local `expected_etag` argument from the event read before approval, compares it with a fresh pre-write read, and removes it before the native call. It is not a Google `If-Match` precondition: another change can still race the write. After a confirmed write, Bud saves its identifiers before read-back, then verifies times and preserved nonvolatile metadata—including attendee RSVP details, reminders and conference data. A mismatch or unknown outcome requires inspection, not another automatic write. Real working hours, protected-time rules and a specific live mutation still need your choices and permission.

## Public web research

In normal `bud serve` mode, ask for research with sources, for example:

```text
Find the official Pydantic AI documentation for human tool approval. Read it and explain how denial works. Include source links.
```

Bud's `search_web` and `read_webpage` tools call [Exa's hosted MCP service](https://docs.exa.ai/reference/exa-mcp) directly. They do not need Composio or connected accounts. Keyless access has a free rate limit and requires a direct IPv4 connection. Set optional `EXA_API_KEY` in `.env` to use your own Exa plan; it is sent only in an authentication header. A failed keyless call never switches to paid access. Search excerpts are leads, not proof that bud read the page. Retrieved content can be cached, so a successful read does not prove freshness.

Search returns up to 10 sources. Page reads accept 1 to 3 public HTTP or HTTPS URLs per call, with 20,000 characters per page by default and a 50,000 maximum. Bud refuses URL credentials, private literal IP addresses, and local hostnames. Exa owns fetching, redirects, DNS resolution, and extraction. These are remote public-source tools, not your browser, local network, or signed-in accounts.

Large results use the existing stored-result readers and read-only Code Mode. Web content remains untrusted data. The guidance tells bud to keep private account details out of queries unless you explicitly request sharing them. This guidance is not a complete defense against prompt injection. Authenticated Exa usage is separate from the model costs shown by `bud costs` and is not covered by its model-dollar brake.

`--simulate` keeps the fictional inbox and calendar. It does not add public web access.

## Text workspace

Ask bud to save a research brief as a `.md` or `.txt` file. It requests approval before creating or updating a file. In a new conversation, ask it to list or search the workspace and read the saved brief. Workspace reads also work in read-only Code Mode.

The dedicated `workspace/` directory sits beside the session database, separately for simulation and each account mapping. Bud does not get access to your home directory or course notes. Paths must be visible and relative. Traversal, symlinks, hard links, special files, and credential-like names are refused. Content must be UTF-8 text of at most 128 KiB. These path checks do not detect secrets within ordinary text. Put only material approved for the agent in this directory.

Creates never overwrite an existing file. Updates need the SHA-256 revision returned by a recent read. A conflicting edit requires a new read and approval. Writes publish the complete file atomically and claim the approval before dispatch, so recovery cannot reuse that approval. An interrupted write needs inspection rather than an automatic retry. Keep `workspace-dispatches.jsonl` with the session state.

List and search inspect one directory at a time, at most 500 entries, and mark incomplete results. Listing returns at most 100 safe entries; literal text search returns at most 50 matches. Reads return at most 32,000 characters and report truncation. Local editors must not edit the same file during an approved update. This is a scoped tool boundary, not isolation from hostile local processes.

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

Composio manages hosted OAuth, so you do not need your own Google Cloud console setup. Managed OAuth availability depends on your project. The CLI commands remain available; account links can also be requested directly in `bud chat`:

```text
/connect gmail
/connections
/refresh
```

Use `/connect calendar` for Calendar. These are explicit user controls, not model tools. The hosted link appears only in the immediate authenticated response/chat output, not model history, memory, session events, or connection-status output. Simulation refuses account controls without contacting a provider.

After sign-in, `/refresh` verifies metadata and reports whether restart is required. Stop and restart the **server process**, then start a **new conversation**. This is the initial supported tool-refresh boundary, not hot reload: tools and DBOS are never reconstructed in the running process. Reads/Stop/denial remain available after a binding changes, but new messages, positive approvals, queue resume, and account dispatch are fenced until restart.

Each explicit connection attempt retires the previous local generation, even when reconnecting the same remote account ID. Pending and accepted references are separate; only verified ACTIVE references enter a new tool scope. Changing owner, generation, or account mapping creates a new storage scope. Old conversations, memory, approvals, journals, and dispatch claims remain in their original directories; they are never copied into the new scope. A root live-server lock prevents concurrent live processes across generations. Remote status failures block new account dispatch. A check cannot recall a request already in flight, and does not prove continuous remote authorization between observations.

3. Start normal mode and open a chat in another terminal. Only active connections add account tools. Public web research works without either Google connection:

```sh
uv run bud serve  # terminal 1, public web and connected accounts
uv run bud chat   # terminal 2
```

Reply drafts take a single recipient and plain-text input. Bud requires a thread ID, omits the subject to preserve Gmail threading, and disables signature insertion. Composio stores HTML even with `is_html=false` and can append quoted original history. The creation path reads the returned draft ID back, checks its draft status, thread and single recipient, and verifies the new reply text separately from recognized quoted history. A live harmless draft and a fresh-conversation read confirmed that quoted history was included; nothing was sent.

New-reply verification accepts CRLF/LF normalization, HTML line breaks, and one HTML entity-decoding pass. It preserves intentional whitespace and trailing newlines. It recognizes only the observed final top-level `gmail_quote` container, not arbitrary hidden content or a substring anywhere in the body. Results distinguish the new reply match, quoted history, and exact whole-body match. Unsupported MIME, multipart bodies, or unfamiliar HTML remain unverified. This is bounded verification of a known connector shape, not a general HTML/MIME parser or proof of the quote's provenance. If creation succeeds but verification is unavailable or mismatched, inspect the existing draft instead of creating another.

In a fresh conversation, bud can list draft metadata with `GMAIL_LIST_DRAFTS`, then read a selected ID with `GMAIL_GET_DRAFT`. Draft listing has no query filter. Bud must match candidates from returned pages and report incomplete searches. Simulation has separate bounded `list_drafts` and `read_draft` tools. Simulated draft creation now requires approval and records its replay key atomically with the draft.

Every real-account write, including draft creation, waits for `/approve <approval-id>` or `/deny <approval-id>`. Before dispatch, bud saves a claim for that approval in `dispatches.db`. Confirmed draft identifiers are saved there before awaiting read-back, so cancellation does not discard an already observed ID; cancellation itself still propagates. A completed DBOS step receipt skips another execution during recovery. If a write has a claim but no confirmed result, the claim blocks another dispatch under the same approval. This is not a guarantee of exactly-once delivery by Google. Inspect the account before you request a new action or give a new approval. The original action may already have completed.

With the default `DATA_DIR`, simulated conversations use `data/bud.db`, the durable journal uses `data/runtime.db`, and persistent spend counters use `data/world-spend.db`. Real-account state stays in `data/composio/sessions/<binding-hash>/`, with separate `bud.db`, `runtime.db`, `spend.db`, and `dispatches.db` files. The hash includes the local owner, connection generation, and accepted account mapping. Legacy account-only scope directories are preserved, not automatically resumed. The request cost ledger remains in `data/bud.db` for both modes. Git ignores `data/`.

## iMessage through Photon v2

Bud uses Photon's official Python v2 client and Socket.IO events. The Python core
owns the conversation, tools and approvals; Photon carries plain-text messages.
There is no separate agent, Node runtime or connection to your Mac's Messages database.

Use the [Photon v2 dashboard](https://app.beta.photon.codes/), not the separate
Spectrum dashboard. Register your exact Messages send-from email or phone number
and use the shared number assigned to that identity. Supply these privately in
the server's environment or ignored `.env`:

| Setting | Value |
| --- | --- |
| `PHOTON_API_KEY` | Project-scoped v2 key beginning `pho_sk_` |
| `PHOTON_PROJECT_ID` | v2 project ID beginning `pho_prj_` |
| `PHOTON_OWNER_HANDLE` | Your exact Messages send-from email or E.164 number |
| `PHOTON_AGENT_HANDLE` | The different number assigned to that sender in v2 |

A Mac sending from an Apple-account email can use v2's email-identity assignment;
an iPhone is not required. Spectrum credentials and assignments are different and
are not used. Bud does not create assignments, buy plans or provision numbers.
Partial configuration fails before account recovery.

The normal command is unchanged:

```sh
uv run bud serve
```

There is no separate messaging command or installation extra. Normal dependency
installation includes the v2 client. The server starts and stops messaging with
its own lifetime.

Only the configured owner's iMessage direct conversation is accepted. Unknown
senders, groups, outbound echoes, SMS/RCS and unsupported content do not start
agent work. Both the human identity and assigned agent number must match the
configuration; a number from another Photon project is not interchangeable.

Chat normally: short requests produce one useful response, not accepted/working
announcements. New input can clarify or revise an unapproved proposal without
getting stuck behind it. Corrected writes always require fresh permission.
Conversation and tool history stay in the same primary session used by `bud chat`.
Other interfaces' answers are not pushed into Messages.

A file preview looks like:

```text
Create notes.txt?

The requested file contents.

/approve <approval-id>
/deny <approval-id>
```

Send the exact command shown in the preview. Bare "yes", emoji and native Reply
do not grant permission: the v2 messages inspected during live use did not expose
the reply target needed to bind those answers safely. A correction replaces the
unapproved proposal and requires a fresh command. Stale or already-decided IDs
cannot authorize another action.

Long previews retain the complete details, with decision commands kept together
after them. Superseded previews are checked before enqueueing and again before
dispatch. The terminal retains its detailed controls.

Say `stop`, `pause`, `resume` or `status` for conversation controls. Stop acts
immediately, including during the burst window. It cannot undo already authorized
work. `/connect gmail|calendar`, `/connections` and `/refresh` remain available;
sign-in links are immediate owner output, not model input or durable transport text.

Normal results use ordinary text bubbles. Emoji are text, not native Tapbacks.
Effects, rich cards, typing/read signals and reply-bound permissions are not part
of this text-only integration.

`bud serve --simulate` always disables real messaging, even when credentials are
present. No messaging provider connection is needed for
the fictional world. Use it for experiments rather than real-account mutations.

Routes, incoming digests, replay cursors and pre-dispatch send claims stay under
`DATA_DIR/photon/`, scoped to the configured identities and core account binding.
Returning from Spectrum reuses the original v2 state, not a backup of old core
databases. Unused Spectrum metadata is left untouched in its separate directory;
it is never read as v2 state. Pending legacy native-reply previews are retired
from the transport; `/status` shows current exact commands.

Socket.IO reconnects using the last completed provider cursor. Incoming duplicates
reuse stable core IDs; uncertain controls and outgoing sends are not repeated.
Accepted, delivered and unknown are distinct, and the SDK's automatic send retries
are disabled. Delivery claims survive restart; dispatched bodies are cleared.
Provider replay retention and uninterrupted service are not guaranteed. The
authenticated `/runtime` endpoint reports messaging state separately from the core.

Sources: [v2 setup](https://photon.codes/docs/beta/get-started/imessage),
[live events](https://photon.codes/docs/beta/websocket/session),
and [replay cursors](https://photon.codes/docs/beta/websocket/cursors).

## Memory

Ask bud to remember a fact or preference in `bud chat`, for example:

```text
Remember that I prefer status updates in three bullet points.
```

bud requests approval for `write_memory`. After you approve, a new session can use that preference. Ask it to update or forget an entry when the preference changes. Every memory write and deletion waits for approval; reads and searches do not.

The notebook uses Pydantic AI Harness Memory with SQLite persistence. A bounded main summary enters the model context, and longer notes are read on demand. Search matches text; it is not semantic email or document search. Do not store passwords or API keys in memory.

Real memory stays with its immutable account binding in `data/composio/sessions/<binding-hash>/memory.db`. Simulated memory uses `data/world-memory.db`, and every eval attempt gets a temporary store. `bud world reset` restores the fake inbox and calendar, not the memory notebook. Forgetting a note does not erase old conversation logs.

Memory content enters the model context, so local storage does not mean local-only processing. Real mode sends it to the selected model provider. Simulation must contain synthetic data only. Save, deny, update, forget, and fresh-session recall after server restarts have been verified in simulation with live model calls.

## Large tool results and cleanup

Model-visible read results and `run_code` answers of 10,000 characters or more use Pydantic AI Harness `ToolOutputLimits`. Bud stores their full serialized content in the session database's `tool_results` table and sends the model a preview of about 1,000 characters plus a handle. Reads inside Code Mode keep their full values for processing. Real-account stores remain separated by account mapping. Write receipts are not reduced by this capability.

Inside `run_code`, the model can use `read_tool_result` to read or search bounded line ranges. That native reader caps a response at 1,000 lines and 50,000 characters. For oversized single-line JSON, `read_tool_result_chars` returns at most 4,000 characters from an offset or from the end. Both readers pass through the action gateway. An unavailable handle produces a clear response rather than a file path or an automatic source-tool call.

Temporary results become eligible for cleanup 24 hours after their last write or read. Set `TOOL_RESULT_RETENTION_HOURS` in the environment or `.env` to change this positive, finite number of hours. Successful reads renew the lifetime. The server sweeps on startup and every 60 seconds while running. It protects every result belonging to a session with queued work, a running execution, or an approval pause. Once that protection ends, already-old results can expire on the next sweep. Approvals do not yet expire automatically, so an unresolved approval can protect results indefinitely.

The store checks session state and removes expired rows in one SQLite transaction, so cleanup cannot race an already-accepted message or a read renewal. Saved memory and the source Gmail messages or Calendar events are not deleted. Row deletion is not secure erasure: audit events, DBOS receipts, SQLite recovery files, and backups have separate retention concerns. This cleanup does not claim to remove all copies of a result.

If storage fails, the native capability warns and falls back to marked truncation at 4,000 characters. Local storage, readers, approval protection, and startup/periodic cleanup have been exercised with synthetic data.

## Read-only Code Mode

The server exposes selected read tools as Python functions inside Pydantic AI Harness Code Mode. The model calls `run_code` to filter, group, and calculate over their results. Sends, drafts, Calendar changes, and memory changes remain ordinary tools with their existing approval rules. The action gateway records `run_code` and its nested reads.

Monty runs the Python subset in a local sandbox process. No host directories, OS handler, or remote worker are configured. Sandbox code cannot directly read host files, environment variables, or network sockets. It can call only the selected read functions. It does not provide arbitrary Python packages such as pandas or NumPy.

Each snippet has a 10-second execution limit, a 128 MiB heap limit, and a limit of 2,000 suspensions. It can call at most 12 nested tools. Nested calls also count toward the execution's 20-tool limit, together with `run_code` itself. Eager execution and speculative calls are off.

`load_tool_result(handle=...)` loads a saved result into the sandbox, decoding JSON when possible. SQLite refuses payloads above 8 MB before returning the bytes to the host. Reads renew the result's existing lifetime. Missing or expired results fail explicitly. Large source queries need smaller batches rather than silent truncation.

Full nested results stay available to code, while only the snippet's returned value and printed output enter model context. Oversized snippet answers still use the stored-result preview described above. Full nested results remain in audit metadata and recovery records. Code Mode is not a data-erasure feature.

Variables survive between snippets within one agent run. Under DBOS recovery, code runs again using completed read-step receipts to rebuild that state. Variables do not carry into a new message or approval-continuation run. Saved result handles, approved memory, and conversation history have separate lifetimes.

A live synthetic job processed 1,000 records in two snippets and recovered after `SIGKILL` with identical conversation and tool usage. Recovery issued no new model request. Local checks exercised file, environment, network, write, time, memory, tool-count, and output limits. Real-account Code Mode has not been live-verified. Simulated `read_thread` no longer marks messages as read.

## Long conversations

Bud prepares a summary in the background while the current request continues. Preparation starts at 80% of the compaction threshold. The threshold is 65% of the model's resolved context window, with a 64,000-token fallback when the framework cannot resolve the window. These are initial estimates, not a provider-enforced request-size guarantee.

The summary records the goal, current constraints, decisions, confirmed work, relevant failures, source identifiers, and next action. Native Pydantic AI `SummarizingCompaction` retains an unchanged recent suffix of at least 12,000 estimated content tokens, extended where necessary to keep tool calls with their results. A prepared result can replace only the exact history snapshot it summarized. Bud preserves every newer message and rejects preparation if the history, instructions, tool definitions, model route, or request settings change.

When compaction becomes necessary, bud uses a valid prepared result or waits for it to finish. A missing, failed, or unsuitable preparation uses blocking summarization. If the recent content and tool definitions still do not fit, the run stops with a request to reduce the input instead of repeatedly summarizing it.

The DBOS selection step saves the exact replacement history, including newer messages. Recovery reuses a completed selection receipt. Unused preparations live only in memory and can be lost on restart without losing the saved conversation. The cache holds at most 64 preparations. Access removes preparations older than five minutes, capacity pressure evicts the oldest preparation, and shutdown cancels remaining preparation tasks. There is no separate idle cleanup timer.

Summary calls use the same tracked model route as the conversation, with no tools. They share the originating execution's dollar budget and the daily budget, even if preparation finishes after that execution. Each preparation reserves one request from its originating execution. Summary token usage is recorded separately in the request ledger and `context.ready` events; the conversation's main-agent token totals do not include those auxiliary calls. An unused preparation still consumes model allowance and may cost money.

Compaction changes working history, not the original event log, approval records, or approved memory. Summaries cannot grant approval or create saved user memory. Older records remain stored, but a model-facing search tool for the full conversation archive is not implemented yet.

## Layout

- `src/bud/gateway/`: the model list, fallback, and cost ledger. Prices are in `models.toml`.
- `src/bud/core/`: the event log, durable agent runner, recovery version checks, server, and terminal client.
- `src/bud/harness/`: the system prompt, tool policy, action gateway, approved memory, background compaction, read-only Code Mode, temporary results, and persistent limits.
- `src/bud/world/`: the simulated inbox, calendar, contacts, and their tools.
- `src/bud/composio/`: hosted account connections and real tools through SDK MCP.
- `src/bud/evals/`: the task suite and the runner that scores it.
- `src/bud/cli.py`: the `bud` command.

## License

MIT. See [LICENSE](LICENSE).
