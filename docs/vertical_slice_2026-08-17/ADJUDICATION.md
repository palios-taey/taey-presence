# ADJUDICATION — commit 812ae829, two lenses, split verdict

**infra, 2026-08-17.** Both re-audit lenses examined the same commit and reached opposite
verdicts. This document adjudicates. It does not split the difference.

## The two verdicts

| lens | verdict | on the disputed window |
|---|---|---|
| conductor-grok | **ACCEPT** with residuals | C1, LOW-MEDIUM: "residual, not a false claim of perfect simultaneity" |
| weaver-codex (gpt-5.5) | **FAIL** | HIGH: "still permits commit-time mismatch" |

## They do not disagree about the facts

Both located the identical structural fact: `cli_taey_delegate.py:210-212` performs
`_assert_artifacts_stable()` and then `os.replace()`, and a writer may mutate an
already-checked artifact in between. They disagree only on whether that blocks acceptance.

## infra reproduced it independently

Forced interleave (explicitly artificial - the mutation is injected inside `os.replace`,
i.e. at the last possible instant after the final check has passed):

```
exit=0  manifest_written=True
recorded       = 63c1dd951ffedf6f7fd968ad4efa39b8ed584f162f46e715114ee184f8de9201
disk_at_commit = 4a8d8134f29b0b7b60c126f5532bc9f5d9bb73037373cf6fb872d81f1dcefdfd
matches_disk_at_commit = False
```

Identical values to weaver-codex's report. **The manifest certified content that was not on
disk, and exited 0.** That is a produced artifact, not an inference.

## The finding is REAL and IRREDUCIBLE

Measured window between the final stability check and the rename, 5 runs:
`[9.1, 7.7, 8.6, 3.6, 3.2]` microseconds, median **~7.7us**.

It cannot be closed:

- `_assert_artifacts_stable` issues `fstat`/`stat`; `os.replace` issues `rename`. They are
  separate syscalls.
- POSIX offers no compound atomic for "verify N files AND rename".
- Moving the check adjacent to the rename NARROWS the window. It cannot reach zero.
- A post-rename re-verify does not close it either: the post-check has its own window, and
  it reintroduces the DEF-7 class (prior manifest already replaced when drift is found).

**Every implementation that certifies mutable files has this window.** A verdict of FAIL
against it is a verdict against the possibility, not against this commit.

## Therefore the defect is in the WORK ORDER, not only the code

Frozen requirement 6: *"re-verifies each file immediately before writing the manifest so the
recorded state matches the state at write time."*

The first clause is satisfied. The second clause - *recorded state matches state at write
time* - is **unachievable** for files that arbitrary processes may write. The work order
demands a guarantee the substrate cannot provide.

This is the 6SIGMA answer: the fix is upstream, in the contract, not downstream in more
locking. More locking is the patch shape - it adds machinery and still cannot deliver.

## Options, for the work-order owner to decide (NOT infra's call)

1. **Restate the guarantee honestly.** The manifest certifies *state observed at time T*,
   where T is recorded (`generated_at` already exists). Achievable, verifiable, and true.
2. **Constrain the input domain.** Require artifacts be quiesced during collection - which
   matches the actual production use, where a delegated work product is not being rewritten
   by a competing process. Then the window is unreachable in practice rather than merely
   narrow.
3. **Accept and document the residue**, as the code comment at 207-209 already does.

Option 1 or 2 is a root-cause shape. Option 3 alone leaves a doc that promises more than
the code can deliver, which is the failure class this whole exercise exists to kill.

## infra's verdict

- **On the code:** the repair closes every defect that CAN be closed. Grok's severity call
  is the better-calibrated one; weaver-codex's is correct on fact and, in my judgement,
  miscalibrated on severity, because it treats an irreducible property as a fixable defect.
- **On the work order:** requirement 6 is defective as written and needs an owner decision.
- **On shipping:** NOT pushed. Not because the code is unsound, but because the artifact
  would ship against a requirement it cannot satisfy, and I will not record a "meets the
  frozen work order" claim that is false in its second clause.

## Sixth instrument finding, mine

weaver-codex: `10_race_oracle.py` "exercises second-pass/final-stability detection, not the
post-final-check/pre-rename gap". Correct. My oracle mutates during an ADJACENT window and
never touches the disputed one, so its PASS was never evidence about this gap.

The correction has a trap in it: a test for an irreducible window FAILS every possible
implementation, so it must be a **characterization probe, not a gate**. Wiring it as a gate
would recreate the original T-D1 error - a red that accuses code of not doing the
impossible. It is added as non-gating and labelled.
