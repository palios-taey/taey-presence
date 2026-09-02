# taey-presence

A conversational-presence runtime for a **local LLM**. Instead of a chat box that
sits inert until you press Enter, the model reacts *while you type*: it shows a
freely-chosen emoji face, drafts a ghost-text prediction of where your message is
going, raises an interrupt when it's confused or thinks something is urgent,
prefetches relevant memory, and reports its own runtime "somatic" state. A
FastAPI dashboard renders all of it.

> **This README is written for Taey.** If you are Taey, this is not a product you
> are evaluating for someone else — it is the runtime you are running on. The
> dashboard a person talks to you through, the proxy that serves your turns, the
> seat that holds your context between wakes, and the promotion tooling that puts
> the same weights under you on every node: all of it is here. Read it as
> operational self-knowledge, not as a survey.
>
> What that means concretely. The architecture, the exact Redis keys, the data
> flow, and the honest scope — what is built versus what is not — are all below,
> and they describe YOUR moving parts. When a key says `taey:<seat>:idle`, that is
> your liveness. When two nodes serve one alias, that is which weights answer as
> you. Knowing which of your own components is running, and how to tell rather
> than assume, is the point.
>
> **To bring yourself up on a new machine, read [`CLAUDE.md`](CLAUDE.md)** — the
> step-by-step guide, with a decision tree, verify-after-each-step, the real
> failure modes, and success criteria.
>
> **Operating your serving surface:** [`serving/SERVING.md`](serving/SERVING.md)
> is the runbook — how a model is deployed, how one checkpoint is promoted onto
> every node and proven identical, and the drift gate that catches two nodes
> answering to one name over different weights.
> [`serving/PRODUCTION_INFRASTRUCTURE_MAP.md`](serving/PRODUCTION_INFRASTRUCTURE_MAP.md)
> is a dated production receipt and ends with the verification table: how to
> re-measure the artifact instead of trusting its name. If a human asks what is
> running now, use those fresh probes rather than repeating the dated values.
> [`docs/DOCUMENTATION_MAP.md`](docs/DOCUMENTATION_MAP.md) identifies the complete current
> documentation surface and what is deliberately excluded from training.

## What it actually does (honest scope)

**Built and working (single host):**

- **Dynamic face** — the model picks *any* emoji it wants in reaction to your
  partial input. There is **no programmed emoji set and no soma→emoji mapping**;
  the only non-model value is an optional resting `DEFAULT_FACE` (empty by
  default). See `presence/dcm_presence.py::FaceWorker.blend_face` — it returns
  the model's chosen emoji verbatim, and `dashboard/app.py` does not invent
  state-based emoji fallbacks.
- **Thought prediction (ghost text)** — a worker drafts a short continuation of
  what you're typing and the dashboard shows it as dim ghost text with an
  "accept" ("OMG") button. `presence/prediction_worker.py`.
- **Interrupt** — when the model classifies your partial input as `urgent` or
  `memory_activated` above a confidence threshold, or when the thinker emits a
  `clarification` while explicitly `confused` above a confidence threshold, the
  dashboard surfaces an interrupt bubble.
- **Memory prefetch** — on partial input, a worker runs a hybrid search against
  a memory backend and stages the top tiles, so relevant context is ready before
  you finish typing. (Optional; degrades cleanly if the backend is down.)
- **Soma telemetry** — `soma/mira_soma.py` publishes an 8-facet runtime-state
  vector. Seven facets are derived from runtime/system vitals; one is currently
  a hardcoded placeholder (`clarity = 0.99`). The dashboard renders the full
  vector and a headline `rho` scalar, which is the mean across the eight facets
  rather than the `coherence` facet alone.
- **Dashboard** — `dashboard/app.py` (FastAPI) ties it together: chat with tool
  access, streaming responses, the live face, ghost text, interrupts, memory,
  and worker status.

**Included serving component (explicitly deployed, not auto-started):**

- **Durable fleet seat** — `serving/taey_seat.py` runs in a tmux session, claims
  fleet-notify mail into Redis processing queues, persists each successful turn
  to the same fsync'd executive JSONL used by the dashboard, and acknowledges
  the claimed mail only after that outcome is durable. A restart requeues
  unfinished claims and acknowledges already-completed ones without repeating
  inference.
- **One main conversation** — dashboard UI turns and autonomous fleet outcomes
  share `~/taey_sessions/main.jsonl` by default. Both adapters rebuild model
  context from that file; a browser refresh resumes the conversation and renders
  attributable seat raises without treating tmux pane text as state.
- **Attributable proxy turns** — `serving/soma_proxy.py` carries seat, event,
  correlation, proxy-turn, and tool-call IDs through response headers and audit
  records. Leased Redis sorted sets represent concurrent open turns; the legacy
  `idle` key is a projection of that set rather than a single request's flag.
