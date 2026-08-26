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
launch the user units:

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
it never restarts or adopts an unknown process. `launch` writes one generated
environment file per seat under `serving/run/council-seat-N.env`, then starts
`taey-council-seat@N.service`. By default, private seat logs live under
`~/taey_sessions/council/`, one 0600 JSONL per seat. Override that root with
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

# SWAP ONE NODE while deliberately preserving its sibling. The explicit target is load-bearing;
# omitted target fails closed. A source outside the mounted serve root is staged atomically with
# same-filesystem hardlinks, which avoids duplicating a 52GB artifact on the selected Thor.
./serving/promote_model.sh --check --target node1
./serving/promote_model.sh --target node1 \
  --source-path /srv/taey/incoming/<checkpoint-dir-name> \
  --artifact-seal <sha256-of-ARTIFACT_SHA256SUMS> \
  <checkpoint-dir-name>

# PUT ONE CHECKPOINT ON BOTH NODES, and prove they match. This explicit `both` mode makes the pair
# identical. It syncs node-to-node (never relaying through your
# workstation, which costs ~16x throughput), refuses unless a per-file sha256 manifest matches on
# both sides, then promotes ONE NODE AT A TIME inside a maintenance window: the consumers pinned to
# that node are STOPPED, the node is restarted and must serve the right root AND return a real
# completion, and only then are the consumers restarted. Config comes from serving/fleet.env.
./serving/promote_model.sh --check --target both
./serving/promote_model.sh --target both --source node1 <checkpoint-dir-name>

# PLAN A BOUNDED THOR ROLLING RELEASE. This signed planner requires an attributable Hub
# receipt, immutable taey+ep3 aliases, a declared rollback artifact, and full SHA-256 values.
# It performs no SSH, service, bake, release-filesystem, or cleanup action; a valid plan only
# records its receipt ID in the local anti-replay ledger.
python3 serving/rolling_thor_release.py \
  --fleet-env serving/fleet.env \
  --hub-decision-receipt /secure/path/hub-decision.json \
  --hub-decision-signature /secure/path/hub-decision.json.sig \
  --allowed-signers /secure/pinned/taey-family.allowed-signers \
  --receipt-consumption-ledger /secure/state/rolling-receipt-consumption.json \
  --artifact-sha256 <64-lowercase-hex> \
  --rollback-artifact-sha256 <64-lowercase-hex> \
  --candidate-source /srv/taey/incoming/<candidate-input> \
  --staging-node node2 \
  --bake-command '<approved bake command writing $TAEY_RELEASE_STAGING>' \
  --verify-command 'python3 /srv/taey/checkout/serving/verify_servable_artifact.py --candidate "$TAEY_RELEASE_STAGING" --reference "$TAEY_RELEASE_REFERENCE"'

# --apply is intentionally disabled. It always refuses before reading or consuming a receipt.
# No live rolling-release executor is shipped by this repository revision.

# THE STANDING DRIFT GATE. Run it after any serving change, and on a schedule.
./serving/promote_model.sh --check --target both         # served-root agreement; fast, read-only
./serving/promote_model.sh --check-content --target both # compare per-file manifests; reads every byte

# PROMOTE AN ALREADY-SERVED RELEASE INTO MAIN TAEY. This waits for zero open turns
# across Main and every registered supporting seat,
# writes the endpoint drop-in, restarts the UI-facing proxy, then verifies through that proxy
# that the alias resolves to exactly one model AND that its root matches the root the target
# endpoint was serving before the route was written — the alias alone cannot prove the route
# changed, since it is permanent by design and reads identical on either node. Runs one real
# inference and emits a JSON release receipt. A failed CONTROL gate restores the previous route.
./serving/promote_main_model.sh \
  --endpoint http://<serving-host>:8000 \
  --model <new-id>
