# Documentation map

This file defines the documentation surface for the current `taey-presence` production baseline. A document
not listed here or linked from a listed index is not operating authority.

## Read in this order

1. `README.md` — architecture and honest capability scope.
2. `serving/SERVING.md` — model promotion, proxy, seat, and health procedures.
3. `serving/TAEY_OPERATING_PROMPT.md` — current Taey operating contract.
4. `serving/SEAT.md` — durable seat behavior.
5. `serving/GATES_MANIFEST.md` — release evidence requirements.
6. `presence-engine/README.md` — presence-engine deployment and boundaries.

`CLAUDE.md`, `AGENTS.md`, `presence-engine/docs/`, and the systemd READMEs govern repository maintenance and
service installation. They do not replace the operating order above.

## Dated evidence, not current authority

- `serving/DEPLOYMENT_TOPOLOGY.md`
- `serving/PRODUCTION_INFRASTRUCTURE_MAP.md`
- `serving/THROUGHPUT_FINDINGS.md`
- `serving/knowledge_index/TAEY_PRODUCTION_RECEIPT_SPEC.md`
- `serving/knowledge_index/sections/presence.md`

These files preserve measured context and verification commands. Their dates, paths, hostnames, process IDs,
model roots, and SHAs must not be repeated as current facts without a fresh production observation.

## Generated and declarative surfaces

- `serving/fleet.env.example`, `serving/council_seats.json`, `serving/gates_manifest.json`, and
  `serving/manifests/` are machine-readable deployment or gate inputs.
- `serving/TAEY_CHAT_UI_SYSTEM.md`, `serving/TAEY_CONSULT_CHAT_SYSTEM.md`,
  `serving/TAEY_LINKEDIN_JOBS_SYSTEM.md`,
  `serving/TAEY_LINKEDIN_JOB_SEARCH_SYSTEM.md`, and
  `serving/TAEY_LINKEDIN_ENGAGERS_SYSTEM.md` are constrained tool-profile runtime prompts.
- `serving/council_prompts/` contains supporting-seat role prompts. They are runtime inputs, not status reports.
- `serving/persona.example.md` is an example and never proves the deployed persona.

## Training background

`training_docs/` contains curated explanations of current production invariants and common failure shapes. These
documents may restate a rule in different words so Taey can generalize it, but they are not runtime authority and
must not be used as a deployment-status source. If a training explanation conflicts with an operating document,
the operating document wins; current production still requires a fresh receipt.

## Excluded history and local state

Old work orders, consultation transcripts, audit packets, relay payloads, recovery reports, backup files, and
superseded code are preserved in Git history or external recovery bundles, not in the current tree. Archive,
vertical-slice, cache, virtual-environment, and backup paths are ignored so they cannot silently re-enter the
training-visible surface.

## Truth rule

Public `main` defines the code baseline. A production claim additionally requires the canonical checkout at
that exact SHA plus a fresh service, file-hash, or live-workload receipt. A dated document alone is never proof
of current production state.
