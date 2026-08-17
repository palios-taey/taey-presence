# Independent adversarial audit — task-03d54456 (Grok / LOGOS)

| Field | Value |
|---|---|
| Subject commit | `7538c98fc83da7eb953f160b861451612c273569` |
| Branch | `agent/codex-taey-delegate-collect` |
| Worktree | `/home/mira/.peer-worktrees/infra-codex-vslice-collect` (READ-ONLY) |
| Baseline | `a027c7f73f5e9309eb3e6664a9e3ea6114b2e31d` |
| Work order | `/home/mira/taey_runs/vertical_slice_prep/02_frozen_work_order.json` |
| Auditor fixture | `/tmp/vslice_grok_audit_3670069` |
| Verdict | **REJECT / CHANGES REQUIRED** — not a clean bill |

## What I ran (Observed)

1. Read frozen work order + supervisor acceptance script + full source of `fleet_orchestrator/cli_taey_delegate.py` and `atomic_write_text` in `easy_setup.py`.
2. Confirmed worktree HEAD == `7538c98…` and diffstat vs baseline (3 files, +120 lines).
3. Independent probes under `/tmp/vslice_grok_audit_3670069` via:
   `env PYTHONPATH=<worktree> python3 -m fleet_orchestrator.cli_taey_delegate collect …`
4. Monkeypatch race to demonstrate write-time mismatch (no worktree mutation).
5. Concurrent rewrite / special-file probes.

I did **not** trust infra’s summary. I did **not** modify the worktree.

## Requirement map (own assessment)

| # | Requirement | Assessment |
|---|---|---|
| 1 | Fields from exists/getsize/sha256 over bytes | **Mostly met** for artifact records |
| 2 | No model/arg/cache values in artifact fields | **Met** (path is resolved from declared path; hash from open/read) |
| 3 | Missing/unreadable/zero-byte → non-zero, no/unchanged manifest | **Met** in probes |
| 4 | sha256 exactly 64 lowercase hex | **Met** (stdlib hexdigest + regex) |
| 5 | Atomic write: same FS temp, fsync, rename | **Met** via `atomic_write_text` (temp in parent, fsync, replace, dir fsync) |
| 6 | Re-verify each file **immediately before writing** so recorded state matches write time | **NOT MET** |
| 7 | Exit 0 only when all found, hashed, recorded | **Met for sequential happy path**; **violated under post-hash mutation** because exit 0 with stale hash (see D1) |

## Defects

### D1 — HIGH — Re-verify-before-write absent; exit 0 with stale/wrong manifest

**Where:** `fleet_orchestrator/cli_taey_delegate.py:65-68` (`collect_artifacts`), `71-80` (`cmd_collect`)

**Code shape:**
- `collect_artifacts` hashes twice (discard first pass results), returns list.
- `cmd_collect` builds JSON from that list, then `atomic_write_text` with **no re-read of files**.

**Work order text (requirement 6):** re-verify each file **immediately before writing** so recorded state matches state at write time.

**Concrete failure (Observed):**
```
# After hashes returned, mutate race.txt from AAAA → BBBB, then write
manifest_sha = 63c1dd95…  (AAAA)
disk_after_write = 4a8d8134… (BBBB)
MISMATCH_AT_WRITE_TIME True
exit 0
```
Manifest claims a filesystem state that is false at write completion.

**Severity:** HIGH vs frozen work order and vs the original production failure class (manifest not bound to FS at commit time).

### D2 — MEDIUM — Double-pass is not write-time re-verify (and first pass is dead work)

**Where:** `cli_taey_delegate.py:65-68`

First loop only gates existence; returned values come solely from second loop. Any mutation **after** the second loop and **before** rename is uncaught (D1). The double pass may look like “re-verify” in a summary; it is not requirement 6.

### D3 — LOW/MEDIUM — `main` does not catch `RuntimeError` from atomic verify

