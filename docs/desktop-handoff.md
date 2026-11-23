# Verify a chat file on paired desktops

A desktop worker connects outbound to Bud, checks one file the owner selected,
and posts a receipt in that file’s conversation. The same job can run on two
desktops in order. Both workers can sleep or disconnect between steps. They
resume from the server’s job record and reuse any local receipt they already
computed.

This flow verifies a file using SHA-256. Browser execution and interactive
takeover use their own controls and need a separate integration.

## Try it with the Mac companion

1. Build and run the companion with `scripts/dev-stack up --simulate`, or
   connect the companion to a Bud server you already operate. Pair each worker
   against the same server. When testing with dev-stack, use the server URL
   and `DATA_DIR` printed by that stack for the owner commands.
2. On the server host, use `bud desktop --url "$BUD_URL" pair "My Mac"`.
   Pair a second worker with a different name, such as `Daytona`. Each response
   includes a token for that worker. Share each token privately with its
   intended worker and keep the owner credential on the server host.
3. Run `bud desktop --url "$BUD_URL" run` on each worker. Enter its pairing
   token when prompted. For unattended workers the same token can come from
   `BUD_DESKTOP_TOKEN`. Remote workers require an HTTPS route to the Bud server.
   The Mac and Daytona worker initiate the connection and listen on no ports.
4. Attach a small test file in the companion, send the message, and open
   its attachment. Use **Verify on paired desktops** in the preview toolbar.
   Select one or two desktops in execution order and queue verification.
5. The original chat gets a receipt with the desktop name, platform, and SHA-256
   after each step. Try stopping the second worker before queueing, then start
   it again to see the job resume.

For terminal testing, `bud desktop primary` prints the chat ID.
`bud desktop attachments CHAT_ID` prints the file names and IDs.
`bud desktop dispatch CHAT_ID FILE_ID FILE_NAME DESKTOP_ID [DESKTOP_ID]`
   queues the same operation. `bud desktop status CHAT_ID JOB_ID` shows progress,
and `bud desktop revoke DESKTOP_ID` removes a worker’s access. All commands use
`--url` before the action when the server differs from the default.
