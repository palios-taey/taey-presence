# SEAT ACK GATE — two-file shape declaration contract

| Field | Value |
|---|---|
| **Contract id** | `seat_ack_deliverable_declaration.v1` |
| **Status** | SPEC ONLY — not an implementation |
| **Author seat** | conductor-grok (LOGOS) |
| **Task** | `task-23a49cdb` |
| **Date** | 2026-08-17 |
| **Implements against** | `serving/taey_seat.py` `_run_turn` (the site that records `turn_outcome` with `ok=True`) |
| **Depends on** | `taey-delegate collect` (merged; schema with in-band `verification`) |

## 0. Honesty about provenance

The original “Grok two-file shape” that Chats accepted was a **design sketch**, not a frozen
syntax document: two physical artifacts (a deliverable-declaring packet + a filesystem
manifest), one check at `ok=True`, scope limited to deliverable-declaring turns.

**This document is the first full declaration contract.** Where the sketch was silent, the
gaps are marked **NEW (specified now)** rather than retrofitted as if they had always been
settled. Implementers must not invent alternate syntax; if this contract is incomplete for a
field, STOP and ask — do not pad.

---

## 1. What the two files are

| # | File | Role |
|---|---|---|
| **F1** | **The packet body** (the string that becomes `prompt` / fleet message content for the turn) | **Declaration of intent.** May optionally embed a machine-parseable **deliverables block**. If present and valid, the turn is *deliverable-declaring*. |
| **F2** | **The manifest** (`artifacts.json` or path named by the declaration) | **Filesystem receipt.** Produced by `taey-delegate collect` (or any producer that emits the same schema). Certifies paths/bytes/sha256 under a verified window. |

There is **no third file** in this shape. Logs, reply prose, chat extracts, and model claims are
not substitutes for F2.

---

## 2. How a deliverable-declaring packet is identified

### 2.1 Explicit block only — no NLP

A packet is deliverable-declaring **if and only if** its body contains **exactly one**
well-formed deliverables block matching the syntax below.

- Free-text claims (“I wrote three files under /tmp/…”) **do not** declare deliverables.
- Multiple well-formed blocks → **malformed declaration** (gate fails the turn; see §6).
- A block present but unparseable / missing required fields → **malformed declaration**.

### 2.2 Exact syntax (NEW — fully specified here)

The block is a fenced region in the packet body:

````text
```taey-deliverables
version: 1
manifest: /absolute/or/tilde/path/to/artifacts.json
paths:
  - /absolute/or/tilde/path/to/file_a
  - /absolute/or/tilde/path/to/file_b
```
````

Rules:

| Rule | Detail |
|---|---|
| Fence open | Line is exactly three backticks + `taey-deliverables` (optional trailing whitespace). Language tag is **case-sensitive**: `taey-deliverables`. |
| Fence close | Line is exactly three backticks. |
| Body language | YAML subset, UTF-8. No tabs. |
| `version` | Required integer. Gate implements **only** `version: 1`. Other values → malformed. |
| `manifest` | Required string. Path to F2. Expanded with `expanduser`, then `realpath`/`abspath` at gate time. |
| `paths` | Required non-empty YAML list of strings. Each entry is a declared deliverable path. Expanded the same way. Empty list → malformed. |
| Ordering | `paths` order is not significant for coverage. |
| Duplicates | Duplicate path strings after resolution → malformed. |
| Other keys | Forbidden in v1. Unknown keys → malformed. |

**Not allowed in v1 (explicitly out of scope):**

- Header-only / HTTP-style fields outside the fence.
- JSON-only form (use YAML subset as above).
- Glob patterns, directories-as-deliverables, “all files under”.
- Implicit “whatever collect was last run with”.

### 2.3 Where the block lives

- In the **same string** the seat treats as the turn prompt for fleet-claimed packets
  (today: formatted fleet message body / operator packet text fed to `proxy.ask`).
- The gate scans **that string only**, not prior history and not the model reply.

---

## 3. Where the path set comes from

**Only from `paths:` inside the deliverables block (F1).**

- Not from model output.
- Not from scanning a worktree.
- Not from the manifest’s `artifacts` list alone (the manifest must *cover* the declaration;
  the declaration does not grow from the manifest).

After expansion+resolution, call this set **`D`** (declared paths).

---

## 4. Where the manifest path comes from

**Only from `manifest:` inside the same deliverables block.**

- No default relative `./artifacts.json` unless the declaration literally says so.
- No env-var override in v1.
- No “discover newest artifacts.json under /tmp”.

Resolved path is **`M`**.

---

## 5. Gate predicate (the one check)

### 5.1 When it runs

**Single chokepoint:** immediately before any code path would append `turn_outcome` with
`ok=True` after a model reply for a turn whose **prompt/packet body** is deliverable-declaring.

In today’s tree that is `_run_turn` after `proxy.ask` returns and before
`store.append("turn_outcome", ok=True, ...)`.

Do **not** add a second gate on non-actionable acks, pointer-only paths, or operator
conversation that never embeds a deliverables block.

### 5.2 When it does not run

