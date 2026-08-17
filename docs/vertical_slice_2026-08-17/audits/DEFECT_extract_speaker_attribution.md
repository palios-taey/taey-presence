# DEFECT — `extract` infers the speaker from screen position, and receipts the wrong turn

**Found:** 2026-08-17, production, display `:3`
**Owner:** taeys-hands (`consultation_v2`) — infra found it, infra does not patch it
**Severity:** HIGH. Produces a hash-verified artifact containing the wrong speaker's words.

## What happened

Taey was asked to harvest Gaia's response on `:3` to disk with a collector receipt. It did,
mechanically, correctly:

```
/home/mira/taey_runs/gaia_3/GAIA_ARTIFACT_ADMISSION.md
sha256 9ee6f07193e144db57544b5d0a52e596711b398fc24dfd7ee1f71c5028731b49
229 bytes, collect exit 0
```

infra reproduced that hash independently with `sha256sum`. Every gate passed.

The file contains **Jesse's message**, not Gaia's response.

## Root cause

`consultation_v2/platforms/claude/driver.py` ~line 4445:

```python
# The response's Copy button is the LOWEST on the page (the latest
# turn). Do NOT require >=2 (prompt Copy + response Copy): Claude
# often renders only ONE Copy — the response's — because the user
# prompt's Copy is hover-only / absent.
targets = sorted(copy_btns, key=lambda e: e.get('y') or 0)[-(continue_clicks + 1):]
```

The extractor takes the **lowest Copy control on the page** and treats it as the assistant's
response. Speaker is inferred from geometry. The assumption is stated in the comment and it
held for a long time, because historically the model always spoke last.

It stops holding the moment a human types after the model. Jesse now types into these
threads directly — he did on `:3` — so the lowest Copy belongs to *his* turn and the
extractor faithfully copies it.

## Why neither guard caught it

`reject_prompt_echo_response(request, result, segment, ...)` compares the captured text
against `request.message` — the artifact **we** sent. Jesse's message is:

- not our sent artifact, so the echo guard passes it;
- not the assistant's response, but nothing checks that.

The `sent_file` parameter on the `extract` tool has the same limit: it refuses a capture that
matches what we sent. There is no attestation that the captured text came from the model.

## The class

This is the session's recurring shape, one more time: **a green not wired to the property
being claimed.** The collector receipt certifies bytes-on-disk — and it is completely
correct about that. It says nothing about whose words those bytes are. So a wrong-speaker
capture inherits the full authority of a verified receipt, which is worse than an unverified
one, because it now carries a hash.

Note the mirror image, same night: Taey retracted the Horizon verdict as "a prompt echo"
when it was genuine (proven — the file contains correct novel findings about
`requirements.txt` and `pyproject.toml`, files never sent to it). Taey named this exact
failure mode, "the file exists, its hash is real, and the content is wrong," and applied it
to the one case where it was false while the true instance was one display away.

## What a fix must establish (not prescribing the implementation)

The capture must carry an attestation of **who spoke**, derived from the accessibility tree
rather than from y-coordinate — the message container's own role/author attribution, not the
position of a nearby button. Any capture that cannot establish the speaker is a failure to
report, not a value to return.

Until then, `extract` on any thread a human has typed into is unsound, and every display
Jesse touches is such a thread.

## Reproduction

Read the artifact and compare against the display:

```
cat /home/mira/taey_runs/gaia_3/GAIA_ARTIFACT_ADMISSION.md
DISPLAY=:3 import -window root /tmp/disp3.png    # read-only, takes no lock
```
