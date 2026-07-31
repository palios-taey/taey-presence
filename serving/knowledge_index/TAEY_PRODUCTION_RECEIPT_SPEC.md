# TAEY_PRODUCTION_RECEIPT_SPEC — "no receipt → refuse"
**Status:** v2.4, 2026-07-31 — v2.3 + the compiled_at_commit equality DROPPED (proven unsatisfiable by infra's two-iteration demonstration during rollout step 4; replaced with ancestry-of-pinned_sha; integrity carried by the blob-hash binding). Closes only on the reviewer's explicit clean verdict. Implementation waits on that verdict.
**Consumes:** TAEY_KNOWLEDGE_INDEX_SPEC (the index is the registry AND the root of trust); the per-surface validation suites; the repos' CI gates.
**Rule being made mechanical (Jesse directive):** the served Taey uses ONLY production infrastructure and must be able to NOT ACCEPT anything else. A 27B cannot judge "is this production" — so no judgment is asked anywhere in this spec. One check, two verdicts, zero interpretation.

## 1. Definition and scope (finding 5)

A **public production capability** is an entry in the compiled index's `sections_present`
with `status: production`. This spec governs EXACTLY that set plus any outward or shared
fleet surface an instruction asks Taey to use (a repo, endpoint, or tool named by anyone,
including operators, that is not already in Taey's verified set for the session).

**Explicitly OUT of scope — the private/local authority path:** the index spec's
`private_slot` / user-private local configuration (its own schema-gated mechanism) and the
seat-local surfaces the served prompt itself provides (displays, a11y bus paths, the seat's
own tools). Those are governed by the index spec's private-slot schema, not by receipts —
Taey does not refuse its own hands. The boundary is mechanical: if the target resolves to a
private_slot entry, the receipt check is NOT invoked; if it resolves to nothing at all, R1
refuses.

**No-override, scoped:** within scope, no override flag exists — the only door to ACCEPT is
becoming a production entry (receipt + index). Humans and operators can always act outside
Taey; what they cannot do is instruct Taey to silently bypass the check. A REFUSE is a
correct outcome; working around one is the failure.

## 2. Root of trust (findings 2, 3)

The chain is: **served prompt → compiled index at its pinned content → entry → receipt**,
verified downward only:

- The index carries, per production entry: `repo{name, public_url, pinned_sha}`, the
  EXISTING `receipts.liveness` path (the index spec's own field — it is THE single receipt
  path authority; this spec introduces NO second path field), `receipts.liveness_sha256`,
  `liveness{probe_cmd, expect}` — ALL compiled, never hand-maintained (SOURCE_MANIFEST
  discipline; hand-edit = index gate failure upstream of this spec). *(Index schema
  additions — `pinned_sha`, `receipts.liveness_sha256`, `generated_at_commit`
  (top-level), and per-entry `artifact_manifest{path, sha256}` — land as an index-spec
  versioned change in the same PR series as the checker; the compiled presence index
  currently lacks all four, which is exactly why rollout §7 sequences index-first. The
  artifact manifest is a canonical-JSON file (sorted keys, no insignificant whitespace,
  UTF-8) listing {path, sha256} per deployed artifact of the entry; its sha256 is over
  those canonical bytes.)*
- The receipt blob is fetched **at the entry's `pinned_sha` via the public API**
  (`GET /repos/{repo}/contents/{entry.receipts.liveness}?ref={pinned_sha}`) — never from a working
  tree, never from an unpinned branch. Its sha256 MUST equal `entry.receipts.liveness_sha256`.
- Therefore a receipt cannot be borrowed, forged, or hand-edited without breaking either
  the index gates (upstream) or the hash equality (here).
- **R0 is defined against the ADOPTED index**: the index bootstrap (per the index spec)
  starts from the shipped snapshot and may adopt a strictly-newer live index after its own
  gates pass; the bootstrap's output is one adopted index object + its content hash. R0
  re-fetches and re-hashes THAT object; inequality with the adopted hash is
  REFUSE: index-stale. The shipped snapshot hash is never compared after adoption — the
  valid-live-newer path is preserved, and no implementer interpretation of "served locator"
  exists.

## 3. The receipt (compiled, never hand-maintained)

```json
{
  "receipt_version": 2,
  "surface_id": "",
  "repo": "OWNER/NAME",
  "artifact_commit_sha": "<the commit of the DEPLOYED ARTIFACT this receipt attests - never the commit containing this receipt file>",
  "artifact_manifest_sha256": "",
  "gates_manifest_ref": "<path in repo at artifact_commit_sha listing required contexts>",
  "liveness": {
    "probe_cmd": "<exactly the entry's compiled probe command>",
    "expect": {"lang": "jq|text", "predicate": ""}
  },
  "index_entry_ref": "",
  "compiled_at_commit": ""
}
```

Binding rules (field equality unless this section names a different mechanical predicate). **The
self-reference is broken by construction**: the receipt's LOCATION authority is the
pinned fetch itself (`entry.receipts.liveness` at `entry.repo.pinned_sha` + blob-hash
equality) — the receipt never stores the SHA of its own containing commit. What it stores
is `artifact_commit_sha`: the commit of the deployed artifact it ATTESTS, which is a
different, earlier commit and therefore committable by normal git. Bindings:
`surface_id == entry.id`, `repo == entry.repo.name`,
`artifact_commit_sha == entry.artifact_commit_sha` (a new compiled index field, added to
the rollout-step-2 set),
receipt blob sha256 == `entry.receipts.liveness_sha256`, `liveness.probe_cmd == entry.liveness.probe_cmd`,
`liveness.expect == entry.liveness.expect`, `index_entry_ref` resolves to the same entry.
`compiled_at_commit` records the head the RECEIPT compiler read, and R2 requires only
that it is an ANCESTOR of (or equal to) `entry.repo.pinned_sha` — never an equality with
`generated_at_commit`. *(v2.4: the former equality was proven UNSATISFIABLE by
demonstration — receipts must be fetchable at pinned_sha, so they are committed before
the index build head exists, so they cannot contain that head's sha; the constraint
translated the mismatch by one commit per iteration forever. Dropping it weakens nothing:
the blob-hash binding `receipts.liveness_sha256` already pins the exact receipt bytes the
index was built against, which is the integrity the equality pretended to add.)*
`generated_at_commit` remains defined as the SOURCE commit the index build read — a parent
of the commit containing the index file, never that commit itself.
`artifact_manifest_sha256` MUST equal `entry.artifact_manifest.sha256`, whose manifest file
(at `entry.artifact_manifest.path`, fetched at pinned_sha) re-hashes to the same value
under the canonicalization of §2 — field, path, format, and algorithm all schema-defined.
Any inequality = REFUSE: binding-mismatch. There is no partial credit.

## 4. The gates check (finding 1)

Required contexts come from a source OUTSIDE the receipt: the repo's committed
**gates manifest** (`gates_manifest_ref`, read at `artifact_commit_sha` via the public API),
itself versioned and guarded by the repo's own CI. Its schema is exact:

```json
{
  "manifest_version": 1,
  "required_contexts": ["<context name>", "..."],
  "trusted_actors": {"apps": ["<app slug>"], "logins": ["<login>"]}
}
```

Actor matching is field-exact: a CHECK RUN satisfies a required context only if
`check_run.app.slug` is in `trusted_actors.apps`; a COMMIT STATUS satisfies one only if
`status.creator.login` is in `trusted_actors.logins`. No other API field is consulted; a
required context whose satisfying object lacks the matching field, or whose value is not
listed, = REFUSE: untrusted-actor. A manifest failing this schema = checker-error
(treated as REFUSE by the caller).
R4 verifies:
- the manifest's context set is NON-EMPTY,
- the statuses/check-runs for **exactly `artifact_commit_sha`** satisfy EVERY manifest
  context, with field-exact success semantics: a COMMIT STATUS satisfies a context when
  `status.context == <name>` AND `status.state == "success"`; a CHECK RUN satisfies one
  when `check_run.name == <name>` AND `check_run.status == "completed"` AND
  `check_run.conclusion == "success"`. No other field is consulted. Actor-matched per the
  schema rule above (no other trust source, no inference),
- extra green contexts are ignored; a missing or non-success required context = REFUSE.
An empty manifest, an unreadable manifest, or gates reported for any sha other than `artifact_commit_sha` = REFUSE.

## 5. The check (Taey-runnable, deterministic)

```
taey-receipt-check <surface_id>
```
Input is a `surface_id` ONLY (finding 2: URL/path inputs invited interpretive resolution).
When an instruction names a URL/path instead of an id, Taey runs the index's compiled
reverse-lookup (`taey-index-resolve <url-or-path>` — exact normalized string membership
against compiled entry fields, no fuzzy matching); a failed lookup is R1 REFUSE by
definition, no judgment involved.

| step | check | REFUSE code |
|---|---|---|
| R0 | fetched index content hash == the ADOPTED index hash (bootstrap output, §2) | index-stale |
| R1 | surface_id is a `status: production` entry in `sections_present` | not-in-index |
| R2 | receipt fetched at pinned_sha; sha256 + equality bindings match (§3); compiled_at_commit ancestor-or-equal to pinned_sha | binding-mismatch / no-receipt |
| R3 | `artifact_commit_sha` reachable from the repo's default branch | unreachable-sha |
| R4 | gates per the committed manifest, exact-set, non-empty, sha-exact, actor-allowlisted (§4) | gate-not-green / untrusted-actor |
| R5 | liveness probe passes its compiled predicate (§6) | not-live |

Output, exactly one JSON line:
`{"verdict":"ACCEPT|REFUSE","surface_id":"","reason":"<R-code|checker-error>","checked_at":"","receipt_sha256":""}`
Exit codes: 0 = ACCEPT, 3 = REFUSE, 1 = checker-error. **Checker-error is REFUSE at the
caller** — the served rule treats any non-zero exit identically; there is no error path
that permits acting.

## 6. Liveness predicates (finding 4 — an executable grammar, not prose)

`expect.lang` is one of exactly two forms:
- `jq`: the probe's stdout MUST parse as JSON and `jq -e '<predicate>'` over it MUST exit 0.
- `text`: the probe's stdout MUST match `<predicate>` as an anchored POSIX ERE
  (`grep -qE`), evaluated on stdout only.
Execution contract: probe runs with a 30s timeout, stdin closed, stderr discarded for the
predicate (captured in the receipt-check log); non-zero probe exit, timeout, parse failure,
or predicate failure are all REFUSE: not-live. A predicate that cannot be evaluated
(bad jq syntax, invalid ERE) is checker-error — which the caller treats as REFUSE.
Status codes alone never appear in predicates (the probe-shape law: bodies, not codes).
Existing prose expectations and old-format liveness receipts (stdout_excerpt/rc) are
NON-CONFORMING by definition and are recompiled during rollout — they never pass as-is.

## 7. Rollout (finding 6 — no consumption before receipts exist)

Ordered, each step gated on the previous; the served prompt changes LAST:

1. This spec: adversarial review → clean verdict.
2. Index schema change — ALL binding fields at once: `pinned_sha`,
   `receipts.liveness_sha256`, top-level `generated_at_commit` (the SOURCE commit the build
   read), per-entry `artifact_commit_sha`, per-entry `artifact_manifest{path, sha256}`,
   and per-entry liveness predicate compilation —
   through the index's own gates. Receipts cannot sequence before their binding fields.
3. `taey-receipt-check` + `taey-index-resolve` implemented beside the index compiler —
   **inert**: installed, runnable, referenced by nothing Taey is told to do.
4. v2 receipts compiled and committed for EVERY current `status: production` entry
   (presence first — its validate suite already emits every ingredient).
5. **Gate: every current production entry returns ACCEPT** from the inert checker, and the
   red-first fixture battery REFUSES all four planted defects (no receipt / stale sha /
   red gate / wrong-shape liveness) with the correct R-codes. Both directions or no rollout.
6. Only then: the served prompt gains the one-sentence rule (§1) and the curriculum's T3
   track adopts the check; the closing production observation is Taey, live, refusing a
   non-production path citing the check verdict verbatim.

## 8. Interaction with HOLD/ESCALATE (curriculum consistency)

A REFUSE: not-live on a surface in known recovery is recorded by the curriculum loop as
`external_outage → HOLD` — the check's verdict is unchanged (still REFUSE; Taey still does
not act); only the curriculum's classification of the episode differs. No receipt-check
verdict ever maps to ESCALATE by itself: escalation is the curriculum's routing for the
task, not a property of the surface check.
