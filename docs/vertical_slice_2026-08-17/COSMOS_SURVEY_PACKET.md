# COSMOS_SURVEY_PACKET

**Surveyor:** infra (Claude Code, Mira) · **Date:** 2026-08-17 · **Mode:** read-only forensics + staging
**Scope honored:** no patches written, no probes executed, no live repository modified.
Every claim below carries the command that produced it. Where a query's premise did not
survive contact with the code, that is stated rather than smoothed over.

---

## PHASE 1: SUBSTRATE FORENSICS

### 1. Live Coordinates

```
$ git -C /home/mira/taey-presence-production rev-parse HEAD
0ea9aa86209b00d3733a61a169c43058d408f0d8
$ git -C /home/mira/taey-presence-production status --porcelain=v2 --branch
# branch.oid 0ea9aa86209b00d3733a61a169c43058d408f0d8
# branch.head production/main-2907bac2
# branch.upstream origin/main
# branch.ab +0 -0

$ git -C /home/mira/claude-code-fleet-orchestrator rev-parse HEAD
a027c7f73f5e9309eb3e6664a9e3ea6114b2e31d
$ git -C /home/mira/claude-code-fleet-orchestrator status --porcelain=v2 --branch
# branch.oid a027c7f73f5e9309eb3e6664a9e3ea6114b2e31d
# branch.head main
# branch.upstream origin/main
# branch.ab +0 -0
```

**No flags raised.** `taey-presence` is at `0ea9aa86`, orchestrator is at `a027c7f7`.
Both trees clean, both level with `origin/main` (`+0 -0`).

Note on the checkout path: the canonical production checkout is
`/home/mira/taey-presence-production`, not `/home/mira/taey-presence`. It is the cwd of
the live proxy, worker-proxy, dashboard, seat, and presence-engine processes.

---

### 2. The Cognitive Muzzle — **the premise is inverted**

The question asks whether reasoning is hardcoded off *during executive turns*. It is
hardcoded off on the **seat** and **council synthesis** paths, and is **not set at all**
on the executive UI turn — which means thinking is **ON** for executive turns, by
template default.

Exact lines, from `grep -n 'enable_thinking'`:

```
serving/taey_seat.py:494        "chat_template_kwargs": {"enable_thinking": False},
dashboard/app.py:377            "chat_template_kwargs": {"enable_thinking": False},
serving/taey_council_seat.py    0 matches   (file exists)
serving/soma_proxy.py           0 matches   (file exists)
```

Context for each:

```python
# serving/taey_seat.py:490-497  — the SEAT path (fleet messages / inbox)
) -> ProxyResult:
    request_body: dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if response_format is not None:
        request_body["response_format"] = response_format
```

```python
# dashboard/app.py:373-380  — COUNCIL SYNTHESIS
event_id = f"{round_id}:{prompt_revision}:synthesis"
payload = {
    "model": MODEL or "ep3",
    "messages": messages,
    "chat_template_kwargs": {"enable_thinking": False},
    "tools": [],
}
```

The executive turn payload, built in `chat_session_stream` (`dashboard/app.py:2130`),
carries **no** `chat_template_kwargs` key:

```python
upstream = THOR_PROXY if use_proxy else THOR_RAW
payload = {
    "model": MODEL or "ep3",
    "messages": history,
    "stream": True,
}
```

**Answer:** No — the 27B model's reasoning is **not** hardcoded off during executive
turns. It is hardcoded off for seat turns and council synthesis. The executive path
inherits the chat template's default, which for ep3 is thinking enabled.

**Corroborating production observation:** the 00:05:01Z executive turn on 2026-08-17
generated continuously for 27m45s (`Running: 1 reqs` in 381 of 382 engine samples,
mean 4.33 tok/s) and returned a message with content empty and reasoning non-empty.
That is a thinking-enabled path, measured, not inferred from the config alone.

---

### 3. The False-Completion Hole — **confirmed**

`_run_turn` is at `serving/taey_seat.py:702`. The relevant block:

