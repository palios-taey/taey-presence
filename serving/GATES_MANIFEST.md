# Gates manifest — what must be green for an artifact to be production

`serving/gates_manifest.json` is the source OUTSIDE the receipt that says which CI
contexts must be green at an entry's `artifact_commit_sha`. It lives here, committed and
guarded by this repo's own CI, precisely so a receipt cannot assert its own gate set.

## Why the set is currently one context

`required_contexts` must be satisfied at **every** production entry's `artifact_commit_sha`,
and a required context that is missing or non-success on any of them is a REFUSE. Measured
against the four current entries:

| context | green on how many of the 4 artifact commits |
|---|---|
| `public-clean` | **4 / 4** |
| `stay-clean` | 2 / 4 |
| `GitGuardian Security Checks` | 2 / 4 |

`stay-clean` and `GitGuardian` are absent from some artifact commits — not failing, simply
never run there, because workflows carry path filters and those commits touched different
paths. Listing them would make entries REFUSE for a reason that has nothing to do with
their health.

**A one-context manifest that is TRUE beats a three-context manifest that refuses
everything.** The set grows as entries' artifacts land on commits that carry more
contexts; it is not a ceiling, and it should be re-measured rather than assumed whenever
an entry's `artifact_commit_sha` moves.

## Actor matching is field-exact

A check run satisfies a context only if `check_run.app.slug` is in `trusted_actors.apps`.
A commit status satisfies one only if `status.creator.login` is in `trusted_actors.logins`.
No other API field is consulted. `logins` is empty because every context we require today
is a check run; an empty list is a deliberate "nothing is trusted here", not an oversight.