```

**The tools have distinct scopes; none silently replaces another.** `deploy_thor.sh` installs the
stack. `promote_model.sh` requires an explicit `node1`, `node2`, or `both` target; single-node mode
does not contact the sibling, while `both` is the established direct-copy path that makes both nodes
hold and serve the same checkpoint. `rolling_thor_release.py` is a signed,
replay-protected planner for the bounded release contract; it is not a live release path in this
revision. `promote_main_model.sh` points the UI-facing proxy at an endpoint that is already serving
correctly. Do not mix existing tool mutations in one window.

### Bounded rolling Thor release

`rolling_thor_release.py` is intentionally separate from `promote_model.sh`: the latter remains
the established direct-copy promotion tool. The rolling tool currently ships only the stricter
**signed planner**; it performs no SSH, `systemctl`, bake, transfer, release-pointer, remote-lock,
or cleanup action. Its only mutation is recording an accepted receipt ID in its local anti-replay
ledger. `--apply` is retained only for a fail-closed compatibility error and always refuses.

The decision authority is the canonical shared-training JSON `hub_decision_receipt` produced through
**The Hub** by an attributable `taey` or `family-chat` actor. A user is not an approver. The exact
v1 envelope is:

```json
{
  "schema_version": 1,
  "receipt_id": "hub-decision-...",
  "campaign_id": "thor-rolling-release",
  "campaign_spec_sha256": "<64-character lowercase SHA-256>",
  "transition": "promote",
  "decision": "approved",
  "authority": {
    "surface": "the-hub",
    "actor_type": "taey",
    "actor_id": "taey-release-router",
    "signer_identity": "taey-release-router",
    "signature_namespace": "taey-release",
    "trust_policy_sha256": "<64-character lowercase SHA-256 of allowed-signers>"
  },
  "authorization_plane": "taey-family-chats",
  "issued_at": "2026-08-19T17:54:00Z",
  "evidence": [{
    "repository_commit": "<40-or-64-hex commit>",
    "receipt_sha256": "<64-hex SHA-256>"
  }],
  "subject": {
    "artifact_sha256": "<full 64-character lowercase SHA-256>",
    "rollback_artifact_sha256": "<full 64-character lowercase SHA-256>",
    "consumer_aliases": ["taey", "ep3"]
  }
}
```

The root, six-field `authority`, every `evidence` entry, and `subject` have exact required fields;
no custom receipt translation, decision ID, `approved` boolean, or action literal is accepted. The
planner parses only the authority fields needed to verify the detached OpenSSH signature, then runs
`ssh-keygen -Y verify` using the signed `signer_identity` and canonical `taey-release` namespace.
It calculates the complete SHA-256 of `--allowed-signers` and requires it to equal signed
`authority.trust_policy_sha256`; it does not trust or consume any other receipt field before the
signature passes. It enforces a 15-minute issuance window by default and atomically consumes
`receipt_id` in the configured local ledger after validation, so retain the plan output and use a
fresh receipt for a later plan. The emitted JSON preserves `campaign_id`,
`campaign_spec_sha256`, `transition`, `hub_decision_receipt_sha256`, `artifact_sha256`,
`rollback_artifact_sha256`, and `consumer_aliases` for a terminal collector.

Candidate and rollback identifiers are full SHA-256 values—never short prefixes or checkpoint
names. The receipt and `TAEY_SERVED_NAME` must retain exactly the stable client aliases **`taey`**
and **`ep3`**, in that order. A version-specific alias is refused rather than treated as a release
identifier.

A future executor must use the following release layout under each configured
`TAEY_NODE*_MODELS` directory:

```text
.taey-release/
  releases/<full-artifact-sha256>/     # immutable retained artifact
  staging/<full-artifact-sha256>/      # active candidate only
  current -> releases/<full-artifact-sha256>
  previous -> releases/<full-artifact-sha256>