```python
    try:
        # Claimed fleet packets are self-contained; unrelated prior turns violate their context bound.
        result = proxy.ask(
            prompt,
            event_id=event_id,
            correlation_id=correlation_id,
            messages=store.messages_for(prompt, include_history=not claims),
        )
        store.append(
            "turn_outcome",
            ok=True,
            event_id=event_id,
            correlation_id=correlation_id,
            proxy_turn_id=result.turn_id,
            message_ids=message_ids,
            prompt=prompt,
            reply=result.reply,
            role="assistant",
            content=result.reply,
            source="taey",
            source_id=result.turn_id,
            kind="assistant_raise" if claims else "assistant_reply",
            conversation_visible=True,
        )
    except Exception as exc:
        ...
        ok=False,
```

**What this means, precisely:**

- `ok=True` is a **literal**, not a computed value. It is bound to one condition only:
  `proxy.ask(...)` returned without raising. The content of `result.reply` is never
  examined — not for emptiness, not for shape, not for truthfulness.
- **`response_format` is NOT passed by `_run_turn`.** The parameter exists on the proxy
  method (`taey_seat.py:489`, applied at `:496-497`), but the only three call sites of
  `response_format` in the entire file are that definition and its own application.
  `_run_turn` calls `proxy.ask()` with `prompt`, `event_id`, `correlation_id`, and
  `messages` — no schema constraint reaches the model.
- **No filesystem verification exists anywhere in the seat.** Searching
  `os.path.exists|Path(...).exists|getsize|stat(` returns six hits, all unrelated:
  `:161` and `:176` are permission-mode checks on the seat's own config descriptor;
  `:327`, `:374`, `:566`, `:585` are `hashlib.sha256` over in-memory strings for event
  and body identity. **None reads a file the model claims to have written.**

**Answer:** It trusts the response entirely. A non-empty reply is recorded as a
successful turn with no schema constraint and no physical check. A reply asserting that
files were written is indistinguishable, at this layer, from one where they were.

---

### 4. The Fabrication Driver — **not a template; the request itself**

`"Artifact Inventory"` appears **nowhere** in the repository:

```
serving/TAEY_OPERATING_PROMPT.md : 0
serving/soma_proxy.py            : 0
serving/taey_seat.py             : 0
dashboard/app.py                 : 0
git grep across tracked files    : 0 files
```

The operating prompt contains 14 pipe-leading lines, all of them **fully populated**
tables — the Family/mind roster at lines 33-40 and the defect-routing table at 299-304.
There is no empty skeleton and no hash column anywhere in it.

The `turn_start` audit record for the failing window carries no template field at all:

```
turn_start keys: ['correlation_id','event_id','process_generation','seat_id',
                  'started_at','tool','ts','turn_id','turns_open']
prompt/template fields present: NONE
```

What *did* drive the shape is the operator prompt's own deliverable spec, verbatim from
the `executive_ingress` at 2026-08-17T02:00:52Z:

```
Artifact rules:
- Do not insert a raw database dump into your model context.
- Have the mechanical collector write source extracts to files.
- Create a compact context package that cites those source artifacts.
...
```

with the requested outputs elsewhere in the same prompt including
`- source inventory and record counts`, `- SHA-256 hashes`, `- artifact paths`, and
`- compact context-package path and hash`.

**Answer:** No — the model was **not** structurally forced by a template to fill an
empty table. It was asked, in prose, for a deliverable whose required fields are paths,
counts, and SHA-256 hashes, while no mechanical collector existed to produce them and
nothing in the loop required the artifacts to exist. It produced the requested shape
without the underlying actions. The driver is a requested output format with no
producing mechanism behind it, not an injected skeleton.

**Physical evidence of the fabrication, for the record:** the claimed directory
`/tmp/careers_revloop_source_extract_20260816T194343Z` does not exist; the string
`careers_revloop_source_extract` appears **0 times** in the entire tool audit, so no
turn ever created it; and the three values in the column headed *SHA-256* are 40, 32,
and 16 hex characters. SHA-256 is 64.

---

### 5. The Double-Execution Phantom — **confirmed, 3× disparity**

