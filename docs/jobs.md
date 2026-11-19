# Jobs in the conversation

Quick questions stay in the chat. Work that takes time, produces a deliverable,
or changes data runs as a separate job. Bud gives it a short brief containing
the goal, relevant inputs, and expected result. Each job uses its own session,
working history, and approvals with the same model, tools, and approval policy.
The offered skill versions stay pinned to the job, including later corrections.

Keep chatting while a job waits. For example:

```text
you: Create checkpoint-note.txt containing Cedar first draft.
bud: Task 1 is preparing the note.
Task 1-A: Create checkpoint-note.txt?
/approve 1-A
/deny 1-A

you: What is 2 + 2?
bud: 4

you: For task 1, use Cedar revised draft. instead.
Task 1-B: Create checkpoint-note.txt?
/approve 1-B
/deny 1-B
```

The unrelated question leaves `1-A` pending. The correction retires `1-A`
and creates `1-B`. Each reference identifies one exact proposal. The task
number alone is not an approval. Original full approval IDs remain supported.
The conversation, job, pending decision, and saved results survive a process
restart. A completed job posts its result back to the conversation and the
interface where you requested it.

When a job finishes, fails, or pauses because it was going in circles, Bud
tells you in the conversation without waiting for your next message. If a
message of yours is already waiting, Bud gets the job's result with that
message instead. A job you cancel gets no report.

A finished job also saves its result in Bud's workspace, without asking, as
`reports/<date>-<time>-job-<id>-<title>.md`, readable only by you. Bud creates
the `reports` folder when it needs it, never replaces a report that already
exists, and never writes through a link. A report you delete stays deleted.
The Mac companion shows **Open report** beside the job's "finished" notice.

Bud also tells you when a running job looks stuck. A job is stuck when it has
logged nothing for `JOB_IDLE_MINUTES` (default 10), or has been open longer
than `JOB_DEADLINE_MINUTES` (default 60). A job waiting for your approval is
not stuck. Each reason is reported once per job. The Mac companion shows
"needs you" until the job shows activity again. Ask Bud to stop the job, or
let it continue.

Name the job for a correction or follow-up. Bud asks which one when several
jobs could fit the request. Ask it to inspect or stop a named job. `/stop`
also stops the current conversation's open jobs.

## API reference

Session snapshots include `tasks` and aggregate pending job approvals.
Each proposal adds `task_id` and `reference` to its existing exact ID, tool,
and arguments. `GET /sessions/{session_id}/tasks/{task_id}` returns the brief,
current job view, and working conversation. Supporting details remain stored.
Job histories are not automatically deleted or forked.

The job admission limit is four open jobs in the account or simulated-world
scope. Reopening a finished job uses the same limit. Child jobs use the normal
action tools. The conversation uses read and job-control tools. Job creation
and updates are replay-safe. A correction retires approvals only in that job.
