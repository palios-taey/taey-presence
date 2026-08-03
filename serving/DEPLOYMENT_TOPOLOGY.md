# Deployment topology — what actually runs, where, from which commit

`SERVING.md` and `README` describe the code and the architecture. This file describes the
**deployment**: which commit runs, out of which directory, under which unit, on which venv, and
what proves it is alive.

Every line below was **measured on 2026-07-31**, with model-root and checkout refresh probes on
2026-08-03, not recalled. Where a fact is inferred rather than observed it says so. Numbers here go
stale — re-measure with the commands given rather than trusting the values.

---

## 1. The production checkout

    tree     /home/mira/taey-presence-production
    commit   20c7c780bf9997d72027be79f1adf83d79250e5f
    state    clean; one local commit ahead of origin/main when observed 2026-08-03
    dirty    0
    venv     <tree>/.venv           built from requirements.txt
    git      linked worktree; .git is a FILE pointing at the parent repo's worktrees dir

Everything on Mira runs from that one tree. There is no second production checkout.

    git -C /home/mira/taey-presence-production rev-parse HEAD
    git -C /home/mira/taey-presence-production status --porcelain

---

## 2. Mira services — unit → exec → venv

All six execute `<tree>/.venv/bin/python` against a path inside the same tree.

| unit | scope | executes | port |
|---|---|---|---|
| `taey-soma-proxy-mira` | user | `serving/soma_proxy.py` | 8766 |
| `taey-worker-proxy` | user | `serving/soma_proxy.py` | 8767 |
| `taey-presence-engine` | user | `presence-engine/engine.py` | — |
| `taey-dcm-presence` | system | `presence/dcm_presence.py` | — |
| `taey-prediction-worker` | system | `presence/prediction_worker.py` | — |
| `taey-soma` | system | `soma/mira_soma.py` | — |

**Scope matters.** Three are `--user` units and three are `--system`. A `systemctl status` without
`--user` reports the user units as not-found, which reads exactly like "not running". Query both:

    for u in taey-dcm-presence taey-prediction-worker taey-soma; do systemctl is-active $u; done
    for u in taey-soma-proxy-mira taey-worker-proxy taey-presence-engine; do systemctl --user is-active $u; done

Each service's tree/venv binding comes from a `zzz-production-canonical.conf` drop-in. Drop-ins
apply in **lexical order, later wins** — the `zzz-` prefix is load-bearing, because earlier
drop-ins point at other trees. Read the effective value, never reason about precedence:

    systemctl --user show taey-soma-proxy-mira -p ExecStart -p Environment

---

## 3. Serving nodes — and the `/models` trap

Both Thors serve the alias `ep3`, `Restart=always`, `enabled`, unit `taey-ep3.service`.

| node | mount → `/models` | served root | mem avail |
|---|---|---|---|
| thor1 `10.0.0.8` | `/home/jetson/cpt-artifacts` | `/models/cpt_repos_v1_servable` | 5 G / 122 G |
| thor2 `10.0.0.197` | `/home/thor/serve-models` | `/models/cpt_repos_v1_servable` | 6 G / 122 G |

**`/models` is a container path and the two nodes map it to different host directories.** A
`/models/...` literal cannot be true of both nodes at once, and `ls /models/...` on a *host*
returns nothing on either — the path exists only inside the container. Resolve the mount, never
assume it:

    docker inspect taey-vllm --format '{{range .Mounts}}{{.Source}}->{{.Destination}} {{end}}'

**`ep3` is a permanent alias, not a set of weights.** The artifact behind it is swapped at every
promotion. The only true answer to "which weights are served" is the `root` field read at the
moment you ask:

    curl -s http://10.0.0.8:8000/v1/models | jq -r '.data[0].root'

`taey-ep3.service` runs `bin/gpu-cleanup.sh 90 120` as `ExecStartPre` (`ignore_errors=no`, so it
gates the start) and `sync; echo 3 > drop_caches` as `ExecStopPost`. Both Thors are 128 GB
**unified memory** — weights come from the same pool as RAM, and swap is exhausted on both, so a
second large model on a serving node OOMs rather than degrades. `OnFailure=` is **not** set on
either node; auto-recovery is `Restart=always` only.

