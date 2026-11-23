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

## Deliver a file through a browser on another desktop

Configure a normal HTML form that uses a file input, a text reference field,
and a submit button on a single form. The confirmation page must show the exact
reference in one element. Bud uses the reference to check whether an earlier
submit succeeded. For a synthetic test, use IDs like these.

~~~json
{
  "url": "https://portal.example.test/deliver",
  "file_input": "invoice-file",
  "reference_input": "reference",
  "submit_button": "submit-form",
  "receipt": "receipt-number"
}
~~~

Start a Browse-enabled Bud server on the browser desktop and pair Chrome with
it. Run the worker there with its paired-device token and
`bud desktop --url "$COORDINATOR" run --browser-local http://127.0.0.1:8420`.
The worker uses the local server's owner credential for loopback requests only.
The coordinator can reach only the paired-worker endpoints, and it gets no
browser login data.

Dispatch a sent attachment with
`bud desktop --url "$COORDINATOR" dispatch CHAT FILE_ID FILE_NAME DESKTOP_ID --form form.json`.
With two desktop IDs, the first verifies the file and the second opens the form.
Bud reports readiness in the originating chat. If the site requires sign-in,
complete it on the destination browser and click **Continue**. Then approve the
file and destination with
`bud desktop --url "$COORDINATOR" browser CHAT JOB_ID approve --desktop DESKTOP_ID`.

While approval is still pending, Bud can wait for the destination or the owner
can change it with `bud desktop retarget CHAT JOB_ID DESKTOP_ID`. The new
desktop opens the configured URL with its own browser session. If a site saved
a draft at that URL it can show up there. Any typing held just in the old tab
stays on the old machine.

If the connection drops after approval is consumed, Bud looks for the matching
receipt in the existing tab. When it cannot confirm an outcome, the job shows
**Uncertain**. `bud desktop browser CHAT JOB_ID verify` checks again without
submitting. Standard forms are the supported target. Test a site with a
synthetic file and a safe draft workflow before using its real submission page.
