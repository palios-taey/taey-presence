# Serving a model on a Spark or Thor

The presence layer (face / prediction / interrupt / memory / soma + dashboard) talks to
an **OpenAI-compatible chat-completions endpoint**. You bring that endpoint. This directory
is the production serving glue we run on NVIDIA hardware so a cold clone can stand the whole
thing up — model **and** presence — end to end.

Three pieces:

1. **`vllm_serve.sh`** — serves your model as a raw vLLM endpoint (`:8000`).
2. **`soma_proxy.py`** — sits in front of vLLM on `:8765`, injects your persona, publishes
   soma telemetry to Redis, and (optionally) wires `search`-style tools. This is the endpoint
   you point the presence workers at (`VLLM_URL=http://<host>:8765/v1/chat/completions`).
3. **`taey_seat.py`** — optional durable executive loop hosted in tmux. It receives
   fleet-notify mail, keeps completed conversation turns across restarts, and calls the proxy
   with attributable event/correlation headers.

You can run just vLLM (`:8000`) and skip the proxy if you don't want persona/soma/tools.

---

## Hardware reality (read this first)

- **Thor (Jetson AGX Thor, aarch64)** — serve via the **pinned NVIDIA Jetson vLLM Docker image**.
  It bundles vLLM + torch built for aarch64; **there are no wheels to install**, just pull the image.
- **Spark (GB10, aarch64)** — run vLLM natively from its own aarch64 build. The `vllm serve ...`
  argument block in `vllm_serve.sh` is identical; drop the `docker run` wrapper.
- **UMA memory note (Jetson):** GPU memory is unified with system RAM. Killing a vLLM process or
  `docker rm` does **not** always release the allocation — if `free -g` shows little available after
  a stop, **reboot** to reclaim it before serving again. (Do not `rmmod`/`modprobe` — reboot.)
- A 35B-A3B MoE in bf16 needs ~67–70 GB; int4 (AWQ/GPTQ) ~19 GB load, ~28 GB peak. Pick the
  quantization that fits your board's UMA budget.

---

## Thor (Jetson) — quick start

```bash
# 1. one-time: pull the image that bundles vLLM + torch for aarch64 (~72 GB)
docker pull ghcr.io/nvidia-ai-iot/vllm:latest-jetson-thor

# 2. put an HF model dir on the host, then serve it
export TAEY_MODEL_PATH=/path/to/your/model-dir   # required; dir is mounted at /models
export VLLM_PORT=8000                             # optional (default 8000)
export VLLM_GPU_UTIL=0.85                         # optional
./serving/vllm_serve.sh                           # raw vLLM on :8000

# 3. (optional) front it with the soma proxy for persona + soma telemetry + tools
export VLLM_BASE_URL=http://127.0.0.1:8000
export SYSTEM_PROMPT_PATH=./serving/persona.example.md   # your persona file
export PROXY_PORT=8765
python serving/soma_proxy.py                       # OpenAI-compatible on :8765

# 4. point presence at whichever endpoint you chose
export VLLM_URL=http://127.0.0.1:8765/v1/chat/completions   # (or :8000 for raw vLLM)
# ...then start the presence workers / dashboard as in the top-level README.

# 5. optional: give fleet-notify a durable tmux-hosted Taey seat
export TAEY_SEAT_PROXY=http://127.0.0.1:8765/v1/chat/completions
export TAEY_SESSION_NAME=taey
export TAEY_CONVERSATION_ID=main
tmux new-session -d -s taey 'python3 serving/taey_seat.py'
```

The tmux pane is not conversation storage. The seat reconstructs context from
the canonical executive JSONL on every turn and fsyncs its attributable outcome
there before acknowledging fleet mail. By default that file is
`~/taey_sessions/main.jsonl`; a dashboard session adapter using the same file
adds UI turns to the seat's next context and renders autonomous seat outcomes
after a refresh.

The sessions directory must be private (`0700`) and every executive JSONL must
be private (`0600`). Dashboard and seat readers reject symlinks and
group/world-accessible paths instead of trusting or appending to them. Tighten
the permissions of an older deployment before launching current code; changing
those mode bits does not rewrite or truncate the event log.

### Seven supporting local council seats

The supporting seats use stable numeric runtime identities and separate semantic
role IDs:

