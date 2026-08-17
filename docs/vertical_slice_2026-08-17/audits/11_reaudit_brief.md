# RE-AUDIT BRIEF — commit 812ae829 (the repair) — open mandate

## Subject

- Commit: 812ae8298cbd313e9e737899f35a05911e22ba16
- Branch: agent/codex-taey-delegate-collect
- Worktree: /home/mira/.peer-worktrees/infra-codex-vslice-collect
- Predecessor (REJECTED by both of you): 7538c98fc83da7eb953f160b861451612c273569
- Baseline: a027c7f73f5e9309eb3e6664a9e3ea6114b2e31d
- Frozen requirements: /home/mira/taey_runs/vertical_slice_prep/02_frozen_work_order.json

## DO NOT TRUST THE REQUESTER

Do NOT take infra's word, summary, or verification as ground truth. infra's FIRST
verification instrument was proven wrong during this cycle - it used fixed sleeps that
fired after the command had already exited, and it rendered FAIL verdicts against code
that had done nothing wrong. Assume infra's second instrument may be wrong too, in a
way infra has not noticed. Read the source, run what you need, reach your own
conclusions. If you cannot access something, say so and BLOCK rather than ruling on a
description.

Your mandate is OPEN and adversarial: FIND DEFECTS. You are not being asked to confirm
that a fix works. "It looks correct" without evidence of independent examination is a
non-answer.

## Context you need, stated as neutrally as I can

This commit is a rewrite of a predecessor that you both rejected. It adds roughly 166
lines: retained file descriptors, advisory locking via fcntl, stat/fstat fingerprints,
a re-read-and-compare pass, an explicit regular-file check, an output/artifact path
distinctness check, and a manifest write transaction local to the tool. None of that new
machinery has been examined by anyone independent. New concurrency code is exactly where
new defects live.

The predecessor's rejection items were: no write-time re-verification; an output path
allowed to alias a declared artifact; a dead double-pass; a bare RuntimeError escaping
the error contract; a same-size mid-hash race; and an absent S_ISREG policy.

## What to determine

Questions, not a checklist.

- Does 812ae829 actually satisfy the frozen work order, or does it appear to?
- Where can it STILL emit a manifest value not derived from the file's bytes at write time?
- Does the new locking/fingerprint machinery introduce defects of its own - deadlock,
  fd exhaustion on many artifacts, lock inversion, failure on filesystems without flock,
  behavior on NFS, unbounded memory, symlink races between resolve and open?
- Is the new local write transaction genuinely crash-safe, and does a failure anywhere in
  it leave a prior manifest untouched?
- Can the tool be made to hang rather than fail?
- Is anything in the diff outside the declared scope?
- Was the shared helper `atomic_write_text` left unmodified? (It was declared off-limits;
  verify that independently rather than believing it.)

## Also audit infra's replacement instrument

infra's first instrument was defective and its replacement is:
`/home/mira/taey_runs/vertical_slice_prep/10_race_oracle.py`
plus the suite `/home/mira/taey_runs/vertical_slice_prep/07_supervisor_acceptance_v2.sh`

Attack them. Can the oracle report PASS when the property does not hold? Is its trigger
(/proc/<pid>/io rchar threshold) sound, or can it fire at the wrong moment? Are its
INVALID conditions sufficient, or are there ways for it to draw a conclusion from a trial
that never exercised the window? What would pass it while still being broken?

## Hard constraint

READ-ONLY on the worktree. Do not modify, commit to, or clean it. A second independent
auditor is working the same artifact concurrently. Run the tool only against your own
temp fixtures, write only under your own temp directory, and note that `pip install -e`
writes egg-info into a source tree - install from a copy if you need the console script.

## Report

Per defect: file:line, a concrete failure scenario, severity. State what you ran, and
your verdict plainly. Honest-incomplete is acceptable. A false clean bill is not.