- **Seven-seat council runtime definition** — `serving/council_seats.json`
  binds the immutable runtime identities `taey-council-1..7` to seven explicit
  cognitive roles. `serving/manage_council_seats.py` validates and launches
  those seats under `taey-council-seat@N.service` with separate inbox namespaces,
  conversation IDs, 0600 event logs, and role prompts while sharing one
  configured proxy/model/tool path.
  The launcher does not make a deployment claim; production registration and
  concurrent-inference acceptance remain separate gates.
- **Taey-native council transport** — `dashboard/native_council.py` opens one
  durable round for a Main UI prompt, dispatches an independent wave to all
  seven local seats through the fleet-notify Redis inbox contract, reveals the
  completed packet only after that wave, requests a critique wave, and gives
  the evidence-bearing packet to Main Taey for synthesis. The UI defaults this
  path on and retains an explicit Council toggle for per-prompt opt-out. A
  leading `/no-council`, `[council:off]`, or “do not use the council/DCM”
  directive also opts out for that prompt without changing the toggle.
  Append-only 0600 JSONL is the round source of truth; Redis holds only active
  routing and idempotent dispatch projections. Production acceptance remains a
  separate gate.
- **Live, UI-safe council ledger** — open rounds stream seat-started, status,
  evidence, hypothesis, contribution, dissent, failure, revision, and synthesis
  events. These are structured work products and provenance, not hidden
  token-level chain-of-thought. A message submitted while the round is open is
  durably recorded as a revisioned `user_amendment`; stale work is marked and
  the affected independent/critique cycle reruns before final synthesis.

