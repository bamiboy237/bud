# Security & Approval Model

Bud operates under a strict principle of least surprise: **Read is automatic; Write requires explicit user consent.**

## The Approval Flow
1. When Bud plans a side-effect (e.g. creating a workspace file, modifying a calendar event, or sending a draft), it registers an exact `approval_id`.
2. The execution pauses, surfacing the proposal to the user (in terminal, web, or iMessage).
3. The user grants approval via exact reference (`/approve <id>`) or denies it (`/deny <id>`).
4. Replayed or duplicated dispatches verify pre-conditions against SQLite claim records to prevent double-execution.

An email action also carries a readable preview of the original message: subject, recipients, and the full body, captured from Bud's own records when the action is held. The preview is presentation only. The decision still names one exact approval ID, and the tool arguments do not change. A preview that cannot be read marks itself unavailable and never blocks the decision. Previews apply to new approvals; older pending approvals keep their original shape.

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
