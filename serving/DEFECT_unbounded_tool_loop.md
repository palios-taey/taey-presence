# Defect spec — soma_proxy.py unbounded tool-call loop

Repo `/home/mira/staging/taey-presence-build` (PUBLIC taey-presence) • file `serving/soma_proxy.py`.
Found during the v-audit: the auditor model looped to **round 536** of tool calls and never produced a
verdict → empty/non-JSON response → callers get `KeyError 'choices'` / `Expecting value: line 1 column 1`.
Real defect (also a DoS/runaway risk for any tool-using deployment). **6SIGMA root-cause, small.**

## The bug
`soma_proxy.py:1177` — the non-stream tool-execution loop is `while True:` with **no round bound**.
A model that keeps emitting tool_calls (search_isma/list_dir/retrieve_document/...) loops forever; the
caller times out / gets an empty body. Line 1111 even `body.pop("max_rounds", None)` — strips any client cap.

## Root-cause fix
Bound the loop and force a final answer at the cap:
1. `MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "8"))` (module-level, env-configurable).
2. In the loop: when `round_num >= MAX_TOOL_ROUNDS`, do ONE final `/v1/chat/completions` call with tools
   removed (drop `tools`/`tool_choice`, or set `tool_choice="none"`) so the model MUST return text, then
   break and return that. Log a warning that the cap was hit (`log.warning("tool-round cap %d hit; forcing final answer", MAX_TOOL_ROUNDS)`).
3. Keep the normal break (no tool_calls → final) unchanged.

This guarantees a bounded, always-returns-text response. Default 8 is ample for legit multi-search
grounding while killing runaways.

## Production verification (no unit-test suite)
On the live auditor (Thor2 :8765, full-parity), re-issue a complex judging call that previously looped
(e.g. an epistemic/religion/mathematical_reality probe) and confirm: it returns a SCORE within <=8 tool
rounds, non-empty `choices`, no `Expecting value` error. Then the v-audit subset re-run completes clean.
