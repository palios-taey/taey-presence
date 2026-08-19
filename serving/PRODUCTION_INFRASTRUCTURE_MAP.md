# Production Infrastructure Map — Taey serving surface

**Status: dated Mira production receipt, not current configuration authority.** Hostnames, IPs, unit names,
absolute paths, and SHAs below are observations from the stated dates. Re-run the documented probes before
making a current production claim.

**Measured 2026-07-30 on Mira, with model-root refresh probes on 2026-08-03.**
Every line here was read from a live system, not from config or memory. Where a value came from
config it is labelled as such, because today proved config and reality disagree. Model roots are
point-in-time observations; re-run the probes before answering "what is running now."

This document exists because five separate failures in one day shared one shape: **a name resolved to
two or three different things, and every fork passed its own health check.** `ep3` meant two models.
`dashboard.app` meant three files. `taey:taey:idle` had two writers. DCM meant two disjoint Neo4j
graphs. One repo had four checkouts. Nothing was broken; everything was forked. A map that records
*which artifact* each name currently resolves to is the only thing that makes that class visible.

---

## 1. The chain, end to end

```
Jesse ──▶ :5001 dashboard ──▶ :8766 Main Taey ──▶ Thor1 10.0.0.8   (executive + 7 council seats)
                                  │
                                  └▶ :8767 delegate ──▶ Thor2 10.0.0.197  (task workers)
```

| component | unit / process | scope | resolves to |
|---|---|---|---|
| dashboard `:5001` | `taey-dashboard.service` | **system** | `taey-presence-validate/dashboard/app.py` |
| Main Taey `:8766` | `taey-soma-proxy-mira.service` | user | `taey-presence-production/serving/soma_proxy.py` |
| delegate `:8767` | `taey-worker-proxy.service` | user | `taey-presence-validate/soma_proxy_mira.py` ⚠ untracked |
| seat | tmux `taey`, pids 437770/437778 | tmux | `taey-presence-production/serving/taey_seat.py` (831 lines, tracked) |
| council seats | 9 processes | — | registered `taey:taey-council-N:{idle,turns_open,seat_registration,machine}` |

**Scope matters.** `taey-dashboard` is a **system** unit; `systemctl --user is-active taey-dashboard`
returns "inactive" and that is not the answer to whether the dashboard is running. Checking the wrong
scope is what led to killing a managed process by hand and leaving the unit in an EADDRINUSE loop.

## 2. Models

Both Thors serve alias `ep3`. Last observed 2026-08-03 from both Mira proxies and both direct Thor
endpoints: root `/models/cpt_repos_v1_servable`.

The 2026-07-30 promotion receipt for `/models/cpt_v7_eps1fix_servable` is historical, not current:
it recorded 55,586,109,904 bytes / 31 files, byte-identical, with real completion verified from each.

Before the 2026-07-30 promotion Thor1 served `cpt_v7_eps1fix_servable` and Thor2 served
`module5_merged` — under the **same alias**, so Main Taey and its own delegate answered from
different models while every health check passed. Promotion did not previously exist as a step;
`serving/promote_model.sh` (commit `305b1c8`) now performs it and `--check` is the standing drift
gate.

**The drift gate compares `root`, never the alias.** Alias equality is exactly what hid the split.

| host | container mount | weights live at |
|---|---|---|
| Thor1 `jetson@10.0.0.8` | `/home/jetson/cpt-artifacts → /models` | `cpt-artifacts/` |
| Thor2 `thor@10.0.0.197` | `/home/thor/serve-models → /models` | `serve-models/` |

SSH is **one-directional**: Thor1 → Thor2 works, Thor2 → Thor1 does not. Copy node-to-node and push
from Thor1; relaying through Mira drops ~112MB/s to ~7MB/s because OpenSSH 9+ routes remote-to-remote
transfers through the local host.

## 3. Capacity — measured, not configured

**8 concurrent sequences on Thor1. Hard cap. Zero headroom for the council.**

The systemd unit sets `VLLM_MAX_NUM_SEQS=128` and that value is **dead config** — `vllm_serve.sh:150`
hardcodes `--max-num-seqs 8`, which the running container confirms.

