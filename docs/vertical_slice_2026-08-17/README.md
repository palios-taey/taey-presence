# Vertical slice, 2026-08-17 — delegation with physical evidence

The complete evidentiary record of one day: a tool built to stop a language model from
inventing file hashes, audited independently four times, repaired once, and **not shipped**,
because the requirement it was built against turns out to demand something the operating
system cannot provide.

Preserved here so it is auditable rather than scattered across `/tmp` and a local worktree.

## Why this exists

On 2026-08-16 a production turn reported completion with an "Artifact Inventory" listing
three files under a directory that did not exist, with "SHA-256" values of 40, 32 and 16 hex
characters. SHA-256 is 64. No tool call in the entire audit had ever created that directory.
**The manifest was written by the model instead of by the filesystem.**

The slice removes the model from that path: a mechanical collector reads the disk, and the
supervisor verifies by re-deriving every number rather than reading a report.

## The subject code is in another repository

`taey-delegate collect` lives in **`palios-taey/claude-code-fleet-orchestrator`**
(`fleet_orchestrator/cli_taey_delegate.py`), at commit
**`812ae8298cbd313e9e737899f35a05911e22ba16`**, branch `agent/codex-taey-delegate-collect`.
At the time of writing that commit is **local only and unpushed**. This directory is the
record *about* that code, not the code.

## Layout

| path | what it is |
|---|---|
| `README.md` | this file |
| `ADJUDICATION.md` | **start here** — two lenses split, and how it was resolved |
| `COSMOS_SURVEY_PACKET.md` | the read-only forensics that opened the slice |
| `work_order/01_probe_a_relay.md` | Phase 1 relay canary (Taey drove it; passed) |
| `work_order/02_frozen_work_order.json` | **frozen** requirements for the collector |
| `work_order/03_claude_supervisor_prompt.md` | the supervisor procedure followed |
| `work_order/04_dispatch_brief.md` | brief handed to the implementing peer |
| `audits/round1_grok_verdict.md` | REJECT — no write-time re-verification (+4 more) |
| `audits/round1_codex_verdict.md` | REJECT — alias destroys artifact; and the supervisor's own gate could not fail |
| `audits/round2_grok_verdict.md` | code ACCEPT w/ residuals; **instrument REJECT** (false green) |
| `audits/round2_worker_raw_gate.txt` | raw gate output from the repairing peer |
| `audits/06_audit_brief.md` | round-1 brief (open mandate, don't-trust-me clause) |
| `audits/08_consolidated_defects.md` | round-1 findings deduped across both lenses |
| `audits/09_impact_analysis.md` | why the GitNexus gate could not resolve, and what replaced it |
| `audits/11_reaudit_brief.md` | round-2 brief that **tripped a provider content filter** |
| `audits/12_reaudit_brief_codex.md` | the same review, framed accurately; this one worked |
| `instruments/05_supervisor_acceptance.sh` | acceptance v1 — **DEFECTIVE**, kept as evidence |
| `instruments/07_supervisor_acceptance_v2.sh` | current suite (15/15 on 812ae829) |
| `instruments/10_race_oracle.py` | event-synchronized race oracle: INVALID verdict + positive control |
| `instruments/14_window_characterization.py` | measures the irreducible window. **Not a gate** |

The round-2 Codex verdict is summarised in `ADJUDICATION.md`; it arrived as a notification
rather than a file, so unlike the other three there is no verbatim artifact to preserve.

## Verdict history

| round | lens | verdict |
|---|---|---|
| 1 | Grok | REJECT — no write-time re-verify, +4 |
| 1 | Codex | REJECT — output could alias and destroy an input; post-replace failure destroyed prior manifest |
| 2 | Grok | code ACCEPT with residuals C1–C4; **instrument REJECT** |
| 2 | Codex (gpt-5.5) | code FAIL — post-final-check/pre-rename gap; harness blind to it |

## The requirement-6 decision — DECIDED

Frozen requirement 6 asked that the manifest reflect *"the state at write time."* The measured
window between the final stability check and the rename is **3.06–7.80us, median 3.7us**, and
it is **irreducible**: `fstat`/`stat` and `rename` are separate syscalls and POSIX offers no
compound atomic for "verify N files and rename". Every implementation certifying mutable files
has this window.

The defect was therefore in the work order, not only in the code, and it has been
**amended** — see `work_order/AMENDMENT_1_requirement_6.md`. The manifest certifies *the state
observed during a verified window ending immediately before commit*, and the residual interval
is measured and published rather than claimed away.

**Commit `812ae829` satisfies the amended work order and is accepted on the code.** No
requirement was weakened to let it pass; only the physically unsatisfiable clause was
corrected, and the replacement is more specific, not less.

## What went wrong on the supervising side

Kept deliberately, because it is the most transferable part.

The code under audit was wrong **twice**. The instruments measuring it were wrong **seven
times**, all mine:

1. a race test using fixed sleeps that fired 240–416ms *after* the command had exited — it
   accused correct code, and I reported its output as a reproduction
2. a false green hiding inside the fix for that
3. an `os.sync()` inflating a timestamp by ~200ms, producing spurious INVALID
4. an acceptance script that counted failures then exited 0 — a gate that could not fail
5. a suite blind to the very window under dispute
6. a watcher matching stale scrollback instead of the event
7. a harness leaking **13G** of `/tmp` — the same filesystem as `/var/spark`, so it was
   consuming the knowledge-graph disk headroom another seat was raising alarms about

The through-line: *diligent about production, credulous about my own tooling.* The counters
that worked were structural, never care — a third verdict (`INVALID`) distinct from pass and
fail, a positive control, A/B against a known-broken build and a known-impostor build, and
cleanup registered at creation rather than at each of eight exit paths.
