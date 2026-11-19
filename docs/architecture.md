# Architecture & Durability

Bud is built around an append-only event log and deterministic state recovery.

## Core Concepts

1. **Event Sourcing (`src/bud/core/log.py`):**
   Every action, user prompt, and tool call is recorded sequentially in an SQLite event store. Clients stream live tokens over Server-Sent Events (SSE) and reconstruct session state deterministically.
2. **Durable Execution (`src/bud/core/runner.py`):**
   Runs execute under DBOS durability. If the host crashes or the process is terminated mid-flight, Bud inspects uncompleted tasks upon restart and safely resumes or fails without duplicating side-effects.
3. **Model Gateway (`src/bud/gateway/`):**
   Requests route through a multi-provider gateway supporting ChatGPT subscriptions, OpenAI, DeepSeek, Muse, and configured API or local endpoints. Tier routes can fall back after availability failures. Pinned models do not fall back, and local routes do not fall through to other endpoints.

## Conversation and jobs

The primary conversation and each job use separate durable sessions. A waiting
job leaves the conversation free to answer another question. Each job keeps a
short brief, working history, pinned skill versions, and exact approvals.
Job approvals and results return to the conversation. See [Jobs](jobs.md).

## Saved tool results

A tool result over 10,000 characters is saved in the session database, and the model
sees a short preview with a handle. Results from earlier turns are replaced the same way.
The model reads a saved result back with `read_tool_result_chars` or `load_tool_result`.

A saved result expires 24 hours after its last read (`TOOL_RESULT_RETENTION_HOURS`). It
never expires while its session is running or waiting for approval. Inside `run_code`,
Bud can keep a result longer with `keep_tool_result`: 1 day from now by default, 1 to 3,650
days, or until it chooses again. Keeping never makes a result expire sooner than the default.