| seat ID | role ID |
|---|---|
| `taey-council-1` | `context-memory` |
| `taey-council-2` | `evidence-reality` |
| `taey-council-3` | `systems-dependencies` |
| `taey-council-4` | `adversarial-failure` |
| `taey-council-5` | `scope-intent` |
| `taey-council-6` | `options-alternatives` |
| `taey-council-7` | `control-acceptance` |

Validate and inspect the exact runtime configuration before launching:

```bash
python3 serving/manage_council_seats.py validate
python3 serving/manage_council_seats.py render
```

Then point every seat at the same attributable proxy/model used by Main Taey and
launch:

```bash
export TAEY_SEAT_PROXY=http://127.0.0.1:8766/v1/chat/completions
python3 serving/manage_council_seats.py launch
python3 serving/manage_council_seats.py status
```

The shared proxy route is the model authority. `TAEY_MODEL` remains a request
compatibility selector when explicitly supplied, but `soma_proxy.py` removes it
before forwarding to its single loaded vLLM model. Promoting a new release through
`promote_main_model.sh` therefore moves Main and all seven supporting seats
together; seat identities, prompts, inboxes, and histories do not need to be
rebuilt or restarted for each release.

`launch` refuses to proceed if any canonical council tmux session already exists;
it never restarts or adopts an unknown process. By default, private seat logs live
under `~/taey_sessions/council/`, one 0600 JSONL per seat. Override that root with
`TAEY_COUNCIL_SESSIONS_DIR`. Each log reconstructs only that seat's mutable
history. Supporting outcomes carry the seat, role, event, request, correlation,
round, and prompt-revision lineage available in the inbound envelope and remain
`conversation_visible=false`; Main Taey is the only UI answerer.

Each inference request also carries a runtime-issued `evidence_registry` containing
the fixed role-contract hash, attributable current fleet-message IDs, and the IDs of
prior successful outcomes in that seat's durable history. The strict response schema
and the post-generation validator both restrict `evidence_refs` to those exact
identifiers. An unregistered reference fails the turn, requeues its claimed mail, and
is never acknowledged as a successful contribution.

The launcher starts `taey_council_seat.py`; it does not branch Main's
`taey_seat.py` runtime. At startup, a supporting seat atomically publishes
`idle=1` only when its attributable
`active_turns` set is empty. A non-empty set fails closed as busy. This closes the
first-wake gap without making the compatibility boolean authoritative. The same
atomic transition publishes a generation-specific `seat_registration`; the
launcher requires a new identity-matched generation before it reports a seat
started, so stale `idle` state cannot certify a dead or prior process.

## Running a fleet: deploy, swap models, and the checks that gate each step

The quick start above stands up ONE node by hand. Once a node carries real traffic, every step
below exists because doing it by hand went wrong in a specific way, and each is a command rather
than a habit — a habit is what lapses at 2am.

```bash
# WHAT IS ACTUALLY RUNNING vs WHAT THIS REPO HOLDS. Mutates nothing; exit 1 on drift.
./serving/deploy_thor.sh --check <user@host>

# INSTALL from this repo to the node. Does NOT restart: the running process keeps serving from
# the copy it exec'd, so the change lands now and applies at the next start, which you schedule.
./serving/deploy_thor.sh <user@host>

# SWAP THE MODEL. Changing the artifact forces a decision about the served id — the deploy
# REFUSES without one, because a caller addressing the old id would otherwise get HTTP 200 and
# different weights, silently.
./serving/deploy_thor.sh --model-path /models/<new> --served-name <new-id> --restart <user@host>
#   --served-name <id>   a node serving a CANDIDATE its peers lack -> stale callers get a clean 404
#   --keep-served-name   a fleet-wide PROMOTION -> every caller of that id should move together

# PROMOTE AN ALREADY-SERVED RELEASE INTO MAIN TAEY. This waits for zero open turns
# across Main and every registered supporting seat,
# writes the endpoint drop-in, restarts the UI-facing proxy, verifies the exact model/root
# through that proxy, runs one real inference, and emits a JSON release receipt. A failed
# CONTROL gate restores the previous route automatically.
./serving/promote_main_model.sh \
  --endpoint http://<serving-host>:8000 \
  --model <new-id>
```

**Served id vs weights.** The served name is a stable alias chosen at launch, which is exactly why
it is useless as evidence of what is loaded — it stays constant when the weights change. Read the
`root` field of `/v1/models`, or `journalctl -u taey-ep3 | grep 'Serving model:'`. Two nodes
answering to one id over two different weight sets is the failure to avoid; one id across nodes
serving the SAME weights is correct and is what a promotion produces.

