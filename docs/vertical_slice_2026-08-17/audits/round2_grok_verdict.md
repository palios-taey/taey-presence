# RE-AUDIT — commit 812ae829 (repair) — Grok / LOGOS

| Field | Value |
|---|---|
| Subject | `812ae8298cbd313e9e737899f35a05911e22ba16` |
| Predecessor (rejected) | `7538c98fc83da7eb953f160b861451612c273569` |
| Worktree | `/home/mira/.peer-worktrees/infra-codex-vslice-collect` (READ-ONLY) |
| Fixture | `/tmp/vslice_grok_reaudit_3781180` |
| Verdict | **SPLIT: code ACCEPT-with-residuals; instrument REJECT (false green)** |

## What I ran (Observed)

1. Read frozen work order, reaudit brief, full `cli_taey_delegate.py` at HEAD, `10_race_oracle.py`, `07_supervisor_acceptance_v2.sh`.
2. Confirmed HEAD == `812ae829…`; diff vs baseline touches only scorer path files: `cli_taey_delegate.py` (+ entrypoints/setup from parent commit). **`easy_setup.py` unmodified** (`git diff a027c7f..HEAD -- easy_setup` empty; no `atomic_write_text` import in collect).
3. Independent probes under `/tmp/vslice_grok_reaudit_3781180`.
4. Race oracle ×3 against repair → PASS (hard-fail, no manifest).
5. **False-green probe:** impostor package that fully reads inputs twice then fails for an *unrelated* reason → oracle **PASS** (`longfail_oracle_ec=0`).
6. Mid-hash same-size rewrite of 80MB file without flock → exit 1, no manifest.
7. Exclusive lock held → open/lock fails non-zero.
8. 200 concurrent open fds → exit 0.
9. Hardlink output alias → refused.
10. Injected failure after temp write → prior manifest preserved, temp cleaned.
11. Full `07_supervisor_acceptance_v2.sh` against worktree → **15 passed, 0 failed**.

## Predecessor rejection items vs repair

| Prior defect | Status at 812ae829 (Observed) |
|---|---|
| DEF-1 no write-time re-verify | **Closed for closable races** — second full hash under held fd+flock; `_assert_artifacts_stable` before `os.replace`; oracle mid-run mutate → exit 1, no manifest |
| DEF-2 output aliases artifact | **Closed** — `_assert_output_is_distinct` path equality + `samefile`; alias/hardlink probes refuse |
| DEF-3 dead double-pass | **Gone** — single open path; rehash is verification, not discarded first pass |
| DEF-4 bare RuntimeError from shared helper | **Avoided** — local write transaction; failures are `ArtifactCollectionError`/`OSError` → `ERROR:` contract |
| DEF-5 same-size mid-hash | **Mostly closed** — fstat/path fingerprint (size+mtime_ns+ctime_ns+ino+dev) before/after hash; Observed mid-rewrite fail |
| DEF-6 no S_ISREG | **Closed** — `stat.S_ISREG` on descriptor |
| DEF-7 shared atomic_write destroys prior | **Avoided** — tool-local temp+replace; failure unlinks temp; prior preserved on injected fail |

## New / remaining defects

### I1 — HIGH (instrument) — race oracle false PASS on unrelated hard-fail

**Where:** `/home/mira/taey_runs/vertical_slice_prep/10_race_oracle.py` lines ~112–117:

```python
if proc.returncode != 0:
    if out.exists(): ... FAIL
    print("VERDICT: PASS — detected mid-run drift..."); sys.exit(0)
```

**Scenario (Observed):** Impostor `collect` that (1) reads every artifact fully twice to cross `TRIGGER_RCHAR`, (2) never inspects mutation, (3) exits 1 with synthetic error and writes nothing.

Oracle: mutation triggered, ordering “verified”, **VERDICT: PASS**.

**Why it matters:** T-D1 in `07_supervisor_acceptance_v2.sh` treats oracle PASS as “write-time binding enforced”. A tool that always fails after enough I/O would **green T-D1** without implementing binding. The INVALID gates do not require stderr to mention drift, nor that the tool ever compared pre/post hashes.