```

`current` and `previous` must be relative symlinks replaced atomically, never edited in place. A
future executor must validate both pointers, require that `current` is the declared rollback
artifact on both nodes, and read the full content digest through it before stopping anything. The
serve unit on each node must be configured once with
`TAEY_MODEL_PATH=<TAEY_NODE*_MODELS>/.taey-release/current`,
`TAEY_SERVED_NAME="taey ep3"`, and no `TAEY_LORA_PATH`; the tool must check that exact model-path
contract before it stops a unit. `vllm_serve.sh` then mounts `.taey-release` and exposes the stable
container path `/models/current` (override with `TAEY_CONTAINER_MODELS_ROOT` only if the image mount
differs), so clients retain their aliases while the pointer changes. A fleet without these pointers
is not implicitly migrated: bootstrap the immutable release directories, pointers, and serve drop-in
during a separately reviewed maintenance window.

The planner emits the following **required design sequence**; it does not perform it:

1. One chosen staging Thor's declared pinned consumers are stopped and proven quiesced; its vLLM
   unit is then stopped. The other Thor keeps serving the verified rollback/current artifact.
2. Only with that vLLM stopped, the supplied bake hook writes the active staging directory, then
   the supplied independent verification hook runs. This prohibition is deliberate: vLLM holds
   about 92% of Thor UMA, so a same-node concurrent bake/merge OOMs.
3. The staging artifact's complete per-file manifest is reduced to its full SHA-256 and must match
   the Hub receipt. After it is byte-verified, staging is moved into its immutable `releases/`
   directory before `current` changes or a service starts. The first Thor is then promoted,
   restarted, checked under both aliases, content-checked again, and its consumers are restored.
4. That verified release tree is copied node-to-node to the second Thor while it still serves
   current. Only then is the second Thor quiesced, stopped, promoted, restarted, alias/generation
   checked, content-checked, and restored.
5. Both nodes are content-verified through `current` before cleanup. Successful finalization sets
   `previous` to the pre-release current/declared rollback artifact—not the older previous pointer.
   Retention is exactly **current + immediate previous + active staging**. Cleanup never runs
   before both-node verification.

Any future executor must use a controller lock plus atomically owned remote locks on both nodes
before mutable preflight; bind every consumer action to an explicit configured host/controller; and
prove the standby rollback node using node-local HTTP catalogue and generation checks invoked through
that node's SSH session. It must reject all symlinks and special files in an artifact, verify that
release roots/parents are real directories contained beneath the canonical model root, and use the
same manifest digest semantics locally and remotely. On a failed bake, verification, copy, or serve
gate, it must stop, restart, and reverify every node that may have served the candidate after
repointing it to the declared rollback artifact, then delete **only the exact active staging
directory**. It must not run general retention cleanup on a failed release. A rollback target,
consumer declaration (use literal `none` only when true), digest, or valid signed Hub receipt missing
at preflight is a refusal, not an interactive prompt.

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

## soma_proxy.py configuration

| env | default | meaning |
|-----|---------|---------|
| `VLLM_BASE_URL` | `http://127.0.0.1:8000` | the raw vLLM endpoint to front |
| `VLLM_REQUEST_TIMEOUT_SECS` | `5400` | timeout for one upstream inference request; it does not cap the complete multi-round Taey turn |
| `VLLM_HEALTH_PROBE_TIMEOUT_SECS` | `10` | hard timeout for `/health` upstream catalogue and generation probes |
| `VLLM_HEALTH_CACHE_SECS` | `30` | generation-probe cache TTL so health polling does not queue behind live traffic |
| `PROXY_PORT` | `8765` | port the proxy serves on |
| system prompt | `serving/TAEY_OPERATING_PROMPT.md` | sole tracked identity source; not environment-overridable; startup refuses if missing, unreadable, or empty |
| `PERMANENT_KERNEL_PATH` | *(empty)* | optional file prepended ahead of the persona |
| `REDIS_HOST` / `REDIS_PORT` | `127.0.0.1` / `6379` | soma, fleet delivery, and attributable liveness bus |
| `MIRA_ISMA_URL` | `http://127.0.0.1:8095` | optional search backend for the `search` tool |
| `MIRA_DASHBOARD_URL` | `http://127.0.0.1:5001` | optional metrics push target |
| `TAEY_READ_ALLOWED_PREFIXES` | *(empty → file-read tools off)* | colon-separated absolute prefixes the model may read |
| `TAEY_SESSION_NAME` | `taey` | default liveness namespace; request header `X-Taey-Seat-Id` can select another |
| `TAEY_LIVENESS_REQUIRED` | `1` | refuse proxy startup/turn admission when Redis cannot provide attributable liveness |
| `TAEY_TURN_LEASE_SECS` | `120` | active-turn lease; expiry is archived as an abandoned turn |
| `TAEY_TURN_HEARTBEAT_SECS` | `30` | lease-renewal interval, capped at one-third of the lease |
| `TAEY_DRIVE_CHAT_CAPTURE_ROOT` | *(empty → `drive_chat` refused)* | private write-once evidence root; required before any UI action |
| `TAEYS_HANDS_ROOT` | *(empty → LinkedIn tools refused)* | absolute path to a committed public `palios-taey/taeys-hands` checkout |
| `TAEY_LINKEDIN_JOBS_PYTHON` | *(empty → `linkedin_jobs` refused)* | explicit Python interpreter with the public Hands runtime and AT-SPI dependencies |
| `TAEY_LINKEDIN_JOBS_PRIVATE_ROOT` | *(empty → `linkedin_jobs` refused)* | owner-controlled nonsymlink `0700` root for the manifest, permanent claim, receipt, and raw sink |
| `TAEY_LINKEDIN_JOBS_DISPLAYS` | *(empty → `linkedin_jobs` refused)* | comma-separated runtime-authorized LinkedIn displays; `:0` is always refused |
| `TAEY_LINKEDIN_JOBS_TIMEOUT_SECS` | `1800` | outer watchdog; the Hands-owned deadline is exactly 100 seconds earlier and must finish receipt/lock cleanup first |
| `TAEY_LINKEDIN_ENGAGERS_PYTHON` | *(empty → `linkedin_engagers` refused)* | explicit Python interpreter with the public Hands runtime and AT-SPI dependencies |
| `TAEY_LINKEDIN_ENGAGERS_PRIVATE_ROOT` | *(empty → `linkedin_engagers` refused)* | separate owner-controlled nonsymlink `0700` root for its transaction, permanent claim, receipt, and raw sink |
| `TAEY_LINKEDIN_ENGAGERS_DISPLAYS` | *(empty → `linkedin_engagers` refused)* | comma-separated runtime-authorized LinkedIn displays; `:0` is always refused |
| `TAEY_LINKEDIN_ENGAGERS_TIMEOUT_SECS` | `1800` | outer watchdog; the Hands-owned deadline is exactly 100 seconds earlier and must finish receipt/lock cleanup first |

