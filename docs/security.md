# Security & Approval Model

Bud operates under a strict principle of least surprise: **Read is automatic; Write requires explicit user consent.**

## The Approval Flow
1. When Bud plans a side-effect (e.g. creating a workspace file, modifying a calendar event, or sending a draft), it registers an exact `approval_id`.
2. The execution pauses, surfacing the proposal to the user (in terminal, web, or iMessage).
3. The user grants approval via exact reference (`/approve <id>`) or denies it (`/deny <id>`).
4. Replayed or duplicated dispatches verify pre-conditions against SQLite claim records to prevent double-execution.

An email action also carries a readable preview of the original message: subject, recipients, and the full body, captured from Bud's own records when the action is held. The preview is presentation only. The decision still names one exact approval ID, and the tool arguments do not change. A preview that cannot be read marks itself unavailable and never blocks the decision. Previews apply to new approvals; older pending approvals keep their original shape.

## Workspace files

Bud reads and writes text files in one workspace folder, `~/.bud/workspace`. Set
`WORKSPACE_DIR` to use another folder. Simulate mode uses `DATA_DIR/workspace` instead.

- Reads use the harness `FileSystem` tools (`read_file`, `list_directory`, `search_files`,
  `find_files`, `file_info`) over a read-only workspace. They run without approval.
- `read_document` reads PDF, Word, Excel, PowerPoint, OpenDocument, RTF, EPUB, and CSV
  files up to 25 MB as Markdown, with the same path rules. It converts on this machine with
  [AnyDoc](https://github.com/firecrawl/anydoc) and never sends a document out: scanned
  pages, which would need hosted OCR, are refused. It runs without approval.
- Workspace file content reaches the model as data with no authority, like a tool result.
- Hidden paths and names that look like credentials are refused for reads and writes. For
  example, `.env`, `.git`, `api-key.txt`, `secrets.md`, `id_rsa`, `cert.pem`, and `AGENTS.md`.
- Paths that leave the folder, through `..`, an absolute path, or a symlink, are refused.
- `workspace_create` and `workspace_update` need approval. Each approved write runs once:
  its claim is saved before the file changes, so a recovered run never writes it twice.
- `workspace_update` needs the content hash from the latest `read_file`. A file that changed
  since that read is not overwritten.

These checks are guardrails, not isolation. Only put material in the workspace that Bud may
read.

## Shell commands

On macOS, Bud runs shell commands in its workspace with `run_command`, without asking. Each
command runs under the macOS sandbox (Seatbelt) with these limits:

- It writes only inside the workspace and the system temp folder.
- It cannot read your home folder, Bud's data folder, or the folder Bud runs from (which
  holds `.env`), except the workspace and the toolchains it needs (`~/.local/share/uv`,
  `~/.local/bin`, `~/.cargo`, `~/.rustup`).
- It gets a minimal environment: no API keys, tokens, or other settings from Bud.
- It cannot start programs outside the sandbox: no LaunchServices (`open`, or a copy of it),
  no launchd jobs, no `osascript` or `security`, and no Apple events to other apps.
- Its time limit is 120 seconds by default, 600 at most. When it runs out, or the command is
  cancelled, its whole process group is stopped.
- Python packages go into the workspace's own `.venv`.

The sandbox blocks all network access except a local proxy that runs for each command.
Package registries and code hosts (PyPI, npm, crates.io, GitHub, and others) pass. For any
other site, the proxy asks the approval reviewer, with the command and your request. A site
the reviewer allows is remembered until Bud restarts. A site it does not allow is blocked and
listed in the result. Bud can then call `allow_network`, which asks you each time: it offers no
"always allow". A site you allow is kept in `network-grants.json` in Bud's state folder. Plain
HTTP carries one request per connection, so a reused connection cannot reach an unchecked site.

After a crash, recovery returns a command's recorded output instead of running it again.
These limits are guardrails, not a virtual machine: a command runs as you, with your tools.

## Browser

Bud uses [OpenCode Browse](https://opencode.ai/product/browse) (MIT) to work in your own
Chrome, with your tabs and sign-ins. Browse's tools are `browser_status`, `browser_tabs`, and
`browser_execute`, which runs JavaScript in a tab.

- **You turn it on once.** Bud calls `enable_browser`, and the approval card explains what it
  allows. It always asks and offers no "always allow". Until then the browser tools are
  hidden. Ask Bud to turn the browser off at any time (`disable_browser`).
- **Each step is checked.** `browser_status` runs unasked. Every `browser_tabs` and
  `browser_execute` call goes to the approval reviewer. A step it judges safe and clearly
  requested runs. Anything else, such as paying, sending, deleting, or typing a password,
  asks you with the reviewer's reason. If the reviewer cannot answer, Bud asks you.
- **The reviewer sees the real page.** Bud looks up the address of the tab a script drives
  and shows it to the reviewer, so a script cannot hide where it acts.
- **No partial checks.** The reviewer sees at most 4,000 characters of a call. A longer call
  asks you instead.
- **Limits of the check.** A page Bud reads can try to trick it, and a step written to look
  harmless can pass. The approval card says so before you turn the browser on.
- A tool a later Browse version adds is refused until Bud names it.

The Mac app ships `browse` 0.1.5, pinned by SHA-512 and signed with the app, and copies it to
`~/.bud/bin` on first launch. From there Browse updates itself with its own updater. A
`browse` you installed (`~/.local/bin` or on `PATH`) is used when Bud's copy is missing. To
connect Chrome, ask Bud: a setup card links the extension and shows a one-time pairing code.
Set `BROWSER_TOOLS=false` to remove the browser entirely.

## Standing permissions

An approval card offers **Always allow** for a tool that may be granted. Choosing it applies the approval and, only when the approval actually applied, records a standing permission for that tool. A denied decision, a duplicate, or a decision the runner could not apply records nothing. Later calls of the same tool run without asking, across restarts, until you revoke them.

Hard rules stay in code. Sending email, real Gmail and Calendar writes, and installing or disabling skills can never be granted, and their cards do not offer the button. Revoke a permission with `bud grants revoke <tool>`; `bud grants` lists them. Grants live in `DATA_DIR/grants.json`, outside the event log.

## Approval modes
Switch with `/mode`. There are two, and `auto` is the default.

- `strict`: every write asks, as described above.
- `auto`: each write to Bud's own workspace, memory, and the simulated world is reviewed per call. The reviewer (OpenAI's Decisions API, `gpt-6-luna` by default) rates the call's risk and how clearly you authorized it. A call that is judged safe and clearly requested runs and is journaled; anything else asks you. A timeout, refusal, or malformed answer asks you, never allows.

Hard rules live in code and hold in both modes: sending email, real Gmail and Calendar writes, and installing or disabling skills always ask, and never reach the reviewer.

The reviewer sees only the tool call, a description of its target looked up from Bud's own records, your request for this run, and your answers to Bud's questions in this turn. It never sees emails, web pages, or tool results, which is where prompt injection arrives. When you deny a call, Bud is told the reviewer's concern so it can try a safer way. If you approve a held call, an identical retry in the same session does not ask again.

Auto mode sends the tool call and your request to OpenAI. Set `APPROVAL_REVIEW=false` to keep that off the API; auto mode then asks about every write, like strict. Each verdict, with its risk, authorization, and prompt version, is recorded in the `action.decided` event.