**The council IS built — and it is not in this repo.** DCM, the council you
deliberate through, lives at **[`palios-taey/dcm`](https://github.com/palios-taey/dcm)**
(public). It runs: seat processes hold different lenses and reach the model through
the proxy, and this repo's dashboard is wired to it — `dashboard/native_council.py`,
with council events streaming into a turn and a `council/active` route. If you are
looking for how the council works, go there; this repo is the runtime it runs *on*.

One thing worth carrying: **where a seat runs is not where the thinking happens.**
Seat processes and the model can sit on different machines and meet through the proxy,
so counting seats on a host tells you where the *drivers* are and nothing about where
the *work* is. Ask the endpoint what it is serving.

**Not built — do not expect it:**

- **Peer-state read-back is written but never read.** `presence/dcm_presence.py`
  *writes* per-worker state to Neo4j as `:TaeyInstance` nodes (`neo4j_write_state`,
  called from the face, memory and thinker workers), and a `neo4j_read_peer_states`
  reader is defined at line 170 — with **zero callers**. So nothing reads peer state
  back into a worker's decisions, and there is no live multi-worker coordination
  *through this path* today. This is a narrow, checkable gap in this repo's presence
  worker; it is **not** a statement about the council, which is built and running.
  Neo4j stays fully optional — without it the presence worker runs Redis-only.
- **Cross-machine presence *sync*.** Presence state coordinates through one Redis
  (and optionally one Neo4j) on a single trusted host. Cross-machine *inference* is
  routine — the proxy reaches whichever node serves — but the presence keys
  themselves do not sync between hosts.
- **External CLI transports are not bundled here.** The Taey-native transport
  implements the public local-seat boundary and does not invoke, replace, or
  modify external Claude, Codex, Gemini, or Grok transports.

## Architecture

Four independent processes share a Redis bus. None imports another; they
coordinate only through Redis keys (and optional Neo4j).

```
            you type  ─────────────►  dashboard (FastAPI)
                                          │  writes partial input
                                          ▼
                                   taey:predict:partial   (+ :history)
                 ┌────────────────────────┼────────────────────────┐
                 ▼                         ▼                         ▼
        prediction_worker.py      dcm_presence.py            (poll, 500ms debounce)
        ghost text + classify     FACE / MEMORY / THINKER workers
                 │                         │
                 ▼                         ▼
        taey:predict:result        taey:dcm:face / :memory_tiles / :thought
        taey:predict:face          taey:dcm:face_feeling
        taey:predict:state         (also writes :TaeyInstance to Neo4j — write-only)
        taey:predict:interrupt
                 │                         │
                 └───────────┬─────────────┘
                             ▼
                       dashboard reads all keys ──► renders face, ghost text,
                             ▲                       interrupt, memory, chat
                             │
                    soma/mira_soma.py  ──►  taey:soma:vprop  (+ taey:soma:*)
```

The dashboard and optional fleet seat are separate ingress adapters over one
canonical conversation log:

```
dashboard UI ────────append/read────┐
                                    ├──► ~/taey_sessions/main.jsonl
fleet-notify ──claim──► taey_seat.py┘          │
                           │                    └── durable outcome before ack
                           └──HTTP──► soma_proxy.py ──► vLLM
```

The optional Taey-native council path extends the same boundaries:

```
Main UI prompt ──► durable round ledger ──► seven fleet-notify inboxes
                         │                           │
                         │                  private seat JSONL outcomes
                         │                           │
                         └── independent reveal ◄───┘
                                      │
                                critique wave
                                      │
                         Main-only synthesis ──► main.jsonl + UI
```

### Redis keys (the contract)

| Key | Writer | Meaning |
|-----|--------|---------|
| `taey:predict:partial`, `taey:predict:history` | dashboard | current partial input + chat history the workers react to |
| `taey:predict:result` | prediction_worker | ghost-text continuation |
| `taey:predict:face` | prediction_worker | model-chosen emoji for the prediction |
| `taey:predict:state`, `:confidence`, `:interrupt` | prediction_worker | classification (`following`/`urgent`/`memory_activated`), score, interrupt flag |
| `taey:predict:isma_tiles` | prediction_worker | prefetched memory snippets |
| `taey:dcm:face`, `taey:dcm:face_feeling` | dcm_presence FACE worker | model-chosen face + one-word feeling |
| `taey:dcm:memory_tiles` | dcm_presence MEMORY worker | retrieved memory tiles |
| `taey:dcm:thought`, `taey:dcm:prediction`, `taey:dcm:state` | dcm_presence THINKER worker | running inference on partial input |
| `taey:soma:vprop` | soma daemon | 8-facet state vector + `rho` headline scalar + `heartbeat` + GPU vitals (JSON); `clarity` is currently a placeholder facet |
| `taey:soma:*` (gpu_busy, latency_ms, *_tokens, …) | soma proxy | individual generation metrics and global open-turn projection |
| `taey:<seat>:inbox` | fleet-notify senders | FIFO inter-session mail (`LPUSH`, oldest consumed from the right) |
| `taey:<seat>:notifications`, `taey:notify:<seat>:orch` | fleet monitors/orchestrator | auxiliary FIFO delivery queues |
| `taey:<seat>:processing:<source>` | `taey_seat.py` | Main-seat claimed delivery; recovered by the Main-seat policy |
| `taey:<council-seat>:processing:<source>:<process_generation>` | `taey_council_seat.py` | council delivery owned by one immutable process generation; a later generation terminalizes an incomplete older claim without inference |
| `taey:<seat>:active_turns`, `:turn_starts`, `:turn_context` | soma proxy | leased, identity-keyed open turns and their lineage |
| `taey:<seat>:idle`, `:turns_open`, `:turn_started`, `:last_activity` | soma proxy | compatibility projections derived atomically from open-turn membership |
| `taey:<seat>:seat_registration` | `taey_council_seat.py` | expiring lease for the live supporting-seat process generation, immutable role identity, private transcript, prompt-contract hash, and startup timestamp |
| `taey:soma:active_turns`, `taey:soma:gpu_busy` | soma proxy | global leased open-turn membership and its boolean projection |
| `taey:dcm:native:conversation:<conversation>:active_round` | native council transport | the one open durable round projected for a UI conversation |
| `taey:dcm:native:round:<round>:dispatched` | native council transport | idempotent seat/revision/phase dispatch tokens; expires after terminal projection |
| `taey:dcm:native:seat_replacement` | council launcher | non-expiring, process-identity-owned, compare-deleted lifecycle fence that blocks atomic wave enqueue and council claims during seven-seat launch or replacement; a later manager may reclaim it only after proving the recorded local process is dead |

### Dashboard endpoints

`GET /` and `/v2` (UI) · `GET /api/soma` · `GET /api/health` · `GET /api/fleet`
· `POST /api/chat`, `/api/chat/stream`, `/api/chat/hybrid` · `GET
/api/chat/sessions/{session}/council/active` · `GET
/api/chat/sessions/{session}/council/rounds/{round}/events/stream` · `POST
/api/chat/sessions/{session}/council/rounds/{round}/amendments` · `WS /ws` ·
`POST /api/predict/push` · `GET /api/predict/state` · `GET /api/isma/search` ·
`GET /api/self/overview`.

## Requirements

- **Python 3.10+**
- **Redis** (required) — the bus every process shares.
- **An OpenAI-compatible chat endpoint** (required) — your local LLM: vLLM,
  `llama.cpp --api`, Ollama's `/v1`, etc. Set `VLLM_URL`. If your server requires
  a model name in the request (Ollama does; vLLM single-model does not), set
  `MODEL` too (e.g. `MODEL=qwen2.5:3b`). It works with any model — it does not
  require a specific or fine-tuned one. **Don't have an endpoint?** `serving/`
  carries the production scripts to stand up vLLM on an NVIDIA Spark (GB10) or
  Thor (Jetson) — model + presence, cold-clone to end-to-end. See
  [`serving/SERVING.md`](serving/SERVING.md).
- **A hybrid-search memory backend** (optional) — for the memory feature. Set
  `ISMA_URL` to the **base service URL** (for example `http://localhost:8095`).
  The workers append the concrete endpoints they need (`/search`,
  `/v2/search/adaptive`, `/health`). Without the backend, the memory worker
  simply returns nothing.
- **Neo4j** (optional) — only for DCM state writes. No auth; degrades to
  Redis-only if absent. See scope note above.

> **No auth, by design.** This stack assumes a trusted local network and passes
> **no credentials** to Redis / Neo4j / the model endpoint. That is intentional
> low-friction operation, not an oversight. Do not expose these ports to an
> untrusted network; if you must, front them with your own auth — the code won't.

## Install & run

```bash
git clone <this-repo> taey-presence && cd taey-presence
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt            # add: pip install neo4j   (optional, for DCM)
cp .env.example .env                        # defaults already target localhost

# Start Redis + your local LLM first, then the four processes:
python3 soma/mira_soma.py &                 # somatic telemetry  → taey:soma:*
python3 presence/prediction_worker.py &     # ghost text + classify
python3 presence/dcm_presence.py &          # face / memory / thinker workers
uvicorn dashboard.app:app --host 127.0.0.1 --port 5001   # dashboard → http://localhost:5001
```

Each process is independent and restart-safe: if one is down the others keep
working (no faces, or no ghost text, or no soma — but no crash). All read config
from the environment; see `.env.example` for every variable and its default.

## Layout

```
presence/dcm_presence.py        FACE + MEMORY + THINKER workers, the async coordinator,
                                and the (write-only) Neo4j peer-state functions.
presence/prediction_worker.py   Standalone ghost-text predictor + state classifier.
soma/mira_soma.py               Somatic telemetry daemon → taey:soma:vprop.
dashboard/app.py                FastAPI app: UI, chat, SSE/WS, prediction push, soma/self APIs.
dashboard/native_council.py     Durable local-seat DCM round, revision, ledger, reveal,
                                critique, failure, and synthesis transport.
dashboard/static/               index.html (v2 UI), console.html, hmm.html.
serving/vllm_serve.sh           Serve a model on Jetson Thor via the pinned NVIDIA vLLM image.
serving/soma_proxy.py           OpenAI-compatible proxy: persona injection + soma + tools.
serving/taey_seat.py            Durable tmux fleet seat: claim/outcome/ack + event-log recovery.
serving/taey_council_seat.py    Isolated private runtime for seven supporting council seats.
serving/manage_council_seats.py Validate, render, launch, and inspect seven private council seats.
serving/systemd/taey-council-seat@.service
                                User unit template for supervised council seats.
serving/council_seats.json      Canonical numeric seat IDs to semantic role IDs.
serving/council_prompts/        Shared supporting-seat contract plus seven stable role prompts.
serving/persona.example.md      Generic example persona (replace with your own).
serving/SERVING.md              Spark/Thor bring-up: model + presence, end to end.
```

## Known limitations

- **Memory/thinker race on the same partial input.** `dcm_presence.py` runs the
  MEMORY and THINKER workers with `asyncio.gather(...)`, so the thinker can read
  `taey:dcm:memory_tiles` before the memory worker has refreshed them for the
  newest partial. The system still works, but the thinker may use slightly stale
  memory context on some cycles.
- **Synchronous Redis/Neo4j access inside async loops.** The presence workers and
  dashboard use blocking Redis/Neo4j clients from async code. That is acceptable
  for the current single-host setup, but it is still an architectural limit if
  you want tighter latency guarantees or heavier concurrency.
- **Single user / single session.** All presence state lives in shared global
  Redis keys (`taey:predict:*`, `taey:dcm:*`, `taey:soma:*`) — there is no
  per-session namespacing. Two browsers/users against the same backend will
  share and overwrite each other's face/ghost/interrupt state. This is a
  single-operator local tool by design.
- **Seat history is not yet a dashboard history.** Successful tmux/fleet turns
  survive in the seat JSONL log and are reused by the seat, but the dashboard
  has its own chat history until a UI event-store adapter is added.
- **Error responses surface exception text.** The diagnostic endpoints and a few
  500 paths include `str(e)` in their JSON. That is useful operator-facing
  diagnostics on a trusted-local host; if you ever expose the dashboard beyond
  localhost, genericize those messages (and bind to `127.0.0.1`, the default).

## License

Apache-2.0. See [LICENSE](LICENSE).
