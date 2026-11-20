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
4. Finish the sign-in in your browser, then refresh connections in Bud. A new app's tools
   load on your next message, without a restart.

Starting a sign-in again, or cancelling it, before it finishes changes nothing else: that app
was never active, so no chat or approval can point at it. Replacing, reconnecting, or
removing an account that was active requires a server restart and a new chat, so an approval
given for one account can never run against another.