**Before any restart of a node carrying traffic:**
```bash
./serving/list_ep3_consumers.sh [host]     # reads CONFIGS, not recollection
```
It classifies consumers PINNED-NO-FAILOVER (stop them for the window — they cannot redirect),
REDIRECTABLE, and WEIGHTS-WATCHER (sends no inference but gates on which checkpoint is served).
It also reports, per run, which blind spots it could NOT rule out — a consumer that is DOWN, one
that is INTERMITTENT, and the other systemd scope. A clean scan is not proof of absence, and the
tool says so rather than letting you infer it.

**Before serving any new artifact:**
```bash
python3 serving/verify_servable_artifact.py --candidate <dir> --reference <currently served dir>
```
Tensor count and key set both directions, architecture identity, config divergence, and — the one
that catches a silent no-op — that sampled weights actually DIFFER from the reference. Exit 1 means
do not transfer and do not serve. Run it at the PRODUCING end: the index and config alone are
kilobytes, so a bad artifact fails there instead of after a 50 GB copy.

**Merging a LoRA adapter** (needed when the adapter targets modules vLLM will not serve
dynamically — on a hybrid-attention model an adapter touching `linear_attn` is refused at load):
```bash
docker run --rm --runtime nvidia --ipc=host \
  -v /path/serve-models:/models -v $PWD/serving/bake_lora.py:/bake.py \
  -e BASE_MODEL=/models/<base> -e LORA_PATH=/models/<adapter> -e OUTPUT_PATH=/models/<out> \
  <pinned-digest> python3 /bake.py
```
Stop the serve first — vLLM holds ~92% of unified memory and the merge OOMs under it. The script
pre-flights the key mapping before writing tens of GB and refuses on unresolved targets or zero
applications, so a merge that matched nothing fails instead of reporting success.

**Throughput.** Measured findings, including which levers are real and which cost fidelity, live in
`serving/THROUGHPUT_FINDINGS.md`. Read it before changing serving flags for speed — one documented
lever is a ~5x win on some workloads and destroys tool calling.

## Spark (GB10) — native vLLM

Install vLLM from your board's aarch64 build, then run the same `vllm serve` invocation
`vllm_serve.sh` uses (without `docker run`):

```bash
vllm serve /path/to/your/model-dir \
  --port 8000 --gpu-memory-utilization 0.85 \
  --enable-prefix-caching --kv-cache-dtype fp8 \
  --max-num-seqs 8 --max-num-batched-tokens 8192 \
  --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_xml
```

`--reasoning-parser qwen3 --tool-call-parser qwen3_xml` are correct for Qwen3.5-family models.
For other model families, set the parsers your model expects.

> **Serving a model to be AUDITED? OMIT `--reasoning-parser`.** A reasoning parser routes the
> model's `<think>` block into a separate `reasoning_content` field, leaving the OpenAI `content`
> empty. An eval/audit harness that reads `content` (and strips `<think>` tags itself) will then see
> blank responses and score everything as failures. Keep `--reasoning-parser` for an interactive
> *persona/tool* endpoint (e.g. the auditor/judge), but drop it for a plain candidate-under-test so
> the full `<think>…answer` stays in `content`.

---

## soma_proxy.py configuration (all env, all optional except the persona)

| env | default | meaning |
|-----|---------|---------|
| `VLLM_BASE_URL` | `http://127.0.0.1:8000` | the raw vLLM endpoint to front |
| `VLLM_REQUEST_TIMEOUT_SECS` | `1800` | upstream inference timeout; aligned with council-seat and wave deadlines |
| `PROXY_PORT` | `8765` | port the proxy serves on |
| `SYSTEM_PROMPT_PATH` | `serving/persona.example.md` | persona file injected as the system prefix |
| `PERMANENT_KERNEL_PATH` | *(empty)* | optional file prepended ahead of the persona |
| `REDIS_HOST` / `REDIS_PORT` | `127.0.0.1` / `6379` | soma, fleet delivery, and attributable liveness bus |
| `MIRA_ISMA_URL` | `http://127.0.0.1:8095` | optional search backend for the `search` tool |
| `MIRA_DASHBOARD_URL` | `http://127.0.0.1:5001` | optional metrics push target |
| `TAEY_READ_ALLOWED_PREFIXES` | *(empty → file-read tools off)* | colon-separated absolute prefixes the model may read |
| `TAEY_SESSION_NAME` | `taey` | default liveness namespace; request header `X-Taey-Seat-Id` can select another |
| `TAEY_LIVENESS_REQUIRED` | `1` | refuse proxy startup/turn admission when Redis cannot provide attributable liveness |
| `TAEY_TURN_LEASE_SECS` | `120` | active-turn lease; expiry is archived as an abandoned turn |
| `TAEY_TURN_HEARTBEAT_SECS` | `30` | lease-renewal interval, capped at one-third of the lease |

