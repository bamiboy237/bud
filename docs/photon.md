# iMessage Integration via Photon v2

Bud uses Photon's official Python v2 client and Socket.IO events. The Bud server
starts its iMessage connection. No separate bridge process is required.

## Features
- Receive user prompts and deliver completed replies directly in iMessage.
- Conversational approvals: Bud sends an approval prompt with an exact reference (e.g. `/approve <id>`), waiting for your confirmation before dispatching write actions.

## Configuration

Use the [Photon v2 dashboard](https://app.beta.photon.codes/) to register your
Messages send-from identity and get its assigned agent number.
Set these values privately in your environment or ignored `.env` file:

| Setting | Value |
| --- | --- |
| `PHOTON_API_KEY` | Project-scoped v2 key |
| `PHOTON_PROJECT_ID` | v2 project ID |
| `PHOTON_OWNER_HANDLE` | Your exact Messages send-from email or E.164 number |
| `PHOTON_AGENT_HANDLE` | The different number assigned to that sender in v2 |

Start the server:

```bash
uv run bud serve
```

The configured owner's direct conversation shares history with `bud chat`.
Use the exact approval commands shown in each preview. `/status` shows current
proposals. `bud serve --simulate` disables real messaging.
