# RELIABILITY REVIEW — commit 812ae829 — first-party code, open mandate

## Nature of this work

This is a correctness and reliability review of OUR OWN code, in our own repository, by
the team that maintains it. The subject is a local command-line utility that records file
sizes and SHA-256 checksums into a JSON manifest. The question is whether it can record a
value that does not match the file on disk when other processes are writing to those files
at the same time.

There is no third-party system involved, no exploitation, and no security research. The
concern is data correctness under ordinary concurrent filesystem activity - the same class
of question as "can this report a stale value if the file changes while we read it".

(An earlier version of this brief used adversarial phrasing - "attack it", "hostile" - which
misdescribed the task. The substance is unchanged; the framing above is the accurate one.)

## Subject

- Commit: 812ae8298cbd313e9e737899f35a05911e22ba16
- Branch: agent/codex-taey-delegate-collect
- Worktree: /home/mira/.peer-worktrees/infra-codex-vslice-collect
- Predecessor you previously reviewed and rejected: 7538c98fc83da7eb953f160b861451612c273569
- Requirements: /home/mira/taey_runs/vertical_slice_prep/02_frozen_work_order.json

## Do not rely on the requester's conclusions

Do not take infra's summary or verification as ground truth. infra's first verification
harness was demonstrably wrong - it used fixed sleeps that elapsed after the command had
already finished, so it reported failures against correct code. Its replacement was then
found by another reviewer to report success for a program that never implemented the
behaviour at all. Assume the current harness may still be wrong in some way not yet found.
Read the source, run what you need, and reach your own conclusions.

Your mandate is open: identify defects. You are not being asked to confirm a fix.

## What changed since your last review

The rewrite adds roughly 166 lines: file handles held open for the duration, advisory
locking via fcntl, stat/fstat fingerprints (device, inode, size, mtime_ns, ctime_ns), a
second read-and-compare pass, an explicit regular-file check, a check that the output path
is not also one of the inputs, and a write transaction local to the tool. None of this has
been reviewed by anyone independent, and new concurrency code is where new defects live.

## Questions to answer

- Does 812ae829 satisfy the frozen requirements, or does it only appear to?
- Can it still record a checksum that does not match the file's bytes at the moment the
  manifest is committed?
- Does the new locking and fingerprint logic introduce problems of its own: deadlock,
  file-descriptor exhaustion with many inputs, failure on filesystems that do not support
  flock, behaviour over NFS, unbounded memory, or a symlink changing between path
  resolution and open?
- Is the local write transaction crash-safe, and does a failure at any point leave a
  previously written manifest untouched?
- Are there inputs for which the command fails to terminate?
- Is anything in the diff outside the declared scope?
- Was the shared helper `atomic_write_text` left unmodified? Verify that independently.

## Also review the verification harness

- /home/mira/taey_runs/vertical_slice_prep/10_race_oracle.py
- /home/mira/taey_runs/vertical_slice_prep/07_supervisor_acceptance_v2.sh

Can the harness report success when the property does not hold? Its trigger reads
/proc/<pid>/io rchar to decide when to modify a file mid-run - is that sound, or can it
fire at the wrong moment? Are its INVALID conditions sufficient? A positive control was
just added (the tool must succeed on an undisturbed run before a failure under
modification counts as detection) - is that control sufficient, and what else would pass
the harness while still being wrong?

## Constraint

Read-only on the worktree: do not modify, commit to, or clean it. Run the tool against your
own temporary fixtures and write only under your own temp directory. Note that
`pip install -e` writes egg-info into a source tree, so install from a copy if you need the
console script.

## Report

Per defect: file:line, a concrete scenario with inputs and the resulting wrong behaviour,
and severity. State what you ran and your verdict plainly. Honest-incomplete is acceptable.
