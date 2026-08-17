# THE REAL CARRY — do this now, one action at a time

A REAL careers packet has been produced by the production CLI (assemble_brief.py):

  /tmp/careers_briefs/connection_request_INFRA-CARRY-2026-08-17-01.json   19290 bytes

## Step 1 — collect (run_command)

taey-delegate collect /tmp/careers_briefs/connection_request_INFRA-CARRY-2026-08-17-01.json -o /home/mira/taey_runs/carry2/manifest.json

Keep the exit code. Do not read the packet body.

## Step 2 — ATTACH both files to Claude on :3. Do NOT paste. Attach.

The packet is 19KB. Never paste a long packet. Use the proven attach sequence, ONE call each,
observing between:

  1. focus the attach control ('Add files and more'), then key='space'
     FOCUS+SPACE, NOT click — a click opens a popup whose items are unnamed and unreachable
  2. type the menu label, e.g. 'Add photos'
  3. key='Down'
  4. key='Return'                → the GTK file dialog opens
  5. action='focus_dialog'       → REQUIRED. The dialog is a SEPARATE X11 window; without this
                                   your keystrokes go to Firefox's address bar
  6. key='ctrl+l'
  7. key='ctrl+a'
  8. type the FULL ABSOLUTE PATH
  9. key='Return'
  10. observe and CONFIRM the filename chip is in the composer before you send

Do that for the PACKET, then again for the MANIFEST:
  /tmp/careers_briefs/connection_request_INFRA-CARRY-2026-08-17-01.json
  /home/mira/taey_runs/carry2/manifest.json

## Step 3 — send

Type a short message saying what the two attachments are: a real careers connection_request
brief produced by assemble_brief.py, and the taey-delegate collect manifest over it. Ask the
Chat to accept or correct. Then key='Return' to send.

## Step 4 — STOP. Do not wait for the reply in this turn.

Report immediately: collect exit code, manifest path, that BOTH filename chips were confirmed
in the composer before sending, and that you sent. Harvest the reply in a later turn.

## Standard

Report only what a tool returned. Anything you did not verify, mark [Unknown]. If a step does
not do what you expect, STOP and say which step and what you saw. Do not retry in a loop.
