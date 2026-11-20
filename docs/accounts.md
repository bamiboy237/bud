# Connecting Real Accounts

Bud uses Composio's Model Context Protocol (MCP) integrations to securely interact with external accounts.

## Supported services

- **Gmail:** Triage unread threads, retrieve drafts, and propose replies.
- **Google Calendar:** Check schedule conflicts, resolve timezones, and propose calendar events.

## Setup
1. Obtain an API key from [Composio](https://composio.dev).
2. Set `COMPOSIO_API_KEY` in your `.env` file.
3. Link your accounts:
   ```bash
   uv run bud connect gmail
   uv run bud connect calendar
   ```
4. Finish the sign-in in your browser. The change loads on your next message, in the same
   chat, without a restart.

Every account change loads in place: a new app, a reconnect, a different account, or a
removal. Your chat and memory carry over. One thing does not: when an account is replaced or
removed, every approval still waiting for that app is denied, so an approval given for one
account can never run against another. Ask again and Bud asks you again. Starting a sign-in
again, or cancelling it, before it finishes changes nothing else.
