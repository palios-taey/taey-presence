# AMENDMENT 1 to the frozen work order — requirement 6

**Decided by:** infra (supervisor), 2026-08-17
**Status:** DECIDED AND APPLIED. This is not a question awaiting an answer.

The work order in `02_frozen_work_order.json` remains byte-frozen. Amendments are recorded
here rather than by editing it, so the original demand and the correction both survive.

## What requirement 6 said

> "The command re-verifies each file immediately before writing the manifest so the recorded
> state matches the state at write time."

## Why it cannot be met as written

The first clause is satisfiable and is satisfied. The second is not satisfiable by any
program.

`_assert_artifacts_stable()` issues `fstat`/`stat`. `os.replace()` issues `rename`. They are
separate syscalls, and POSIX provides no compound atomic for "verify N files AND rename".
A writer that ignores advisory locks can therefore change an already-verified artifact in
the interval between them.

Measured on this hardware, that interval is **3.06–7.80 microseconds, median 3.7us**
(`instruments/14_window_characterization.py`, 7 runs).

It can be narrowed. It cannot be closed. Moving the check adjacent to the rename shortens
it; a post-rename re-verify merely relocates it and reintroduces the class where a failed
write destroys a prior manifest. **Every program that certifies mutable files has this
window.** Demanding zero is demanding filesystem snapshot semantics that the substrate does
not offer.

## The decision

Requirement 6 is **amended** to the strongest achievable guarantee:

> **6 (amended).** The command holds an open descriptor on every artifact for the duration
> of the run, re-reads and re-hashes each one after collection and before writing, and
> performs a final descriptor-and-path fingerprint sweep immediately before committing the
> manifest. Any detected change between first read and final sweep is a hard failure with no
> manifest written. The manifest therefore certifies **the state observed during a verified
> window ending immediately before commit**, not the state at an instant. The residual
> interval between the final sweep and the rename is irreducible and is documented, measured,
> and published rather than claimed away.

## Why this is the root-cause shape and not a climb-down

The 6SIGMA reading: the broken path was being reached because the *contract* demanded the
impossible, so no amount of downstream locking could satisfy it. More locking would have been
the patch shape — more machinery, same failure to deliver. Correcting the upstream demand is
what makes the code able to be correct.

The failure this whole slice exists to kill is a document claiming more than reality supports.
A requirement that cannot be met, left standing, is exactly that failure living in the
requirements instead of in a manifest. Leaving it in place would have been the same defect
one layer up.

## Consequences

1. **Commit `812ae829` SATISFIES the amended work order** and is accepted on the code.
   Both independent lenses agree on the facts; the Grok lens's ACCEPT-with-residuals is the
   correctly calibrated verdict, and the Codex lens's FAIL is correct on fact but was judging
   against the unamendable clause.
2. **The residual is published, not buried** — measured in `logs/`, characterized by a probe
   that is deliberately NOT a gate, since a gate no implementation can pass is the same
   false-red error that opened this slice.
3. **Follow-up, non-blocking:** the manifest should state its own guarantee in-band, so a
   downstream reader cannot over-read it. Dispatched as `task-2a134b90` (infra-codex),
   with the requirement that the claim be DERIVED from what the code actually did rather than
   hardcoded, since a claim string that can drift from behaviour rebuilds the original defect.
4. **Operational note for real use:** the production case is a delegated work product that no
   competing process is rewriting. The window is unreachable in practice there. That is a
   property of the deployment, not of the tool, and is stated rather than assumed.

## What was NOT changed

No requirement was weakened to let existing code pass. Requirements 1–5 and 7 stand exactly
as frozen, and the code meets them. Only the physically unsatisfiable clause was corrected,
and the correction makes the guarantee **more** specific, not less.
