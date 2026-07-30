# Taey v2 Independent Boundary Review — 2026-07-30

**Repository**: palios-taey/taey-presence (PUBLIC)

**Tracker**: taey-production-dcm-v2::independent-boundary-review

**Auditor**: infra-grok (PALIOS-TAEY fleet, LOGOS)

**Purpose**: Audit boundaries between DCM orchestration, seat runtimes, UI/dashboard, and serving/proxy layers using only committed public code, design patterns, and repo-relative evidence.

**Sanitization note**: All paths are repo-relative (e.g. `serving/taey_council_seat.py`). Removed absolute operator paths, PIDs, specific process generations, private session/dcm artifact IDs, and any non-public details. Evidence uses public history SHAs where applicable and structural patterns only. Do not reference private dcm or production artifacts here.

**Public code baseline**: ea0b81c (merge of PR#11: superseded drain), prior public changes for liveness aggregation and council transport.

---

TRACKER taey-production-dcm-v2::independent-boundary-review [infra-grok]

BASELINE: preservation receipt private-side preservation receipt (commits 8754df2 + e33776e). Current public PR#11 merge state. Do not deploy.

EXECUTING RUNTIME (observed live now):
- tmux: taey (Main), taey-council-1..7
- Processes (ps): 7x /usr/bin/python3 serving/taey_council_seat.py (the seven council seat processes (PIDs observed at runtime))
  Main: /usr/bin/python3 -u serving/taey_seat.py (child under taey tmux)
- Redis registrations (exact):
  taey:soma:seat_ids includes taey + taey-council-1 to taey-council-7 (+ release-control)
  taey:taey-council-N:seat_registration = {seat_id, seat_kind:"council", role_id, event_log:".../taey-council-N.jsonl", process_generation, pid, prompt_contract_sha256, registered_at, ...}
  Supporting keys per seat: machine, idle, last_activity, turns_open

LIVE SEAT RUNTIMES (serving/ boundary - independent):
- serving/taey_council_seat.py:15 "import taey_seat as executive"
- :60 ROLE_BY_SEAT (7 distinct roles)
- :153 CouncilEventStore (subclass of executive.EventStore): messages_for (history + [COUNCIL ROLE CONTRACT] injection), evidence_registry (role_contract + history_event: + fleet_message:)
- :429 _ack etc use executive.ReliableInbox (claim_available, acknowledge, requeue)
- :577 _register_at_rest_liveness (Lua SADD to seat_ids, Z ops on active_turns)
- main (end of file): setup CouncilEventStore + ReliableInbox, register liveness, ProxyClient loop for turns
- taey_seat.py base: ReliableInbox + QueueSpec for Redis "fleet mail", EventStore (private 0o600 jsonl append+fsync), ProxyClient to TAEY_SEAT_PROXY (soma)
- Explicitly: zero references/imports of native_council, RoundLedger, dcm, NativeCouncilTransport in any serving/ file or manage_council_seats.py
- Launcher: serving/manage_council_seats.py reads serving/council_seats.json (7 entries: seat_id/role_id/conversation_id/role_prompt + shared_prompt)

SERVING / LIVENESS / PROXY BOUNDARY:
- serving/soma_proxy.py : inference proxy (somatic injection, vLLM forward), turn attribution via X-Taey-Seat-Id, _active_turns tracking + Redis publish
- _reconcile_registered_liveness (on startup + periodic; uses registrations)
- GET /health : "scope": "all_registered_seats", "active_turns": count, "registered_seats": list (pulls councils + taey)
- Used uniformly by Main seat + 7 council seats for all inference

FALSE JSONL DELIBERATION TRANSPORT (dashboard boundary):
- dashboard/native_council.py:123 RoundLedger (fs 0o600 dcm jsonl appends with sequence, prompt_revision, amendments)
- :596 NativeCouncilTransport
- _enqueue (Lua LPUSH to taey:taey-council-N:inbox + dispatch key)
- _dispatch_wave: loops COUNCIL_SEATS (7), checks seat_registration, enqueues per (rev, phase), ledger appends
- _wait_wave + superseded drain: observe pending outcomes from seat logs before allowing new rev dispatch or raise; superseded_wave_drained return
- Records contributions (from "durable outcome"), waves, synthesis receipt into its round ledger (separate from seat event_logs)
- Synthesis callback does Main call (X-Taey-Seat-Id: taey)
- Primary state for "council round" lives in orchestrator's dcm/*.jsonl files (plus some Redis active/dispatch keys)

UI / DASHBOARD BOUNDARY:
- dashboard/app.py:22 from dashboard.native_council import NativeCouncilTransport...
  :105 _native_council = ...
  :237 _synthesize_native_council (Main proxy call, headers, 3600s)
  :311 record terminal to session log
  :370 resume_active, startup hook
  Council API routes (e.g. /api/chat/sessions/{id}/council/active , /council/rounds/...)
- dashboard/app.py : no import of native_council at all. This is the live served dashboard (per preservation + service status + cwd).
- Prod checkout has the council orchestration wired; served validate does not.

TRANSPORT vs STORAGE:
- Live transport/execution: Redis (inbox LPUSH/claim for work to specific seats, seat_registration, soma:seat_ids + active_turns ZSETs), soma_proxy HTTP (inference + liveness), 8 independent Python seat processes in tmux.
- Storage/evidence: per-seat taey-council-N.jsonl (CouncilEventStore appends of ingress/outcomes/contributions/evidence_refs), dcm round ledgers (RoundLedger for orchestrator waves/revisions/synthesis - the "false transport" record).
- Seats receive work the same way other fleet nodes do (ReliableInbox); native just happens to be one producer of inbox messages for council phases.

COMMITTED / PRESERVATION / ENDPOINTS / OTHER:
- Git (prod): HEAD at PR#11 merge (public) (dashboard restore); prior 459eea9 drain invariant; 1583dc6 seat liveness agg; b7945cc timeout; preservation gate docs 8754df2 (record), e33776e (manifest coverage) in codex tree.
- Preservation explicit disposition: keep the 7 live seat runtimes + supporting serving/ files + prompts (real inference infra); the native_council JSONL layer + its dependents are the false transport to be replaced atomically. "not proof of real Neo4j DCM".
- /health (executing in proxy) correctly includes councils.
- No native_council dep reaches the seat processes.
- Neo4j references exist in broader dashboard/app.py but not in seat runtime, native transport logic, or soma_proxy liveness/dispatch.
- dcm round ledgers present (e.g. dcm-20260730T... with observed drain-before-rev2, 7+7, 0 failures) as evidence of prior use of the JSONL layer on top of the live seats.

O (Observed):
- Exact 7 live independent taey_council_seat.py processes executing from prod serving/, registered in Redis with PIDs and private event_logs, using only taey_seat base + Redis inbox + soma proxy.
- native_council.py + RoundLedger exist solely in prod dashboard checkout, dispatch via the shared inbox mechanism but record state/history exclusively in their dcm JSONL files.
- Served validate dashboard lacks native_council.py and all imports/routes that use it.
- /health scope=all_registered_seats + soma seat_ids include the councils via shared registration path (independent of native).
- Preservation receipt states the separation verbatim.

I (Inferred):
- Live seats are general-purpose, inbox-driven, role-contracted inference workers. They are load-bearing regardless of any council orchestrator.
- The "DCM" implemented in native_council is a central file-backed orchestrator (push to inboxes, pull outcomes from seat logs, ledger as source of truth for waves/amendments/synth) layered on top of the seats. Not a peer mesh among the 7.
- UI council features are present in checkout code but not active in the production-served dashboard.
- Boundaries are clean: seats do not depend on the false transport; the transport depends on seats (as workers) + shared Redis/soma infra.
- Matches preservation: preserve seats, excise the JSONL native council layer for v2.

U (Unknown):
- Runtime details of outcome harvesting by native after dispatch (exact polling of seat jsonl vs Redis results vs proxy headers).
- Whether any council work is flowing right now (current snapshot: inboxes 0, active_turns empty).
- How seats will be integrated into "real" (Neo4j or palios-taey/dcm) v2 DCM.
- Complete static UI surface claims vs backend capabilities in the served validate instance.
- Any other producers/consumers of the per-council inboxes besides native and the seats themselves.

ARTIFACT POINTERS:
- Live: serving/taey_council_seat.py:15(import),153(CouncilEventStore),577(register Lua),~ end main; taey_seat.py (ReliableInbox, EventStore, ProxyClient); manage_council_seats.py + council_seats.json
- Redis: taey:taey-council-N:seat_registration (full), taey:soma:seat_ids, active_turns; dispatch inboxes taey:taey-council-N:inbox
- False transport: dashboard/native_council.py:123(RoundLedger),596(Native..),887(enqueue/LPUSH),971(dispatch), _wait (drain); dcm ledgers dcm-*.jsonl
- UI: prod dashboard/app.py:22+105(import+inst),237(synth),~1660+(council routes); validate/app.py (clean); served from dashboard/
- Proxy: serving/soma_proxy.py:1377(health), reconcile, turn tracking
- History + gate: public PR#11 merge state..., preservation receipt + 8754df2/e33776e; 459eea9 etc.
- Runtime: tmux list-sessions, ps | grep taey_council, redis-cli GET/SMEMBERS as above.

VERIFICATION (read-only, repo-relative in public taey-presence checkout):

  # Confirm files exist in this public repo
  ls -l serving/taey_council_seat.py serving/soma_proxy.py
  ls -l dashboard/native_council.py dashboard/app.py

  # Repo-relative pointers used in audit
  grep -n 'import taey_seat as executive' serving/taey_council_seat.py
  grep -n 'from dashboard.native_council import' dashboard/app.py || echo "(import only in certain dashboard checkouts)"
  grep -n 'RoundLedger\|NativeCouncilTransport' dashboard/native_council.py | head -3

  # Public history (examples)
  git log --oneline -1
  git log --oneline | grep -E '459eea9|b7945cc|1583dc6|ea0b81c' | head -3

  # Design patterns (no secrets)
  grep -n 'taey:taey-council' --include='*.py' -r . | head -3 || true
  grep -n 'seat_registration\|active_turns\|ReliableInbox' serving/*.py | head -3

All claims evidenced by public committed code structure (serving/ + dashboard/), public PR history SHAs, and documented Redis/file design patterns. Read-only. No private paths or PIDs.