If the packet body has **no** ` ```taey-deliverables ` fence:

- Turn is **not** deliverable-declaring.
- Gate is a no-op.
- Existing behaviour (including conversational raises to Jesse) is **byte-identical**.

### 5.3 Success predicate for deliverable-declaring turns

All of the following must hold or the turn **must not** record `ok=True`:

1. **Declaration well-formed** per §2.2 → yields `D` and `M`.
2. **`M` exists** as a regular file; readable.
3. **`M` parses** as JSON object.
4. **Schema floor (compatible with `taey-delegate collect` output):**
   - top-level `artifacts` is a list of objects;
   - each object has `path` (string), `bytes` (int ≥ 1), `sha256` (string matching
     `^[0-9a-f]{64}$`);
   - optional but if present must not break parse: `generated_at`, `tool_version`,
     `verification` (the in-band block from collect is **not** re-validated in full by this
     gate; see §5.5).
5. **Coverage:** for every `p ∈ D`, there exists an entry `a` in `artifacts` such that
   `realpath(a["path"]) == p` (both sides resolved the same way as §2.2).
6. **Disk bind for declared paths (cannot-lie floor):** for every such covering entry,
   re-read the file at `p` and require:
   - `os.path.getsize(p) == a["bytes"]`
   - `sha256(open(p,"rb").read()) == a["sha256"]`  
   If the file is missing/unreadable at gate time → fail (even if the manifest claims it).

If any step fails → record outcome with **`ok=False`** (or equivalent non-success path the
seat already uses for hard failures), include a short machine-readable reason, and **do not**
treat the turn as a successful completion for ACK purposes. Exact error field names are
implementation detail; the boolean `ok` must not be True.

### 5.4 What the gate does **not** do

- Does not run `taey-delegate collect`.
- Does not invent paths from the reply text.
- Does not require the model to emit hashes in prose.
- Does not parse or trust Chat platform receipts.
- Does not change proxy/ask timeouts or tool surfaces.

### 5.5 Relationship to collect’s `verification` block

`taey-delegate collect` embeds a `verification` object (methods, residual window, etc.).

- **Gate v1 does not require** re-deriving that object.
- **Gate v1 does require** path coverage + live disk re-hash match for every declared path
  (stronger than “file exists”; weaker than re-proving the whole residual-window story).
- If a future v2 wants to require `verification.methods` to equal the known collect sequence,
  that is a **separate** amendment — not silently assumed here.

---

## 6. Failure classes (normative)

| Class | Condition | `ok` |
|---|---|---|
| `not_declaring` | No deliverables fence | unchanged path (no gate) |
| `malformed_declaration` | Fence present but invalid YAML / missing fields / bad version / dup paths / >1 block | False |
| `manifest_missing` | `M` does not exist or not a regular file | False |
| `manifest_unreadable` | open/parse failure | False |
| `manifest_schema` | missing `artifacts` / bad sha shape / bad types | False |
| `coverage_gap` | some `p ∈ D` absent from manifest paths | False |
| `disk_mismatch` | size or sha256 disagrees with live file | False |
| `pass` | all §5.3 checks hold | True allowed |

---

## 7. Ordinary conversational turns (must be unaffected)

| Packet content | Gate |
|---|---|
| No ` ```taey-deliverables ` fence | **No check.** Same code path as today after `proxy.ask`. |
| Tmux operator free text without the fence | **No check.** |
| Non-actionable fleet message ACKs (`_ack_non_actionable_claims`) | **Out of scope** — do not add the gate there. |

**Proof requirement for implementers (production observation, not unit tests):** one real
conversational turn without a fence still records success the same way as before this patch.

---

## 8. Worked examples

### 8.1 Declaring + good manifest → may `ok=True`

Packet body contains:

````text
Carry these extract files to the authoring chat.

```taey-deliverables
version: 1
manifest: /tmp/revloop_run/artifacts.json
paths:
  - /tmp/revloop_run/extract.md
  - /tmp/revloop_run/receipt.json
```
````

And `/tmp/revloop_run/artifacts.json` is a collect output whose `artifacts[].path` cover both
paths with matching live sha256/bytes → gate allows `ok=True`.

### 8.2 Declaring + no manifest → must not `ok=True`

Same packet; `artifacts.json` missing → `ok=False` even if the model reply is fluent and
claims success.

### 8.3 No declaration → unaffected

Packet body is only: `Jesse: what is the status of Thor1?`  
No fence → gate idle → normal conversational outcome.

### 8.4 Prose-only fabrication (the 2026-08-16 class)

Packet has **no** fence; model invents paths and fake short “SHA-256” strings in the reply.  
Gate does **not** fire (not declaring). That failure class is closed by **making producers
declare** on real carry packets, not by NLP of the reply. Schema-lock / packet templates
(later tasks) force the fence onto carry packets; this gate only enforces the receipt once
declared.

---

## 9. Implementer checklist (still not implementation)

1. Parse fence from **prompt/packet body only** (pre-ask string).
2. If no fence → return early; zero behaviour change.
3. If fence → build `D`, `M`; run §5.3 before `ok=True`.
4. One chokepoint only.
5. Production observations: (a) declare+manifest → ok, (b) declare−manifest → not ok,
   (c) conversation → unchanged, (d) documented rollback (revert the single chokepoint commit).

---

## 10. Open items I am **not** inventing here

| Topic | Status |
|---|---|
| Exact JSON field names on failed `turn_outcome` beyond `ok=False` | Implementation choice |
| Whether fleet packet format embeds the fence via a template helper | Separate packet-authoring task |
| Forcing every executive packet to declare | Out of scope (would break Jesse lane) |
| Re-running full collect `verification` algebra at the seat | Out of scope for v1 |
| Soft-ACK vs hard-fail UX to the user | Product choice; boolean must stay honest |

---

## 11. Revision note vs the one-line sketch

| Sketch | This contract |
|---|---|
| “two files: packet + manifest” | Named F1/F2 with roles |
| “declares deliverables” | Exact fence + YAML fields |
| “matching manifest covers paths” | Coverage + live re-hash |
| “conversational unaffected” | No-fence = no-op |
| Manifest path / path enumeration | Explicit `manifest:` + `paths:` (NEW) |
| Schema floor for post-collect world | Compatible with merged collect (NEW detail) |

