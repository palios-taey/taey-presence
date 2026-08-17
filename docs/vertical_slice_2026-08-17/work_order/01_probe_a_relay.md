# PROBE A — RELAY CANARY (Taey drives, one action at a time)

Send exactly this string to ChatGPT on display `:2`:

```
TAEY-DELEGATION-SLICE-2026-08-17 SOURCE=HORIZON STATE=RELAY-CANARY
```

Wait for the response.

Extract the response **directly to an on-disk file** using your `extract` action with
`output_file` — do not use the clipboard. `extract` returns the path and the SHA-256
receipt instead of the body; you are not meant to read the body.

Record the SHA-256.

Paste that file into Claude on display `:3` using `paste` with `text_file` set to the
returned path — pass the path, not the content.

**DO NOT click send.** Clear the composer afterward.

---

## Operator notes (verified against the live tool surface at taey-presence `0ea9aa86`)

- `drive_chat` production actions are exactly:
  `activate, click, extract, focus, focus_dialog, key, navigate, observe, paste,
  read_clipboard, type`.
  There is **no** `verify`, `verify_attachment`, `attach`, or `scroll` action. Use
  `observe` to confirm state.
- `output_file` is accepted **only** for `extract` and `read_clipboard`
  (`soma_proxy.py:1527-1533`), and the destination **must not already exist** — the
  write refuses to overwrite.
- `text_file` is accepted **only** for `paste` (`soma_proxy.py:1571`). Its schema text:
  *"absolute path to a file whose EXACT bytes are pasted … pass the path, not the
  content; the tool reads and pastes it. Instant and byte-perfect."*
- This is the no-regeneration route end to end: the body never enters the model on
  either leg.
- On Claude, clicking `send_button` returns `ok:true` and does **not** send — measured.
  That is incidental here because this probe must not send, but do not read `ok:true`
  from a click as proof of delivery.
- Display ownership: `:2` ChatGPT, `:3` Claude. Both are in Taey's lane.