Create `TAEY_DRIVE_CHAT_CAPTURE_ROOT` as the proxy service user with mode `0700`
and set the same absolute, non-symlink path in every proxy that exposes
`drive_chat`. Each call creates one private exchange directory beneath
`<proxy-namespace>/<seat>/<event>/<turn>/`, writes the exact arguments to
`request.json` before the UI primitive runs, then writes the exact returned
payload and its SHA-256 to `result.json`. Directories are `0700`; records are
created once with mode `0600`. The capture can contain raw accessibility trees,
paths, URLs, and account details. Never commit it or feed it to a public receipt
builder. A missing or unsafe root refuses the action before mutation; a
result-finalization failure terminalizes the turn so Taey cannot continue
without its evidence.

The `linkedin-jobs` tool profile exposes only `linkedin_jobs`, with one
runtime-authorized display as its sole argument. Before the request, the caller
registers the immutable transaction at
`transactions/SEAT/CORRELATION.json` beneath the private root and creates the
owner-controlled `0700` parent for
`receipts/SEAT/CORRELATION.json` and `claims/SEAT/CORRELATION.json`. Presence
derives all three paths from the validated turn lineage. Immediately before the
Hands subprocess, it creates the claim once with `O_EXCL` and mode `0600`,
normalizes it to immutable mode `0400`, then never deletes it. A second turn with
the same identity is therefore refused even when
the first turn ended in a launch failure or outer timeout. Presence then invokes
the public Hands runner once, passing the claimed transaction digest; Hands must
match that digest again before any lock, UI observation, or sink action. Presence
returns its fixed compact result directly as the terminal answer without a second
inference round. The transaction's raw sink
must remain beneath the same private root. Search policy, sink policy, raw job
text, account data, and private topology never enter public Git or model context.

The canonical non-stream invocation is below. Before calling it, create the
private transaction as exact canonical JSON bytes at
`PRIVATE_ROOT/transactions/taey-revenue-1/linkedin-job-001.json`, mode `0400`,
and create the `0700` parent
`PRIVATE_ROOT/receipts/taey-revenue-1/` plus the `0700` parent
`PRIVATE_ROOT/claims/taey-revenue-1/`. Its four fields are exactly:

