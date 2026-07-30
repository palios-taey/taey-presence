# Taey v2 preservation gate

Task: `taey-production-dcm-v2::preserve-all-reachable-taey-artifacts`

Date: 2026-07-30

This is the public-safe receipt for the recovery gate that precedes Taey
production and DCM reconciliation. It records verification outcomes and code
boundaries without publishing operator paths, network topology, process IDs,
credentials, prompts, transcripts, or recovery contents.

This receipt does not authorize a merge, deletion, service restart, model
promotion, or production cutover. Archive membership is preservation evidence,
not disposition.

## Observed

### Recovery integrity and coverage

- Fourteen Git bundles passed `git bundle verify` from matching Git repository
  contexts.
- Seven dirty or historical tar archives passed non-extracting membership
  validation.
- The recovery manifest and stored artifact set were compared by relative path
  in both directions:
  - listed artifacts: 29
  - stored artifacts: 29
  - listed but absent: 0
  - stored but unlisted: 0
- Every listed artifact path resolved relative to the recovery root.
- Every recorded byte size and SHA-256 matched its stored artifact.
- Final post-fetch bundles contained every current local and configured-remote
  branch ref visible in the DCM and Taey Presence source repositories.
- The final DCM bundle contains reconciliation commit
  `646996a64b2c526cc3eead0becfc13831022896f`.
- The final Taey Presence bundle contains serving-promotion commit
  `aae9ab3c1ae112f0ac08c1e9193fd07b1e34e71a`.

The gate was reopened once after a checksum command masked eight unresolved
manifest paths by adding a directory prefix outside the manifest. The corrected
acceptance test compares the actual relative-path sets before validating sizes
and hashes.

`git bundle verify` must run from a Git repository context with diagnostics
visible. Bundle validity alone does not prove coverage, so branch-ref set
comparison is a separate required check.

### Production surface recovery

- The exact currently served dashboard source and static asset were archived
  and matched byte-for-byte against their live counterparts.
- The browser-served root contained the established UI contracts
  `togglePrompt`, `jesse-raises`, `context_limit_tokens`, `CONTROLS`,
  `PROMPT`, `THINKING`, `ACTIONS`, and `CACHE`.
- Active service overrides matched their archived copies.
- The active operating-prompt delta matched its archived patch.
- The executive inference health endpoint reported healthy model serving,
  healthy liveness accounting, soma connectivity, and the executive plus seven
  supporting seats in its registration scope.

### Seat and transport boundary

- One durable executive seat and seven supporting seat processes were observed
  executing.
- All seven supporting seats had distinct live registrations, roles, process
  generations, prompt-contract hashes, and private event logs.
- `serving/taey_council_seat.py` imports the ordinary
  `serving/taey_seat.py` runtime.
- `serving/manage_council_seats.py` reads `serving/council_seats.json`, loads
  the shared and role-specific prompts, and validates the canonical
  seat-to-role mapping.
- No serving runtime imports `dashboard/native_council.py`,
  `NativeCouncilTransport`, or `RoundLedger`.
- `dashboard/native_council.py` is a separate centralized JSONL round
  coordinator. The unserved council-era dashboard imports it and exposes
  council routes.
- The currently served dashboard does not import that transport.
- The supporting seats use the shared inbox and inference-proxy infrastructure;
  their existence and concurrency do not prove real Neo4j-backed DCM.

## Disposition supported by evidence

- Keep `serving/taey_seat.py`, `serving/taey_council_seat.py`,
  `serving/manage_council_seats.py`, `serving/council_seats.json`, the seven
  role prompts, and the ordinary seat test.
- Replace only the false JSONL deliberation transport, atomically with every
  import, route, and UI claim that depends on it.
- Carry the established served UI contracts into the canonical dashboard.
- Keep active deployment overrides and prompt artifacts until separately
  prepared, committed replacements pass production cutover and rollback gates.
- Keep all recovery artifacts intact until the later production-control phase
  completes.

## Inferred

- Recovery coverage is sufficient to begin reconciliation because current
  branch refs, identified dirty states, served UI sources, active overrides,
  and the operating-prompt delta are all preserved and indexed.
- The supporting seats are reusable concurrent inference workers. They are not
  themselves a peer deliberation mesh.

## Unknown

- Whether the current model artifact is the accepted release candidate; model
  identity and promotion have separate gates.
- Whether the seven role prompts are operator-ratified; role review is a
  separate task.
- How the seats will connect to the public DCM package and Neo4j protocol; that
  integration has not yet been implemented.
- Whether every preserved UI backend operation is portable and publishable;
  this gate proves preservation, not public deployment readiness.

## Reproduction boundary

Public code boundaries can be inspected with:

```bash
rg -n "import taey_seat|ROLE_BY_SEAT|CouncilEventStore" \
  serving/taey_council_seat.py
rg -n "council_seats.json|role_prompt|shared_prompt" \
  serving/manage_council_seats.py
rg -n "NativeCouncilTransport|RoundLedger" \
  dashboard/native_council.py dashboard/app.py
rg -n "native_council|NativeCouncilTransport|RoundLedger" serving
```

The final command is expected to return no serving-runtime dependency on the
JSONL dashboard transport. Operator-local recovery paths and production probes
remain in private completion evidence rather than this public receipt.