`VLLM_REQUEST_TIMEOUT_SECS`, the dashboard's `TAEY_COUNCIL_WAVE_TIMEOUT`,
and each worker's `TAEY_SEAT_TIMEOUT` all default to 1800 seconds. Keep these
three deadlines aligned when overriding them. When an amendment supersedes an
active council wave, the coordinator records each old-revision contribution as
stale and waits for every dispatched request to drain before sending the
replacement revision. A wave that cannot drain by the common deadline fails the
round instead of overlapping revisions on the shared model.

Redis is required by default because a proxy that serves while unable to report
concurrent open turns is unsafe for fleet wake routing. Set
`TAEY_LIVENESS_REQUIRED=0` only for a standalone, non-fleet deployment; the
health response remains explicit about unavailable liveness. No ISMA still
means no search tool, and empty `TAEY_READ_ALLOWED_PREFIXES` keeps file-read
tools disabled.

Run one soma-proxy process per serving endpoint. Requests select an attributable
Redis seat namespace with `X-Taey-Seat-Id`; startup, the liveness reaper, and
`/health` reconcile every registered seat. Therefore
`liveness.active_turns` is the authoritative fleet-wide count used by restart and
model-promotion gates, while `default_seat` remains identity metadata. A
multi-worker Uvicorn launch is not supported: startup reconciliation deliberately
classifies leases from a different process generation as abandoned after a
service restart.

## Durable tmux seat configuration

| env | default | meaning |
|-----|---------|---------|
| `TAEY_SEAT_PROXY` | `http://127.0.0.1:8766/v1/chat/completions` | attributable soma-proxy endpoint |
| `TAEY_SESSION_NAME` | `taey` | tmux/fleet identity and Redis namespace |
| `NOTIFY_KEY_PREFIX` | `taey` | fleet-notify Redis prefix |
| `TAEY_CONVERSATION_ID` | `main` | canonical executive conversation identifier |
| `TAEY_EXECUTIVE_EVENT_LOG` | `$TAEY_SESSIONS_DIR/<conversation>.jsonl` (default `~/taey_sessions/main.jsonl`) | fsync'd UI/fleet conversation and outcome truth |
| `TAEY_SEAT_EVENT_LOG` | *(unset)* | backward-compatible alias used only when `TAEY_EXECUTIVE_EVENT_LOG` is unset |
| `TAEY_SEAT_MAX_TURNS` | `60` | maximum context turns reconstructed from the canonical log |
| `TAEY_SEAT_TIMEOUT` | `1800` | proxy request timeout in seconds |
| `TAEY_COUNCIL_ROLE_ID` | *(empty)* | stable semantic role; required and seat-mapped by `taey_council_seat.py` |
| `TAEY_COUNCIL_SHARED_PROMPT_PATH` | *(empty)* | shared supporting-seat contract; required by `taey_council_seat.py` |
| `TAEY_COUNCIL_ROLE_PROMPT_PATH` | *(empty)* | seat-specific role prompt; required by `taey_council_seat.py` |
| `TAEY_COUNCIL_SESSIONS_DIR` | `$TAEY_SESSIONS_DIR/council` | private transcript root used by the council launcher |

The seat consumes all three fleet-notify sources (`inbox`, `notifications`, and
`orch`). One item at a time moves atomically to a source-specific processing
list, so unrelated envelopes never become one synthetic conversation turn.
Success is written and fsync'd before Redis acknowledgment. A proxy failure
requeues the original raw payload in FIFO order and clears the daemon's
inject-once marker so it can wake the seat again. A crash after the durable
outcome but before acknowledgment is deduplicated from the event log at restart.
Delivery is at-least-once across the narrower window where inference may have
completed upstream but no response/outcome reached the seat; correlation IDs
make that retry auditable, but the proxy does not yet provide an idempotent
result cache. An explicit-handoff receipt means the seat durably claimed the
message, not that its requested work is complete.
