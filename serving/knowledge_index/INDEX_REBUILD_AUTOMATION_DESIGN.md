# Main-side index rebuild automation — design

**Owner:** infra · **Status:** DESIGN, not implemented. The task rules that the race and loop
guards are designed before anything is built, under the restraint precedent of 2026-07-31.

## The problem it kills

The index's commit fields are *derived from git*. Squash merges mint a new commit, so every
recorded SHA would attest something absent from main's history and G0's ancestry check fails on
the merge result. Plain merges work, but only under strict up-to-date + rebuild-at-head — which
means every index-carrying PR needs a manual "update, rebuild, re-verify" lap before it can land.
That lap is the treadmill.

The permanent shape: **the index on main is always derived from main's own history**, produced by
main itself rather than by whoever happened to open a PR.

---

## Finding 1 — the stated termination premise is not what terminates it

The task says *"rebuild-of-rebuild is a no-op so the flow terminates."* **Measured, that is not
true**, and it matters because it is the safety argument.

At the time of writing, on current main:

```
recorded generated_at_commit  070ee49
main HEAD                     bb39a7a
rebuild at HEAD produces      2 changed files   ← not a no-op
```

`generated_at_commit` is *the head the build read*. So a rebuild produces a diff whenever HEAD has
moved at all — which, after a rebuild commit lands, it has. A rebuild is a no-op only while nothing
has been committed since the last build. **Relying on no-op-ness as the loop guard would produce an
infinite rebuild loop**, each run recording the commit the previous run created.

**What actually terminates it is the paths filter.** A rebuild commit touches `index.json` and
`serving/manifests/*` only — both *outputs* — and outputs are excluded from the trigger, so a
rebuild cannot retrigger itself. Termination is structural, not incidental.

Correctness of the resulting state is unaffected: if the rebuild reads head `S` and lands as merge
`M`, the index records `S`, `S` is an ancestor of `M`, G0 holds, and `--check` recompiles pinned at
the recorded `S` whose sources are unchanged in `M`.

## Finding 2 — the automation cannot push to main, and that is not fixable in the workflow

```
required checks : ["public-clean"]     enforce_admins : true
strict          : false                restrictions   : none
```

A direct push of a new commit is rejected — a commit cannot have passed `public-clean` before it
exists. Observed today on a human push, verbatim:

```
! [remote rejected] main -> main (protected branch hook declined)
```

`enforce_admins: true` admits no exception, so a bot faces the same hook. **The automation must go
through a pull request**, or protection must be weakened for it.

**This is a ruling, not an implementation choice, and it is deliberately left open here:**

| option | cost |
|---|---|
| **A. Bot opens a PR, waits for checks, merges via API** | every rebuild is gate-verified; more machinery (auto-merge is disabled on this repo, so the workflow must poll then merge) |
| **B. Add the bot to `restrictions` / relax `enforce_admins`** | one push, no PR — but the rebuild commit lands on main **never having passed `public-clean`**, and the gate becomes bypassable by whatever holds the token |

Option B trades away the property that made `public-clean` required in the first place — a gate
that went red on this repo *today* and caught a real violation. A treadmill is annoying; a
bypassable gate is the failure the treadmill exists to prevent. **Recommendation: A.** But the
protection posture is conductor's to set, not infra's to quietly relax.

---

## The design (given option A)

```yaml
on:
  push:
    branches: [main]
    paths:                      # SOURCE inputs only — outputs are absent BY DESIGN
      - 'serving/knowledge_index/sections/**'
      - 'serving/validate_presence.sh'        # the liveness oracle
      - <every artifact_paths entry>
concurrency:
  group: index-rebuild-main
  cancel-in-progress: false     # NEVER cancel: a killed rebuild leaves main's index stale,
                                # and stale-but-green is the state the whole chain forbids
```

**Guards, each against a named failure:**

1. **Output paths excluded from the trigger** — the loop guard (Finding 1). `index.json` and
   `serving/manifests/**` must never appear in `paths:`. A future contributor adding them "for
   completeness" reintroduces the infinite loop; the file says so at the trigger.
2. **`cancel-in-progress: false`** — cancelling mid-rebuild abandons main with an index that does
   not match its sources, which `--check` will fail on the next unrelated PR, pointing at the wrong
   culprit.
3. **Actor guard** — `if: github.actor != 'github-actions[bot]'`, defence in depth behind the paths
   filter. Two independent guards because loops are cheap to start and expensive to notice.
4. **No-diff exit** — if the rebuild changes nothing, exit 0 without opening a PR. This is the
   common case for pushes that touch a source file without moving any derived field.
5. **Fetch depth 0** — G0 walks ancestry; a shallow clone passes vacuously, which is the
   fail-open shape these gates exist to kill.

**Flow:** push touching a source input → rebuild at that head → no diff? exit → diff? branch,
commit, open PR, wait for `public-clean` + `index-contract`, merge with a **merge commit** (never
squash, for the reason at the top).

## Evidence required before this is called done

Per the task, and none of it is satisfied by the design alone:

- workflow SHA
- a real merge landing green end-to-end **without a manual rebuild**
- a **red-test**: a source change without a rebuild goes red, and stays red until the automation
  lands the rebuild commit

The red-test is the load-bearing one. An automation that never demonstrably fails is
indistinguishable from one that does nothing.