**Severity:** HIGH for the *instrument*; does not by itself re-open DEF-1 on 812ae829 (repair independently failed closed on real drift).

### I2 — MEDIUM (instrument) — “ordering verified” overclaims

Oracle prints “mutation preceded commit, and followed first hash” when mut_ns ≤ manifest_mtime (or no manifest) and not the “recorded==post” early case. It does **not** prove the mutation landed between first and second *content* hash of `a.txt` specifically—only that enough aggregate `rchar` elapsed. Repair’s second hash of `a.txt` occurs *before* second pass of `big.bin`; the 1.15×600MB trigger is during/after big’s second pass, so the exercised window is often **after a’s re-verify**, caught by fingerprint stable-check—not by the re-hash equality path. Coverage is real but narrower than the label implies.

### C1 — RESIDUAL / LOW-MEDIUM (code) — non-cooperative writers + fingerprint limits

**Where:** `cli_taey_delegate.py` ~196–200 (comment), `_assert_artifacts_stable` ~155–171, flock LOCK_SH|LOCK_NB ~136–137.

Advisory `flock` does not bind writers that ignore it. Final gate after re-hash is **metadata fingerprint**, not a third full content hash immediately before `os.replace`. Typical writes bump mtime/ctime and fail closed (Observed). A writer that could alter bytes without changing (dev,ino,size,mtime_ns,ctime_ns) would still be out of model—requires exotic/hostile FS ops. Code admits snapshot need. Residual, not a false claim of perfect simultaneity.

### C2 — LOW (code) — no directory fsync after `os.replace`

**Where:** `_write_manifest_transaction` ~211 `os.replace` then return.

Crash after successful replace before parent dir durable may lose directory entry on some power-loss scenarios. Temp was fsync’d; replace is atomic at rename level. Residual durability, not the DEF-7 “replace then verify destroys prior on verify fail” class.

### C3 — LOW (code) — fd count scales with artifact count

**Where:** `collect_artifacts` keeps all handles open until transaction ends.

Observed 200 files OK. Very large N may hit `ulimit -n`. Fail closed on open if exhausted (OSError). Operational limit, not silent wrong hash.

### C4 — INFO — NFS / remote FS flock semantics

Unknown on NFS without live mount test. LOCK_NB may no-op or error depending on server. Policy: fail closed on lock OSError when raised.

## Scope

- Repair commit: **only** `fleet_orchestrator/cli_taey_delegate.py`.
- Parent commit also added entrypoint/setup — in work-order scope for the tool.
- **`atomic_write_text` / `easy_setup.py` not modified** (Observed).

## Work-order satisfaction (own call)

| Requirement | Call |
|---|---|
| Fields from FS read | Met |
| No model/cache values | Met |
| Missing/unreadable/zero → fail, no/unchanged manifest | Met (Observed) |
| sha256 64 lowercase hex | Met |
| Atomic write same-FS temp, fsync, rename | Met (local txn) |
| Re-verify before write | **Met at practical level** (rehash under lock + stable fingerprint before replace); residual C1 |
| Exit 0 only when all recorded | Met for probes; no Observed exit-0 stale manifest on this HEAD |

## Verdict (plain)

1. **Code at 812ae829:** **ACCEPT with residuals (C1–C4)**. Prior HIGH reject items are closed with independent evidence. I did **not** Observe exit 0 with a stale/wrong artifact hash under the race classes I could exercise.
2. **Infra race oracle + T-D1 wiring:** **REJECT / fix instrument**. It can report PASS when write-time binding was never implemented (Observed false green with `longfail` impostor). Do not treat oracle PASS alone as proof of requirement 6 for arbitrary code.

Honest-incomplete: no live NFS trial; no power-loss crash injection; no multi-auditor interaction stress.

## Three-register

| Claim | Register |
|---|---|
| HEAD 812ae829; easy_setup untouched | Observed |
| Repair hard-fails mid-run AAAA→BBBB; no manifest | Observed |
| Oracle PASS on unrelated longfail impostor | Observed |
| Perfect binding vs all non-coop writers | Unknown / residual |
| v2 suite 15/0 on this HEAD | Observed |
