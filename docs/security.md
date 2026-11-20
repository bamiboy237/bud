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
- `workspace_create` and `workspace_update` run without asking in auto mode, the default,
  like the sandboxed shell, which can already write the same files. Strict mode asks for
  them. Each write runs once: its claim is saved before the file changes, so a recovered run
  never writes it twice.
- `workspace_update` needs the content hash from the latest `read_file`. A file that changed
  since that read is not overwritten.

These checks are guardrails, not isolation. Only put material in the workspace that Bud may
read.

## Your files outside the workspace

Bud can read one of your files, or list one of your folders, when you name it:
`read_local_file` and `list_local_folder`. These rules hold in every approval mode:

- **Every read asks you.** There is no "always allow", and the reviewer never answers for
  you. A path Bud refuses anyway (below) is refused at once, without a card.
- **The card names the real file.** Bud resolves `~` and every link first, so a link named
  `notes.txt` that points at something else shows where it really goes. Bud then opens that
  exact path, one folder at a time, without following links, so what you approve is what
  it reads.
- **Some places stay closed even with approval:** Bud's data folder and the folder holding
  its `.env`, `~/.bud`, `~/.ssh`, `~/.gnupg`, `~/Library/Keychains`, the workspace (Bud reads
  it with `read_file`), hidden files, and names that look like credentials (`.env`, keys,
  tokens, passwords).
- **It only reads.** Text comes back in pieces of 16,000 characters. PDF, Word, Excel,
  PowerPoint, OpenDocument, RTF, EPUB, and CSV come back as Markdown, converted on your Mac.
  Files over 25 MB are refused.
- **macOS may ask too.** Reading Documents, Desktop, or Downloads can need permission in
  System Settings, Privacy & Security, Files and Folders.

## Shell commands

On macOS, Bud runs shell commands in its workspace with `run_command`, and long ones with
`run_in_background`, without asking. Each command runs under the macOS sandbox (Seatbelt)
with these limits:

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
Chrome, with your tabs and sign-ins. Bud runs its own Browse relay, with its home folder in
`~/.bud/browse` and port 19989, and asks it for Browse's small typed tools instead of one
tool that runs any JavaScript. The extension connects to one relay at a time: pairing it with
Bud's relay disconnects any other agent that used Browse on this Mac.

- **You turn it on once.** Bud calls `enable_browser`, and the approval card explains what it
  allows. While the browser is off it always asks, and offers no "always allow". While it is
  already on, the call runs without a card. Until you turn it on, the browser tools are
  hidden and Browse does not start. Ask Bud to turn the browser off at any time
  (`disable_browser`).
- **Reading runs unasked.** `browser_status`, `browser_snapshot`, `browser_screenshot`,
  `browser_scroll`, `browser_wait_for`, `browser_handoff`, and listing tabs with
  `browser_tabs` change nothing.
- **Closing tabs runs unasked.** A `browser_tabs` close that names its tabs, and nothing
  else, needs no approval. Bud's relay holds only Bud's tabs, and Browse hands a tab you lent
  back to you instead of closing it.
- **Each step is checked.** Opening a page or a tab, clicking, typing, choosing, dragging,
  and answering a dialog go to the approval reviewer in auto mode, and to you in strict mode.
  The card shows the step, the site its tab really shows, and the reviewer's reason.
- **Uploading a file always asks you.**
- **Never offered:** running JavaScript (`execute`, `eval`), Browse's settings, `webmcp`,
  recording, and network or console capture. A tool a later Browse adds is refused until Bud
  names it.
- **The reviewer sees the real page.** Bud looks up which site a tab shows, without its query
  or fragment, and never passes on an address the model wrote as a looked-up fact.
- **No standing approvals.** An approved step is not remembered, even for the session: the
  same click on a different page is checked again.
- **Limits of the check.** A page Bud reads can try to trick it, and a step written to look
  harmless can pass. Browser steps are not covered by the rule that sends always ask: in
  Gmail or Google Calendar, a click can send. The approval card says so before you turn the
  browser on.

The Mac app ships `browse` 0.1.5, pinned by SHA-512 and signed with the app, and copies it to
`~/.bud/bin` on first launch. The pin covers only that copy: from there Browse updates itself
with its own updater. A `browse` you installed (`~/.local/bin` or on `PATH`) is used when
Bud's copy is missing. To connect Chrome, ask Bud: a setup card links the extension and shows
a one-time pairing code and the relay address. Set `BROWSER_TOOLS=false` to remove the
browser entirely.

## Approval reviewer

In auto mode, the reviewer (OpenAI's Decisions API) judges each reviewable call against your
request. Only a low or medium risk call you clearly asked for runs unasked. Bud asks you when
the call is high or critical risk, weakly supported by your request, raises any concern, or
may pay, send, delete, handle a password or code, or hide its real effect. A one-in-five
chance of any of those is enough. The reviewer sees a call's full arguments up to 32,000
characters; a longer call asks you, and a shell command that long cannot reach a new site.

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
