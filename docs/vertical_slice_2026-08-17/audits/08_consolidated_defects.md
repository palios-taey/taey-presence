# CONSOLIDATED DEFECT LIST — commit 7538c98f, taey-delegate collect

Two independent adversarial audits, both REJECT. Deduped below. Every HIGH was
independently reproduced by infra in its own shell; provenance is stated per item so
you can weigh each on its evidence rather than on who said it.

Sources:
- Grok lens (conductor-grok): /tmp/vslice_grok_audit_task-03d54456.md
- Codex lens (conductor-codex): /tmp/task-fc737533-audit-report.md
  sha256 f0630ebdfa63bab834cbed5d337a6d307805749f7c2ca380e6348afcaeec29ac (verified by infra)

## CODE DEFECTS — fleet_orchestrator/cli_taey_delegate.py

### DEF-1 HIGH — no write-time binding (frozen requirement 6)
Found by BOTH lenses independently; reproduced by infra with natural timing and no
monkeypatch (600MB second artifact widening the window, first file overwritten mid-run):
exit 0, manifest recorded 63c1dd951ffedf6f7fd968ad4efa39b8ed584f162f46e715114ee184f8de9201
while disk at write time was 4a8d8134f29b0b7b60c126f5532bc9f5d9bb73037373cf6fb872d81f1dcefdfd.
Three independent confirmations. Not in dispute.

### DEF-2 HIGH — output path may alias a declared artifact
Codex lens; reproduced by infra:
  collect $A/thing.txt -o $A/thing.txt  ->  exit 0
  sha before da55d2bf4644e7a7d0e2ac6e889cab3072cb2a2fece3bede8f6b818431f9ce18
  sha after  88c60e56a4a1e8c1dedb45c40802905a166575a7546bfab90f29b545da3fab75
The declared artifact was DESTROYED and replaced by the manifest, and the manifest now
certifies content that exists nowhere on disk. This is strictly worse than a stale hash.
The root-cause shape here is small and simplifying: resolve every artifact path and the
output path, and refuse the run when they intersect.

### DEF-3 MEDIUM — dead first pass in collect_artifacts (lines 65-68)
Both lenses; you already agreed. The first loop discards its results and is not the
requirement-6 re-verification. Deleting it should offset complexity added for DEF-1.

### DEF-4 LOW/MEDIUM — bare RuntimeError escapes the error contract
main() catches only ArtifactCollectionError and OSError; easy_setup.py:129 raises a bare
RuntimeError. Confirmed by infra by reading. Produces a traceback instead of "ERROR: ...".

### DEF-5 RESIDUAL — same-size concurrent rewrite during hashing
Grok lens. bytes_read == getsize cannot detect same-size mutation or a mixed read.
You noted strict simultaneity is impossible across arbitrary files without filesystem
snapshots. Agreed. Close what is closable and state the residue honestly in the code.

### DEF-6 INFO / DISPUTED — no explicit S_ISREG check
Grok raised it; you dispute it as a defect and accept it as info. Infra does not
overrule you. Your call, with a one-line rationale in the commit message either way.

## SHARED-HELPER DEFECT — fleet_orchestrator/easy_setup.py

### DEF-7 HIGH — a failed write can destroy the prior manifest
Codex lens: os.replace happens BEFORE the directory fsync and the read-back verify. If
either post-replace step fails, exit is nonzero but the previous manifest is already
gone, with no rollback. That violates the frozen requirement that a hard failure writes
or modifies nothing.

CAUTION, READ THIS BEFORE TOUCHING IT: atomic_write_text is PRE-EXISTING SHARED code
with other callers in this repo. It is not ours to casually re-shape. Run gitnexus
impact on it first. Your proposed direction - write transactionally inside the tool so
the helper boundary disappears for this path - avoids mutating shared behavior and is
the preferred route unless you find a reason it cannot work. If you conclude the shared
helper genuinely must change, STOP and say so rather than changing it; that is a
conductor-owned decision, not ours.

## DEFECTS IN INFRA'S OWN INSTRUMENT — 05_supervisor_acceptance.sh

Recorded here because the acceptance script is part of the artifact set and its defects
are infra's, not yours. Infra owns these fixes; they are listed so the record is complete.

- DEF-8 HIGH: the script always exits 0. It counts failures then ends on echo, so its
  status is the echo's. A gate that cannot fail its caller is a gate that fails open.
  Confirmed by infra. Fixed in 07_supervisor_acceptance_v2.sh via a final [ "$fail" -eq 0 ].
- DEF-9: happy-path exit status never asserted.
- DEF-10: the byte-fidelity check compared a combined "sha bytes" string, so a change in
  only one field would still pass.
- DEF-11: invokes the module via PYTHONPATH rather than the installed console script,
  so the setup.py entry point is never exercised end to end.

## WHAT INFRA IS ASKING FOR

Fix DEF-1, DEF-2, DEF-3, DEF-4 in one pass. Address DEF-5 as far as it is closable and
document the residue. DEF-6 is your judgment. For DEF-7 prefer the local-transaction
route; do not modify shared helper semantics without stopping first.

6SIGMA applies: the fix should SIMPLIFY. You are removing a dead pass (DEF-3) and adding
a real one, and DEF-2's fix is a refusal, not a branch-around. If your change ends up
larger and more conditional than what it replaces, say so and explain why that is the
correct boundary rather than quietly shipping it.

Do not push, do not open a PR, do not touch the live checkout. Commit in the worktree
and report the SHA plus raw output. Infra will re-verify with an expanded suite that
includes a regression oracle already OBSERVED to fail on 7538c98f, so a fix that does
not actually close DEF-1 will be caught.
