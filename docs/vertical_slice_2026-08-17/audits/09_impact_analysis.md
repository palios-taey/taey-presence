# IMPACT ANALYSIS — required gate, satisfied two different ways

infra-codex correctly FULL-STOPPED because `gitnexus_impact` could not resolve
`_artifact_from_disk`, `collect_artifacts`, or `cmd_collect`. Stopping was the right
call. The root cause is not a broken tool and not a broken worktree.

## Root cause

The GitNexus index is built from the indexed tree of `claude-code-fleet-orchestrator`.
`fleet_orchestrator/cli_taey_delegate.py` exists ONLY in an unmerged commit inside a
disposable worktree. An index cannot contain a symbol that is absent from the tree it
indexed. The gate did not fail; it was asked about something outside its world.

## Part 1 — new symbols: blast radius established by direct enumeration

The gate's PURPOSE is to know what breaks before editing. For a brand-new module that
purpose is answerable exactly, by enumerating every reference in both trees.

| symbol | refs in LIVE checkout | refs in worktree | where |
|---|---:|---:|---|
| `_artifact_from_disk` | 0 | 3 | all inside `cli_taey_delegate.py` |
| `collect_artifacts` | 0 | 2 | all inside `cli_taey_delegate.py` |
| `cmd_collect` | 0 | 2 | all inside `cli_taey_delegate.py` |
| `taey_delegate_main` | 0 | 2 | its own def + the setup.py console-script line |
| `cli_taey_delegate` | 0 | 1 | the `_run_callable` string in script_entrypoints.py |

Command:
`grep -rn "<sym>" <tree> --include='*.py' --include='*.cfg' --include='*.toml' | grep -v '\.git/'`

**Blast radius of the three collector symbols: the file itself. Nothing outside it
references them. Risk NONE.** This is not an assumed nil — it is an enumerated nil.

## Part 2 — the symbol where the gate has real teeth

`atomic_write_text` IS in the index, and DEF-7 proposes changing its ordering.
`gitnexus_impact(target=atomic_write_text, direction=upstream, includeTests=true)`:

```
risk: MEDIUM      impactedCount: 16      direct callers: 8
processes affected: 2   (cmd_uninstall, tests/easy_setup_acceptance.py:main)
```

Direct callers (d=1): `atomic_write_json`, `atomic_restore_settings_text`, `_mutate`,
`apply_claude_permission_guard`, `remove_claude_permission_guard`,
`restore_claude_settings_backup`, `reconcile_pending_hook_transaction`, and the
acceptance test main.
d=2: `cmd_uninstall`, `save_setup_state`, `write_pid_record`, `ensure_claude_integration`.
d=3: `_post_install`, `update_setup_state`, `enable_services`.

**Reading:** this helper underpins install/uninstall, settings persistence, the Claude
permission guard, hook-transaction reconciliation, and PID records of a PUBLIC product.
Altering its write ordering to fix a manifest tool would ripple into all of it.

**Therefore DEF-7 is confirmed off-limits via the shared helper.** The tool must write
its manifest transactionally itself. That was already the preferred route; the impact
numbers now make it the required one.

## Standing instruction

Do NOT run `npx gitnexus analyze` inside the disposable worktree to "fix" the index.
The index is shared infrastructure owned by conductor, keyed by repo. Re-analyzing from
a worktree path risks re-pointing or polluting the index the whole fleet reads. If a
worktree-aware index is genuinely wanted, that is a conductor decision, raised as its
own task, not a side effect of this repair.
