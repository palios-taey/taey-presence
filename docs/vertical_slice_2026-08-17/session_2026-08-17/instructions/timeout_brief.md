# ALIGN THE TIMEOUTS — Gemini Patch 3, taey-presence, no gates

Measured defect, still live, independent of PR #319. Do not wait on anything.

## The defect

  serving/taey_seat.py:36     TAEY_SEAT_TIMEOUT       1800s (30 min)
  dashboard/app.py:389,2348   stream timeout          3600s (60 min)
  serving/soma_proxy.py:53    VLLM_REQUEST_TIMEOUT    5400s (90 min)

Three different ceilings, and NONE cancels the layer below. The SHORTEST belongs to the
layer that REPORTS the outcome. Observed 2026-08-17: an executive turn opened 00:05:01Z, the
dashboard 3600s ceiling fired at 01:05:01Z and recorded assistant_failure/ReadTimeout, and
the engine was STILL GENERATING at 01:29, stopping only when the proxy 5400s ceiling fired at
01:35:01Z. Thirty minutes of GPU burned on a turn already reported failed.

## Requirement

When the reporting layer gives up, the upstream generation must be CANCELLED, not orphaned.
The requirement is upstream cancellation, not merely making the numbers equal - equal numbers
with no cancellation still orphans work on a race.

Root-cause shape preferred: if the reporting layer closes/aborts the request in a way vLLM
observes and stops generating, that is better than a longer chain of guards.

## Constraints

- Worktree only, off taey-presence production/main-2907bac2. Do NOT touch the live checkout.
- REVERSIBLE and PROVEN reversible: this is the live serving path Jesse converses through. If
  it regresses, we must be able to put it back in one command. State exactly how.
- Do not change the executive conversational path's behaviour other than cancellation.
- PRODUCTION EVIDENCE REQUIRED, not a test: demonstrate on the real serve that when the
  reporting layer times out, the engine stops. Show the engine-side observation (running
  request count dropping, or the generation ending) - not just that the caller returned.

## Report

Commit SHA, the exact rollback command, and the production observation showing upstream
generation actually stopped.
