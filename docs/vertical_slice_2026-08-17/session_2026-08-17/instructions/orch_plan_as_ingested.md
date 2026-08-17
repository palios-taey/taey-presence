# Project: taey-delegation-loop - Enable the Taey delegation loop
> Chats-specified work to get Taey orchestrating real delegated work with physical receipts. Runway: ~2 weeks from 2026-08-17. Source: Claude Chat (Fable 5 Max) synthesis of four independent responses, routed by Jesse. Author nothing new; execute the list. If a step fails in production, ROLL BACK and take it to the Chats — do not build scaffolding around it.

## Phase: p1-land-collector - Land the collector [order: 1]

### Task: gatekeeper-r5 - Gatekeeper reviews PR #319 head 95d8378 and posts its own audit/gatekeeper status [priority: 10] [owner: gatekeeper]
- Session revived from 13-day dormancy 2026-08-17. Reviewer posts its own attestation; infra must never post it.
- audit/grok already success. ship-gate-acceptance and readme-as-a-stranger already pass.

### Task: merge-319 - Conductor merges PR #319 once r5-audit-gate is green [priority: 10] [owner: conductor] [depends: gatekeeper-r5]
- Conductor's repo, conductor's call. Standing offer: merges instantly when the gate goes green.

### Task: pull-production - Pull merged collector to the production checkout [priority: 15] [owner: infra] [depends: merge-319]
- Evidence: commit SHA on the live checkout plus `taey-delegate collect --help` running from the installed console script.

## Phase: p2-receipted-carry - The receipted carry, THE production result [order: 2]

### Task: carry-packet - Taey carries one real careers/revloop packet end to end with receipts [priority: 10] [owner: taey] [depends: pull-production]
- The lane that produced the 2026-08-16 fabrication. A CLI produces one real packet in a disposable worktree; Taey runs `taey-delegate collect` on the declared paths via run_command; Taey carries packet plus manifest to the authoring Chat over the receipted relay; Chat accepts or corrects.
- Exercises the loop with Taey in the middle. This is training trajectory #1, paired against the Aug 16 fabrication as its rejected sample.
- LEAN: no wait-states inside a single seat turn. Send in one turn; harvest on the consult-complete signal in the next. The signal already exists and already targets taey.
- Evidence: extract path plus independently re-hashed SHA, destination observe, manifest produced by the tool, Chat's response carried back.

### Task: relay-receipts - Relay receipts in-tree for each Chat surface used [priority: 20] [owner: taey] [depends: carry-packet]
- DONE-criterion 1. Extract SHA, destination observe, no-send proof, per surface.
- Known measured defect to clear: on Claude, clicking send_button returns ok:true and does NOT send.

## Phase: p3-substrate - Substrate patches, Chats-ruled order [order: 3]

### Task: seat-ack-gate - Seat ACK gate: a deliverable-declaring turn cannot record ok=True without a matching manifest [priority: 20] [owner: infra] [depends: pull-production]
- Horizon authors the frozen order; Grok's two-file shape. Closes the false-completion hole at serving/taey_seat.py.
- Evidence: an in-tree test proving the seat cannot record ok=True on a deliverable packet without a manifest (DONE-criterion 3), plus a production turn.

### Task: schema-lock - Schema lock via the existing response_format path, scoped to deliverable-declaring packets [priority: 30] [owner: infra] [depends: seat-ack-gate]
- Ships flag-gated, DEFAULT OFF. The executive lane also carries conversational raises to Jesse; a task-status grammar would break that lane.
- Produce a measured A/B proving a conversational raise still round-trips BEFORE the flag is offered for turn-on.

### Task: schema-scoping-ratify - Jesse ratifies the schema-lock scoping and default flip [priority: 30] [owner: infra] [depends: schema-lock]
- Human-review gate. Infra builds, measures and proves rollback; the flip on the lane Jesse converses with is his.

### Task: return-contract-rule - Return-contract template rule: never ask the model for values only the filesystem can produce [priority: 40] [owner: infra] [depends: pull-production]
- Replaces Gemini's Patch 4 as written. VERIFIED 2026-08-17: there is NO prose in TAEY_OPERATING_PROMPT.md demanding hashes; the only SHA line (:125) is the CORRECT extract-receipt discipline and deleting it would remove good behavior. The demand came from the request, not the prompt.
- Deliverables are declared paths; collect supplies the numbers.

### Task: timeout-align - Align seat/dashboard/proxy timeouts so the reporter's timeout cancels the upstream generation [priority: 50] [owner: infra] [depends: pull-production]
- Measured: seat 1800s, dashboard 3600s, proxy 5400s. Shortest belongs to the layer that reports the outcome, and nothing cancels the layer below.
- Reversible, with rollback proven before it goes live.

### Task: thinking-ab - Thinking-ON A/B on one deliverable-packet class [priority: 60] [owner: infra] [depends: seat-ack-gate]
- Either restores the standing serve directive or produces the measurement justifying the deviation.

### Task: submit-status-adapter - Typed submit/status/collect adapter on the taey-presence tool surface [priority: 70] [owner: infra] [depends: carry-packet]
- LAST, per the ruling: only after Taey has done at least one run_command-driven collect.

## Phase: p4-objective-done - Mechanical DONE checklist [order: 4]

### Task: trajectory-bundle - One full Chat -> Taey -> CLI -> collect -> Chat trajectory bundle with zero unexplained discrepancies [priority: 20] [owner: taey] [depends: carry-packet]
- DONE-criterion 2.

### Task: site-driver-receipts - Dated validation receipt per named site surface under the driver contract [priority: 60] [owner: taey] [depends: relay-receipts]
- DONE-criterion 4. LinkedIn, SalesNav, Upwork, X, and the rest. Required before Taey runs any site process unattended.

### Task: three-site-runs - Three real site-process executions by Taey with per-action receipts and zero fabricated claims [priority: 70] [owner: taey] [depends: site-driver-receipts]
- DONE-criterion 5. When this closes, the video is a replay of the record rather than a demo.

## User Stop Conditions
- stop_when_all_ready_tasks_dispatched