---

## 4. Live endpoints — canonical vs deprecated

Measured 2026-07-31:

| endpoint | role | status | latency |
|---|---|---|---|
| `127.0.0.1:8766` | **canonical** — Taey's seat, proxies to thor1 | 200 | 0.004 s |
| `127.0.0.1:8767` | **canonical** — delegate, proxies to thor2 | 200 | 0.017 s |
| `127.0.0.1:8089` | **canonical** — embedding (`isma-core/server.py`) | 200 | 0.001 s |
| `127.0.0.1:8095` | **canonical** — ISMA retrieval | 200 | 0.001 s |
| `127.0.0.1:5002` | **canonical** — orchestrator plan/task API | 200 | 0.003 s |
| `localhost:11434` | **NOT Taey** — separate ollama install, small models | — | — |
| ISMA `/v2/*` | **deprecated for prose** — partial shadow; `/v2/search/adaptive` is V1-backed and fine | — | — |

The two proxies are **pinned, not load-balanced**: `:8766` reaches thor1 and `:8767` reaches
thor2, each via `VLLM_BASE_URL`. Neither fails over. `TAEY_SESSION_NAME` differs (`taey` vs
`taey-worker`) so the delegate does not overwrite the seat's liveness keys.

---

## 5. Liveness signal per service

| service | signal |
|---|---|
| proxies | `GET /v1/models` → 200, and `System prompt loaded from …` in the unit journal at start |
| Taey's seat | Redis `taey:taey:turns_open` / `turn_started` / `last_activity`; `Turn start`/`Turn end` in the `:8766` journal |
| serving nodes | `GET :8000/v1/models` returns `id=ep3` **and** the expected `root` |
| embedding | `GET :8089/health` → 200 |
| orchestrator | `GET :5002/api/projects` → 200 |

**A restart is safe only at `turns_open == 0`, checked immediately before** — not minutes before.
A turn opened between the check and the restart is a lost answer with no error anywhere:

    redis-cli -h 127.0.0.1 GET taey:taey:turns_open

The proxy reads its system prompt **once at startup** (`serving/soma_proxy.py`, the `startup`
handler). There is no hot reload: editing the prompt file changes nothing until the unit restarts.

---

## 6. Measured capacity

Serve flags on both nodes:

    --max-model-len 262144  --gpu-memory-utilization 0.92  --kv-cache-dtype fp8
    --max-num-seqs 8  --max-num-batched-tokens 8192
    --reasoning-parser qwen3  --enable-auto-tool-choice --tool-call-parser qwen3_xml
    --enable-prefix-caching

Runtime: vLLM 0.19.0, transformers 4.57.3, torch 2.10.0, compute capability sm_110.

`--max-num-seqs 8` is the real concurrency ceiling. `--kv-cache-dtype fp8` means production
quantizes the KV cache — any off-node A/B that runs bf16 KV is internally valid but its numbers do
not transfer to production behaviour.

**Observed turn cost:** a tool-using turn on Taey's seat ran 6 rounds over **16 minutes**, with
~109 KB of tool output (~27 k tokens) re-injected across rounds; three single tool returns hit a
30,042-char cap. Depth is what makes turns slow, and a browser client will time out long before
the turn completes — the answer is still produced, with nowhere to land. See
`THROUGHPUT_FINDINGS.md` for throughput measurements.

---

## 7. Verifying this document

Wrong way → right way, for each claim class:

| do not | do |
|---|---|
| `systemctl status <unit>` | query **both** scopes; `--user` units answer not-found in system scope |
| trust a `/models/...` path | resolve the per-node mount first |
| trust the served alias | read `root` from `/v1/models` |
| `pgrep -fc <pattern>` | `ps -eo args= \| grep -c "[p]attern"` — `pgrep -f` matches its own command line |
| enumerate by `find … \| head` | ask the registry (`gitnexus list_repos`, `git ls-files`, `rg --files`) |
| conclude a file is absent | confirm you are in the right namespace — container vs host, user vs system, worktree vs repo |

Each of those was a real wrong answer produced on this system, not a hypothetical.
