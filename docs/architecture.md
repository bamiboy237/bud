# Architecture & Durability

Bud is built around an append-only event log and deterministic state recovery.

## Core Concepts

1. **Event Sourcing (`src/bud/core/log.py`):**
   Every action, user prompt, and tool call is recorded sequentially in an SQLite event store. Clients stream live tokens over Server-Sent Events (SSE) and reconstruct session state deterministically.
2. **Durable Execution (`src/bud/core/runner.py`):**
   Runs execute under DBOS durability. If the host crashes or the process is terminated mid-flight, Bud inspects uncompleted tasks upon restart and safely resumes or fails without duplicating side-effects.
3. **Model Gateway (`src/bud/gateway/`):**
   Requests route through a multi-provider gateway supporting ChatGPT subscription tokens, OpenAI, DeepSeek, and Muse with automatic fallback if an upstream provider experiences availability downtime.
