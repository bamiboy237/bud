# Connecting Real Accounts

Bud uses Composio's Model Context Protocol (MCP) integrations to securely interact with external accounts.

## Supported Services
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
4. Restart the server:
   ```bash
   uv run bud serve
   ```
