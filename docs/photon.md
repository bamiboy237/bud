# iMessage Integration via Photon v2

Bud supports conversational messaging over Apple iMessage using the Photon v2 bridge.

## Features
- Receive user prompts and deliver streamed replies directly in iMessage.
- Conversational approvals: Bud sends an approval prompt with an exact reference (e.g. `/approve <id>`), waiting for your confirmation before dispatching write actions.

## Configuration
Set your Photon credentials in `.env`:
```env
PHOTON_API_KEY=your-api-key
PHOTON_BRIDGE_ID=your-bridge-id
```
Launch the bridge alongside the Bud server to enable iMessage routing.