**Where:** `cli_taey_delegate.py:106-111`; `easy_setup.py:127-129`

`atomic_write_text` can `raise RuntimeError` after `os.replace` if post-write verify fails. `main` only catches `ArtifactCollectionError, OSError` → traceback, exit 1, and the destination path may already have been replaced. Failure path is messier than “ERROR: …” hard-fail contract.

### D4 — LOW (residual) — Same-size concurrent rewrite during hashing

**Where:** `cli_taey_delegate.py:40-52`

Size check (`bytes_read != size`) catches shrink/grow class races when they change length relative to the initial `getsize`. Same-size content replacement during multi-chunk read can still yield a digest that is neither pure pre nor pure post image (classic TOCTOU). Not fully closed; acceptance never stresses it.

### D5 — INFO — Special files

On this Linux, `/dev/zero`, FIFO, `/dev/null` hit `size <= 0` and hard-fail (Observed). That is good here, but the tool never rejects non-regular files explicitly (`S_ISREG`). Portability/behavior elsewhere is Unknown.

## What works (Observed — do not over-claim)

| Probe | Result |
|---|---|
| Happy path N=2, independent sha256/bytes | PASS |
| Missing file: exit 1, prior manifest content+mtime unchanged | PASS |
| Ghost path never existed: exit 1, no ghost manifest | PASS |
| Zero-byte: exit 1, no manifest | PASS |
| Directory: exit 1 | PASS |
| Unreadable (chmod 000): exit 1 | PASS |
| Dangling symlink: exit 1 | PASS |
| Spaces / newline in filename: hash OK | PASS |
| Symlink to file: resolves via realpath, hashes target | PASS |
| Atomic helper: same-dir temp + fsync + replace | PASS (code review + reuse) |
| Diff scope | Only cli module + entrypoint + setup console script — **in scope** |

## Attack on supervisor acceptance (`05_supervisor_acceptance.sh`)

| Gap | Why it matters |
|---|---|
| Never mutates files **between hash and write** | A tool that skips write-time re-verify (this one) still PASSes all five frozen tests + zero-byte extra |
| Never concurrent writer during hash | Mixed/stale digest class invisible |
| Never non-regular files (except zero size by accident) | Explicit `S_ISREG` policy untested |
| Never unreadable / directory | Covered by implementation luck + extra zero test only partially related |
| Ground-truth compare only for happy three files | Good anti-hardcode for those paths; does not prove write-time binding |
| mtime unchanged check | Good for “did not rewrite on failure”; does not prove atomic durability under crash |
| No crash/full-disk simulation | Atomic path not adversarially proven by the script |
| **Would pass while still broken:** | Implementation identical to HEAD: double-hash then write without re-open/re-hash immediately before rename — **exactly D1** |

## Verdict

**REJECT / CHANGES REQUIRED.**

The tool correctly addresses the original “model-invented SHA” scandal for the static sequential cases the supervisor script covers. It does **not** satisfy frozen requirement 6 (re-verify immediately before write), and I Observed exit 0 with a manifest whose sha256 did not match the file bytes on disk at write time.

**Minimum fix direction (not implemented — READ-ONLY mandate):** after building candidate records, re-open each path, re-hash, require exact match to candidate `bytes`+`sha256`, then atomic write; on mismatch hard-fail without writing. Optionally reject non-`S_ISREG` files explicitly.

**Honest incomplete:** full crash-injection durability of `atomic_write_text` under ENOSPC not exercised; same-size mid-hash mixed digest not forced to a pure mixed sample in this session (nondeterministic timing).

## Three-register summary

| Claim | Register |
|---|---|
| HEAD is 7538c98; source as quoted | Observed |
| D1 exit-0 stale manifest | Observed (fixture race) |
| Acceptance script would green this artifact | Observed (script logic + local happy/missing/zero paths) |
| Mixed same-size hash always achievable | Inferred (algorithm); not forced here |
| Portable special-file policy | Unknown without explicit regular-file check |
