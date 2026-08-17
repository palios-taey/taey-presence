# Operational logs — raw, unedited

Raw output from the runs that produced the verdicts in `../audits/`. Published so the
conclusions can be checked against the machine output rather than against a summary.

| file | what it is |
|---|---|
| `round1_worker_acceptance.txt` | the implementing peer's own acceptance run on the first (rejected) commit |
| `round2_gate_run_FALSE_FAILURES.txt` | **the run that accused correct code.** 14 passed / 2 failed against `812ae829`, where BOTH failures were defects in the supervisor's test harness, not the code |
| `supervisor_suite_v2_first_run.txt` | supervisor suite v2, first run — includes the false T-D1/T-D4 failures |
| `supervisor_suite_on_repair.txt` | supervisor suite on the repair: 15 passed, 0 failed |
| `supervisor_suite_negative_control.txt` | same suite on the known-broken original: 12 passed, 2 failed. If this ever goes green the instrument has broken, not the code |
| `codex_round2_harness.py` | audit harness written by the Codex lens (`conductor-codex`) for round 2 |

## Read `round2_gate_run_FALSE_FAILURES.txt` first

It is the most instructive artifact here. It shows a verification harness confidently
reporting failures against code that had done nothing wrong: the race tests used fixed
`sleep` delays, and hashing 600MB takes 0.27s, so the mutation landed **240–416ms after the
command had already exited and committed.** The test could not express "during the run" at
all, and would have failed a perfect implementation.

The peer under audit refused to change either the code or the gate until event ordering was
measured. That refusal was correct and is the behaviour worth copying.

## Not included, deliberately

`weaver-codex-hooks.log` (334KB) is excluded. It is another seat's peer session log and
references seven distinct tasks, most unrelated to this operation. Publishing it would
expose another seat's unrelated work without its review. Out of scope for this record.