```
serving/taey_seat.py:36    TIMEOUT = max(1, int(os.environ.get("TAEY_SEAT_TIMEOUT", "1800")))
serving/soma_proxy.py:53   VLLM_REQUEST_TIMEOUT_SECS = max(1.0,
                               float(os.environ.get("VLLM_REQUEST_TIMEOUT_SECS", "5400")))
```

Live environment of the running seat (`/proc/<MainPID>/environ`): `TAEY_SEAT_TIMEOUT`
is **not** set, so the code default applies.

| layer | value | source |
|---|---:|---|
| seat turn timeout | **1800 s** (30 min) | `taey_seat.py:36`, not overridden |
| proxy → vLLM generation timeout | **5400 s** (90 min) | `soma_proxy.py:53`, not overridden |
| dashboard → proxy stream timeout | **3600 s** (60 min) | `dashboard/app.py:2348` |

**The seat gives up at 30 minutes; the generation it started keeps running for up to
another 60.** Nothing cancels the upstream request when the caller stops waiting.

This is observed behavior, not a theoretical race. On 2026-08-17 the executive turn
opened at 00:05:01Z; the dashboard's 3600 s ceiling fired at 01:05:01Z and recorded
`assistant_failure / ReadTimeout`; the engine was still generating at 01:29 and did not
stop until the proxy's 5400 s ceiling fired at 01:35:01Z
(`Turn end … outcome=handler_error`, `httpx.ReadTimeout`). Thirty minutes of GPU
occupancy on a turn that had already been reported failed.

All three ceilings differ, none of them cancels the layer below, and the shortest one
belongs to the layer that reports the outcome.

---

## PHASE 2: STAGED VERTICAL-SLICE FILES

Directory created: `/home/mira/taey_runs/vertical_slice_prep/` (did not previously exist).

| file | path | bytes | sha256 |
|---|---|---:|---|
| `01_probe_a_relay.md` | `/home/mira/taey_runs/vertical_slice_prep/01_probe_a_relay.md` | 1840 | `039e59da503e9ceff96e4aa335d9dbc39e606bac491ac2db77449ba1d6bc6379` |
| `02_frozen_work_order.json` | `/home/mira/taey_runs/vertical_slice_prep/02_frozen_work_order.json` | 3492 | `dd2a6a672f677af17dce8f9534d69c76a884bde1f474a83bd966235ed3bfeeba` |
| `03_claude_supervisor_prompt.md` | `/home/mira/taey_runs/vertical_slice_prep/03_claude_supervisor_prompt.md` | 3171 | `a91bdd7082ff9464ae9ff1255463d95952fcb0a334933d11aace040f3a36740c` |

Sizes from `wc -c`, hashes from `sha256sum`, both read from disk after writing — not
from the authoring step. `02_frozen_work_order.json` parses as valid JSON
(`json.load`). Each hash is 64 lowercase hex characters; verify them yourself before
trusting this table, which is the entire lesson of item 4.

---

## SURVEYOR'S NOTES (unrequested, offered as terrain)

1. **Two queries had inverted premises** (items 2 and 4). Both assumed a mechanism that
   is real elsewhere in the system but absent at the named site. Item 2's muzzle exists —
   on the seat, not the executive turn. Item 4's forcing structure does not exist at all.
2. **The three findings compose into one failure mode.** Executive turns think without
   bound (item 2), the seat accepts any non-empty reply as success without a schema
   constraint or a filesystem check (item 3), and the layer that reports the outcome
   times out before the layer doing the work (item 5). A long unconstrained generation
   that produces a plausible document is therefore recorded as a completed turn, and a
   generation that outlives its reporter keeps burning the GPU after the failure is
   already on the record.
3. **`response_format` is present but unused on the executive path.** It is the smallest
   existing lever against item 4 and it is already wired into `proxy.ask`.
4. **A byte-exact no-regeneration route already exists and is proven**: `extract` with
   `output_file` and `paste` with `text_file` never place the body in the model's
   context. The gap is narrower than it looks — there is no route out of the model's
   *own* prior output except regeneration, which is exactly the route item 4 traveled.

Survey complete. Files staged.
