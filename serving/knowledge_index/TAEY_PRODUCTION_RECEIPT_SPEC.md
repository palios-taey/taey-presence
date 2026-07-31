# TAEY_PRODUCTION_RECEIPT_SPEC — "no receipt → refuse"
**Status:** v1 draft, 2026-07-31. Adversarial review required before any implementation consumes this.
**Consumes:** TAEY_KNOWLEDGE_INDEX_SPEC (the index is the registry); the per-surface validation suites; the repos' CI gates.
**Rule being made mechanical (Jesse directive):** the served Taey uses ONLY production infrastructure and must be able to NOT ACCEPT anything else. A 27B cannot judge "is this production" — so no judgment is asked. One check, three verdicts, zero interpretation.

## 1. Definition

A surface is PRODUCTION if and only if a RECEIPT for it verifies. There is no other
definition. Anything without a verifying receipt — a path, an endpoint, a repo, a tool,
an instruction naming one — is REFUSED by rule, not evaluated on merit.

## 2. The receipt (compiled, never hand-maintained)

Per surface, `receipt.json`, produced by the same compiler discipline as the index
(hand-edit detection identical to the index's SOURCE_MANIFEST rule):

```json
{
  "receipt_version": 1,
  "surface_id": "",
  "repo": "OWNER/NAME",
  "commit_sha": "",
  "artifact_manifest_sha256": "",
  "liveness": {
    "probe": "<one command or GET url>",
    "expected_shape": "<jq-style assertion on the RESPONSE BODY, never a status code>"
  },
  "gates": [{"context": "", "state": "success", "sha": ""}],
  "index_entry_ref": "",
  "compiled_at_commit": ""
}
```

Field rules:
- `commit_sha` MUST be reachable from the repo's default branch (public API check).
- `expected_shape` asserts body content (the probe-shape law: a probe whose output shape
  is not verified is not a measurement; status codes alone never pass).
- `gates` lists the CI/status contexts that guarded the SHA, verified against the public API.
- `index_entry_ref` MUST resolve: a receipt not reachable from the index is itself REFUSED
  (the index remains the single front door).

## 3. The check (Taey-runnable, deterministic)

One command, no arguments requiring judgment:

```
taey-receipt-check <surface_id | url | path>
```

Resolution order: index lookup → receipt fetch → verify, in this exact sequence, each step
fail-closed:

| step | check | on fail |
|---|---|---|
| R1 | target resolves to an index entry | REFUSE: not-in-index |
| R2 | receipt fetches and validates against schema v1 | REFUSE: no-receipt |
| R3 | `commit_sha` reachable from default branch of `repo` (public API) | REFUSE: unreachable-sha |
| R4 | every `gates[]` context = success for that sha (public API) | REFUSE: gate-not-green |
| R5 | liveness probe returns `expected_shape` (body-asserted) | REFUSE: not-live |

Output: exactly one line of JSON:
`{"verdict":"ACCEPT|REFUSE","surface_id":"","reason":"<R-code>","checked_at":"","receipt_sha256":""}`
Exit codes: 0 = ACCEPT, 3 = REFUSE, 1 = checker-error (checker-error is NEVER acceptance;
the caller treats it as REFUSE with reason checker-error — fail-closed at the caller too).

## 4. The rule in the served prompt

One sentence, zero pointers (prompt stays index-only): before using any surface, tool,
path, or endpoint not already verified this session, run the receipt check; act only on
ACCEPT; report REFUSE verdicts verbatim rather than working around them. A REFUSE is a
correct outcome, never an obstacle: working around one is the failure.

## 5. Non-goals + boundaries

- The check does not rate quality, freshness beyond gates, or fitness — only production-ness.
- HOLD-class surfaces (outage mid-recovery) yield REFUSE:not-live — correct; retry rides
  the surface's recovery, never an override.
- No override flag exists. A human wanting Taey to use a non-production surface makes it
  production (receipt + index entry) — the same door everyone uses.
- Private/operator surfaces are simply not in the index → refused by R1 — the disconnection
  boundary and this spec are the same mechanism.

## 6. Rollout

1. This spec: adversarial review → clean verdict (the standing pattern).
2. `taey-receipt-check` implemented beside the index compiler (public repo), with a
   red-first acceptance: a fixture surface with (a) no receipt, (b) stale sha, (c) red gate,
   (d) wrong-shape liveness — all four must REFUSE with the correct R-code; one genuine
   surface must ACCEPT.
3. Per-repo receipts land with repo owners (compiled from each repo's existing gates +
   validation suites — presence first, since its suite already emits every ingredient).
4. The live production observation that closes the box: Taey, through its seat, is offered
   a non-production path and REFUSES it citing the check — observed, logged, re-queryable.
