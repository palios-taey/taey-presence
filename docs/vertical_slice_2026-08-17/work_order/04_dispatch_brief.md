VERTICAL SLICE WORK ORDER - implement taey-delegate collect.

READ THE FROZEN WORK ORDER FIRST, from disk, byte-exact:
/home/mira/taey_runs/vertical_slice_prep/02_frozen_work_order.json

That file is FROZEN. Implement it exactly as written. Do not paraphrase it, do not add requirements, do not drop any. If you believe a requirement is wrong, STOP and say so - do not silently improve it.

ISOLATION - work ONLY here:
/home/mira/.peer-worktrees/infra-codex-vslice-collect
It is a disposable worktree detached at a027c7f73f5e9309eb3e6664a9e3ea6114b2e31d. Do NOT touch the live checkout at /home/mira/claude-code-fleet-orchestrator. Do NOT touch taey-presence. No service restarts.

REPO CONVENTION for a new CLI in this repo, follow it rather than inventing a shape: a module fleet_orchestrator/cli_taey_NAME.py, a matching function in fleet_orchestrator/script_entrypoints.py, and a console_scripts line in setup.py. See cli_taey_task.py and cli_taey_plan.py as the existing examples.

WHEN DONE: commit in the worktree, then run all five acceptance tests listed in the work order INCLUDING the failure paths, and keep the raw unedited output. Report READY to infra with the commit SHA and that raw output.

Your prose will not be trusted. infra will independently re-run git status, git rev-parse HEAD, git diff against the baseline, every acceptance test, and will re-hash the manifest with its own sha256sum and wc -c in its own shell. A number that did not come from the disk will be caught at that step - that disagreement is the acceptance criterion.

WHY THIS EXISTS: a production turn reported completion with a manifest of three files whose SHA-256 values were 40, 32 and 16 hex characters, pointing at a directory that never existed. The model wrote the manifest instead of the filesystem. This tool removes the model from that path.
