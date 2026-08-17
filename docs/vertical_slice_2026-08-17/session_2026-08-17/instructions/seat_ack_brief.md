# SEAT ACK GATE — a deliverable-declaring turn cannot record ok=True without a manifest

Chats-ruled substrate patch (a), highest priority of the six. The design is SETTLED - Grok's
two-file shape. Do not redesign it. Implement it.

## The hole, measured

serving/taey_seat.py _run_turn records ok=True as a LITERAL, bound to one condition only:
proxy.ask() returned without raising. The reply content is never examined - not for emptiness,
not for shape, not for truth. A reply asserting that files were written is indistinguishable at
that layer from one where they were.

That is how the 2026-08-16 turn was recorded as a successful completion while reporting three
files under a directory that never existed, with SHA-256 values of 40, 32 and 16 hex characters.

## The requirement

When a turn's packet DECLARES DELIVERABLES, ok=True may only be recorded if a matching manifest
physically exists on disk and covers those declared paths. No manifest, or a manifest that does
not cover them, means the turn is NOT a success.

taey-delegate collect is now merged, installed at /usr/local/bin/taey-delegate, and verified
running in production - Taey itself produced a verified manifest at 20:08 today. So the
mechanical check now has something real to check against.

Scope it to DELIVERABLE-DECLARING packets only. The executive lane also carries ordinary
conversational raises to Jesse; those must be completely unaffected.

## Constraints

- Worktree only, off taey-presence production/main-2907bac2. NEVER the live checkout.
- REVERSIBLE, and state the exact rollback command. This is the live seat Jesse converses through.
- Do NOT change behaviour for turns that declare no deliverables. Prove that with a real turn.
- No new gates, no scaffolding, no extra verification layers. One check, at the point where
  ok=True is recorded.

## Evidence required — production, not tests

1. A real turn declaring deliverables WITH a valid manifest -> ok=True recorded.
2. A real turn declaring deliverables WITHOUT a manifest -> ok=True NOT recorded.
3. A real ordinary conversational turn -> unchanged, still works.
4. The exact rollback command.

Report the commit SHA and those four observations. Do not push or merge.
