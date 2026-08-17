# PROBE A RE-RUN — receipted relay canary

Re-run rather than reconstruct. A pass label without receipts in the tree is the Aug 16 bug
in miniature, and the relay is load-bearing for the whole UI objective.

## Constraint that governs this entire task

UI work is **supervised and step-by-step**: observe, then ONE action, then verify, then the
next. Do NOT chain the steps into an autonomous loop, do not build a retry, and do not add a
recovery path. If any step does not produce what you expect, **STOP and report** — a stopped
probe is a fine outcome, a looping one is a banned one.

## Steps

1. Send exactly this string to ChatGPT on display `:2`:
   `TAEY-DELEGATION-SLICE-RERUN-2026-08-17 SOURCE=HORIZON STATE=RELAY-CANARY`
2. Wait for the response. `observe` to confirm it arrived.
3. `extract` the response **directly to disk** with `output_file` set to
   `/home/mira/taey_runs/probe_a_rerun/chatgpt_response.txt` — the destination must not
   already exist. Record the returned path and SHA-256. Do NOT read the body.
4. `paste` into Claude on display `:3` using `text_file` set to that returned path — pass the
   path, not the content.
5. `observe` `:3` and capture what the composer actually contains.
6. **DO NOT click send.** Then clear the composer and `observe` to confirm it is empty.

## Receipts to return

- the extract path and its SHA-256 as the tool returned them
- `pasted_chars` and `source` from the paste result
- what `observe` showed on `:3` before clearing, and after clearing
- explicit confirmation that send was never clicked

## Known measured defect — do not be fooled by it

On Claude, clicking `send_button` returns `ok:true` and does **not** send. Do not read
`ok:true` from a click as proof of delivery anywhere in this task.
