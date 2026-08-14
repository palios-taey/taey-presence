# Claude Code task: first content-reference transport seam

## Authority and scope

Implement only the contract in this file against the current `origin/main`.
This is a bounded mechanical change, not an invitation to redesign the Chat
driver, operating prompt, targeting grammar, or serving architecture.

Allowed implementation files:

- `serving/ui_drive.py`
- `serving/soma_proxy.py`
- `serving/TAEY_OPERATING_PROMPT.md`, limited to the exact operating rule
  specified below
- focused automated tests for this contract

Do not otherwise change:

- any platform YAML or `taeys-hands` code
- display mappings, action names, element targeting, locks, navigation, send
  behavior, extraction behavior, or prompt-echo rules
- systemd units, vLLM settings, deployment files, or running services

Do not merge, deploy, restart services, drive a live Chat UI, or repair anything
outside this contract. Open a PR and return the raw verification receipts.

## Problem

The outbound path is already artifact-backed:

```text
file on disk -> drive_chat(action="paste", text_file=...) -> Chat
```

The inbound path is not on `main`. `drive_chat(action="extract")` returns
`response_text` inside the model-facing tool result. Taey must therefore read
the full response into context and regenerate it to save or relay it. That is
slow and can alter the content.

PR #102 contains an attempted `read_clipboard path=...` implementation, but do
not cherry-pick or copy the branch. It is bundled with rejected, unrelated
changes; it creates missing directories, overwrites existing files, and returns
the captured head and tail to the model. Implement the contract below cleanly
from current `main`.

Add an optional artifact-backed inbound path:

```text
Chat -> drive_chat(action="extract", output_file=...) -> file on disk
file on disk -> drive_chat(action="paste", text_file=...) -> another Chat
```

Taey must be able to relay the extracted response by passing a path, without
receiving or regenerating the response body.

This is the first seam in a larger source-to-artifact-to-sink design. Do not add
database, Redis, dashboard, or arbitrary URI adapters in this PR. Once this
seam is proven, those sources and sinks can reuse the same artifact receipt
without changing the operating rule.

## Required contract

### Tool schema and proxy forwarding

Add optional string property `output_file` to the `drive_chat` tool schema.
It is valid only for `extract` and `read_clipboard`.

When supplied, `serving/soma_proxy.py` must forward it to `ui_drive.py` as
`--output-file <path>`. Invalid action/property combinations must fail clearly.

### File write

In `serving/ui_drive.py`, add one shared helper used by both `extract` and
`read-clipboard`.

The helper must:

- require an absolute path;
- refuse a symlink destination;
- refuse to overwrite an existing file;
- require the parent directory to already exist;
- encode the Python string as UTF-8 exactly once;
- create the file with mode `0600`;
- flush and `fsync` before reporting success;
- calculate SHA-256 over the exact bytes written;
- return:
  - absolute `output_file`;
  - `bytes`;
  - `chars`;
  - `sha256`.

On any error, fail loud and do not report success. Do not create parent
directories and do not silently select a different path.

### Extract behavior

If `output_file` is absent, preserve current behavior exactly: return the
mapped adapter result unchanged.

If `output_file` is present:

1. Call the same `drive_chat_adapter.extract(platform)` exactly once.
2. Preserve the current nonempty-response validation.
3. Preserve the current `sent_file` prompt-echo validation and run it before
   writing.
4. Write `response_text` to `output_file` with the shared helper.
5. Return the adapter metadata plus the artifact receipt.
6. Remove `response_text` from the returned JSON so the response body does not
   enter Taey's context.

Do not clear the clipboard, construct an element reference, click after
extraction, reinterpret the response, normalize whitespace, or retry through
another path.

### Clipboard behavior

If `output_file` is absent, preserve current behavior exactly:
`{"text": <clipboard text>}`.

If `output_file` is present, write the clipboard text with the shared helper
and return only the artifact receipt. Do not include `text` in the returned
JSON.

### Canonical operating-prompt rule

Add one short section to `serving/TAEY_OPERATING_PROMPT.md`. Preserve this
meaning; do not add platform-specific procedures or architecture:

```text
CONTENT TRANSPORT

When content already exists, do not regenerate it. Capture it to a file with
the source tool's output_file and keep the returned path and SHA-256 receipt.
Deliver it with the destination tool's file/path parameter. Read the body only
when you must reason about the body; routing, copying, saving, or relaying does
not require reading it. For a Chat response, prefer drive_chat extract with
output_file; to place that response in another Chat, use drive_chat paste with
text_file set to the returned path. A successful tool call is not proof of
delivery: preserve the receipt and verify the destination.
```

The tool schema remains the detailed invocation authority. The operating
prompt states the invariant, not every source/sink combination.

## Required tests

Add focused automated tests that do not need a display or live Chat account.
Mock the adapter and clipboard boundaries.

The tests must prove:

1. Existing `extract` behavior is unchanged when `output_file` is absent.
2. Existing `read_clipboard` behavior is unchanged when `output_file` is
   absent.
3. Extracted Unicode text is written as the expected UTF-8 bytes.
4. The returned byte count, character count, and SHA-256 match the file.
5. `response_text` is absent from the extract result when `output_file` is
   used.
6. `text` is absent from the clipboard result when `output_file` is used.
7. `sent_file` prompt echo is rejected before any output file is created.
8. Relative paths, missing parent directories, symlink destinations, and
   existing files are refused.
9. The proxy forwards `output_file` only for `extract` and `read_clipboard`.
10. No adapter retry or second extraction occurs.
11. The canonical operating prompt contains the content-transport rule and no
    platform-specific behavior was added.

Run the repository's existing checks plus the focused tests.

## Return package

Return:

- branch name and commit SHA;
- PR URL;
- `git diff --stat origin/main...HEAD`;
- `git diff --check origin/main...HEAD`;
- exact test commands and complete pass/fail summaries;
- the names of every changed file;
- a short statement confirming that no live services, displays, prompts,
  platform maps, or deployment files were changed.

Stop after opening the PR. A Chat will review the source and evidence before
merge.