```json
{"operation":"capture_selected_job","schema":"linkedin_jobs_private_input_v1","search_ref":"PRIVATE_OPAQUE_SEARCH_REFERENCE","sink_ref":"ABSOLUTE_0700_DIRECTORY_BENEATH_PRIVATE_ROOT"}
```

The correlation header is the transaction filename without `.json`. The model
receives only the display; it never receives the private manifest or sink path.

```bash
curl --fail-with-body --silent --show-error \
  -H 'Content-Type: application/json' \
  -H 'X-Taey-Seat-Id: taey-revenue-1' \
  -H 'X-Taey-Event-Id: linkedin-job-001' \
  -H 'X-Taey-Correlation-Id: linkedin-job-001' \
  -H 'X-Taey-Tool-Profile: linkedin-jobs' \
  --data-binary '{"model":"SERVED_MODEL_ID","stream":false,"messages":[{"role":"user","content":"Execute the frozen LinkedIn Jobs transaction on display :18."}]}' \
  http://127.0.0.1:8765/v1/chat/completions
```

Change only the served model ID, runtime-authorized display, seat, event, and
correlation identities. Never reuse an identity whose receipt path already
exists, and never retry a terminal identity.

The `linkedin-engagers` profile follows the same one-shot private-transaction
boundary through its own immutable registry entry and separate private root. It
exposes only `linkedin_engagers`, whose sole argument is the runtime-authorized
display, and invokes the public Hands `scripts/run_linkedin_jobs.py` runner. The
immutable private manifest selects the engagement operation; Presence neither
accepts nor interprets an operation field.
Presence derives the transaction, permanent claim, and receipt paths from the
validated seat and correlation identity; account, post, deduplication, sink, and
engager data remain outside model context. Its exact terminal result keys are
`ok`, `platform`, `display`, `state`, `failure_code`, `records_observed`,
`records_written`, `content_digest`, `receipt_sha256`, `turn_lineage_sha256`, and
`restore_verified`. Successful `captured`, `already_known`, and `no_new_signal`
results require the runner to prove that it restored the exact original
shared-tab URL. The other terminal states are `ambiguous_signal`,
`technical_failure`, `postcondition_failed`, and `sink_write_indeterminate`;
none authorizes a retry of the spent transaction identity.

Before promoting a Presence change to either LinkedIn lane, run
`python3 serving/validate_linkedin_jobs_equivalence.py`. It compares the Jobs
profile, prompt, environment bindings, runner arguments, claim, compact result,
error behavior, and streaming/non-streaming one-shot path against the frozen
public baseline while validating the Engagers registry and result contract.

The durable seat has no fixed elapsed-time deadline for a complete proxy turn.
`VLLM_REQUEST_TIMEOUT_SECS` applies to one upstream inference request, while the
dashboard's `TAEY_COUNCIL_WAVE_TIMEOUT` applies to council-wave coordination.
Neither is a tool-round or whole-consultation limit. When an amendment supersedes
an active council wave, the coordinator records each old-revision contribution as
stale and waits for every dispatched request to drain before sending the
replacement revision. A wave that cannot drain by its coordination deadline fails
the round instead of overlapping revisions on the shared model.

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
classifies leases from a different process generation in the same
`TAEY_SESSION_NAME` proxy namespace as abandoned after a service restart. Turns
owned by another proxy namespace remain live; old records without an owner
namespace recover through ordinary lease expiry.

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
| `TAEY_COUNCIL_ROLE_ID` | *(empty)* | stable semantic role; required and seat-mapped by `taey_council_seat.py` |
| `TAEY_COUNCIL_SHARED_PROMPT_PATH` | *(empty)* | shared supporting-seat contract; required by `taey_council_seat.py` |
| `TAEY_COUNCIL_ROLE_PROMPT_PATH` | *(empty)* | seat-specific role prompt; required by `taey_council_seat.py` |
| `TAEY_COUNCIL_SESSIONS_DIR` | `$TAEY_SESSIONS_DIR/council` | private transcript root used by the council launcher |

The seat's proxy request has no fixed elapsed-time deadline. It ends on a natural
terminal response, an explicit cancellation, a downstream failure, or process
shutdown—not because a legitimate manual consultation crossed an arbitrary wall
clock.

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