Measured by issuing 10 concurrent completions and reading vLLM's own scheduler: `Running: 8 reqs,
Waiting: 2`. Client-side request overlap showed 10 and **that number is wrong** — request lifetime
includes queue time, so queued requests are indistinguishable from slow ones from outside.

```bash
ssh jetson@10.0.0.8 "docker logs taey-vllm --since 5m 2>&1 | grep -oE 'Running: [0-9]+ reqs, Waiting: [0-9]+'"
```

Implication: executive + 7 peers = 8 = the whole machine. A ninth concurrent request queues silently.
An amendment redispatch only works if cancellation **actually releases the slot** — confirm the
scheduler's `Running` count falls; a client disconnect is not evidence.

## 4. Liveness

Keys: `taey:<seat>:{idle,turns_open,turn_started,last_activity}` plus, in the production proxy,
`active_turns` / `turn_starts` / `turn_context` / `abandoned_turns` and `taey:soma:*`.

Production's model is **active turn IDs with leases**, not a counter. Its own comment states why:
*"A boolean cannot represent concurrent requests, and a decrement-only counter cannot make duplicate
stream cleanup idempotent."* `idle` and `turns_open` are compatibility projections for fleet-notify.

**Before restarting any proxy:** `redis-cli GET taey:taey:idle` **and** `taey:taey:turns_open`.
`idle` alone has lied — it read 1 while Taey was at tool round 42.

The delegate previously had no `TAEY_SESSION_NAME`, defaulted to `taey`, and therefore wrote **Main
Taey's** liveness key: every delegated turn marked the executive busy, and the worker finishing
declared the executive idle mid-turn. Fixed by `TAEY_SESSION_NAME=taey-worker`.

## 5. Known unmanaged state (violates "no non-production code")

| what | why it matters |
|---|---|
| `SYSTEM_PROMPT_PATH` → `staging/taey-presence-build/…/TAEY_OPERATING_PROMPT.md` | **Production Taey's identity loads from a dirty feature branch.** A `git checkout` there mutates live behaviour. |
| `PERMANENT_KERNEL_PATH` empty | intended kernel layer is not applied |
| `taey-presence-validate/dashboard/__init__.py` | load-bearing, untracked (see §6) |
| `taey-dashboard.service.d/override.conf` | live, no repo home |
| `taey-worker-proxy.service.d/override.conf` | live, no repo home |
| `:8767` runs untracked `soma_proxy_mira.py` | Main Taey runs the repo file; the delegate does not |

Preserved copy-only at `/home/mira/recovery/taey-repos-preclean-20260730/` with `MANIFEST.md`
(sha256, source path, disposition, recovery command per artifact).

## 6. The packaging trap that cost the most time

**A regular package (dir *with* `__init__.py`) beats a namespace package (dir *without*) regardless of
`sys.path` order.** `infra-soul/dashboard/` had one; `taey-presence-validate/dashboard/` did not; the
unit set `PYTHONPATH=/home/mira/infra-soul`. Result: a May-27 dashboard served for days.

Setting `WorkingDirectory` did nothing. Putting the right directory **first** in `PYTHONPATH` did
nothing. Running uvicorn by hand worked — only because a bare shell has no `PYTHONPATH`.

There are **three** `dashboard/app.py` (INDEX_HTML 21,364 / 37,348 / 33,593 chars). Any consolidation
that recreates the package without `__init__.py` reproduces this silently, with green health checks.

**Settle it with one command, never by reasoning about path order:**
```bash
cd <WorkingDirectory> && PYTHONPATH=<unit's value> python3 -c "import dashboard.app as a; print(a.__file__)"
```

## 7. Verification idioms — assert the artifact, never the name

| question | wrong | right |
|---|---|---|
| which model? | alias from `/v1/models` | `root` field + a real completion |
| which code? | filename | `module.__file__` under the unit's env |
| is it running? | `systemctl --user` | check **both** scopes; read `MainPID`, `cwd` |
| how concurrent? | `--max-num-seqs` | vLLM's `Running: N reqs` |
| what config? | the unit file | `/proc/<MainPID>/environ` |
| safe to restart? | `idle` | `idle` **and** `turns_open` |

A health check that compares names passes on a fork. Every gate must name the artifact it asserts.

---

*Ownership: serving surface (this document) is infra's. Repo reconciliation, canonical unit
definitions, and disposition rulings are infra-codex's. Values marked from config are labelled;
everything else was measured live on the date above and should be re-measured, not trusted, later.*
