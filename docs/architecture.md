# Architecture & Durability

## Core engine

1. **Append-only event log (`src/bud/core/log.py`):** Every user prompt, tool proposal, decision, and result is stored sequentially in SQLite (`bud.db`). Clients stream SSE events and resume deterministically by sequence number.
2. **Durable execution (`src/bud/core/runner.py`):** Agent runs execute under DBOS (`runtime.db`). Crashes or restarts recover unfinished runs without re-dispatching recorded tool claims.
3. **Upgrade fencing (`src/bud/core/runtime.py`):** `replay_version()` fingerprints packaged code and framework versions. After an upgrade or config change, pending approvals from an older build raise `StaleApproval` (`409`) and require fresh confirmation.
4. **Multi-provider gateway (`src/bud/gateway/`):** Routes ChatGPT subscription, API, and local models with tier fallback (`fast` / `frontier`), daily USD spend caps (`BUD_DAILY_USD`), and capability-aware image routing (`src/bud/gateway/attachments.py`).

## Jobs, reminders, and background commands

Quick answers stay in the main conversation. Multi-step work spawns an isolated job session (`max 4` open jobs per scope) with its own history, pinned skill versions, and indexed approvals (`<task_id>-<letter>`, e.g. `/approve 1-A`).

- Finished jobs report back to the originating chat automatically and save `reports/<date>-<time>-job-<id>-<title>.md` in the workspace.
- Silent/long jobs trigger stalled notices after `JOB_IDLE_MINUTES` (10) or `JOB_DEADLINE_MINUTES` (60). `/stop` halts active runs in the conversation.
- **Reminders:** server-stored timers up to 30 days ahead; survive restarts and fire on boot if missed.
- **Background commands:** up to 3 concurrent sandboxed commands running up to 6 hours, streaming logs to `background/<id>.log` in the workspace. Interrupted commands report failure rather than restarting silently.

## Scheduled routines

`src/bud/core/routines.py` runs skill-based workflows on a 5-field cron schedule and IANA timezone inside DBOS:

- **Deduplication:** each slot gets `sched:<routine_id>:<utc_slot>` so recovery never double-runs a slot.
- **Missed & overlap policies:** configurable `run_once`, `skip`, or bounded `catch_up` (max 5); overlapping triggers skip or queue.
- **Separate delivery claims:** execution status and delivery status (`pending`, `claimed`, `dispatched`, `confirmed`, `needs_review`) are tracked independently. Uncertain sends pause the routine (`pause_reason="uncertain"`) rather than retrying blindly.
- **Scoped preferences:** injected at launch in precedence order: `routine prompt > skill:<name> > global`.
- **Approvals & API:** pending actions use `R<seq>-<letter>` and expire automatically after `approval_expiry_seconds` (default 24h). Endpoints live under `/routines`.

## Skills & saved tool results

- **Skills:** Agent Skills ([agentskills.io](https://agentskills.io/)) load lazily: only `name` and one-sentence descriptions sit in the prompt until `list_skills` / `find_skills` / `prepare_skill` / `install_skill` load full instructions.
- **Saved tool results:** tool outputs over 10,000 characters spill to `bud.db`; the model sees a preview handle and reads chunks back via `read_tool_result_chars` or `load_tool_result` (inside `run_code`). Results expire 24 hours after last read (`TOOL_RESULT_RETENTION_HOURS`) unless extended with `keep_tool_result`.
