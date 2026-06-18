# taey-presence

A conversational-presence runtime for a **local LLM**. Instead of a chat box that
sits inert until you press Enter, the model reacts *while you type*: it shows a
freely-chosen emoji face, drafts a ghost-text prediction of where your message is
going, raises an interrupt when it's confused or thinks something is urgent,
prefetches relevant memory, and reports its own runtime "somatic" state. A
FastAPI dashboard renders all of it.

> **This README is written for a coding agent.** If you're an LLM pointed at this
> repo: the architecture, the exact Redis keys, the data flow, and the honest
> scope (what's built vs. not) are all below. You should be able to read this
> file plus the four modules and know exactly what runs, what each process
> reads/writes, and where the unfinished edge is. Tell your human what it does
> and whether it fits their setup.

## What it actually does (honest scope)

**Built and working (single host):**

- **Dynamic face** — the model picks *any* emoji it wants in reaction to your
  partial input. There is **no programmed emoji set and no soma→emoji mapping**;
  the only non-model value is an optional resting `DEFAULT_FACE` (empty by
  default). See `presence/dcm_presence.py::FaceWorker.blend_face` — it returns
  the model's chosen emoji verbatim.
- **Thought prediction (ghost text)** — a worker drafts a short continuation of
  what you're typing and the dashboard shows it as dim ghost text with an
  "accept" ("OMG") button. `presence/prediction_worker.py`.
- **Interrupt** — when the model classifies your partial input as `urgent` or
  `memory_activated` above a confidence threshold, the dashboard surfaces an
  interrupt bubble. (See *Known issues* — the restraint threshold is real but
  under-tuned.)
- **Memory prefetch** — on partial input, a worker runs a hybrid search against
  a memory backend and stages the top tiles, so relevant context is ready before
  you finish typing. (Optional; degrades cleanly if the backend is down.)
- **Soma telemetry** — `soma/mira_soma.py` publishes an 8-facet runtime-state
  vector (fluency, clarity, vitality, presence, warmth, capacity, flow,
  coherence) derived from GPU/system vitals at a fixed cadence. The dashboard
  renders it and a single `coherence` scalar.
- **Dashboard** — `dashboard/app.py` (FastAPI) ties it together: chat with tool
  access, streaming responses, the live face, ghost text, interrupts, memory,
  and worker status.

**Not built — do not expect it:**

- **Cross-host / multi-instance coordination.** Everything coordinates through
  one Redis (and optionally one Neo4j) on a single trusted host. There is no
  cross-machine presence sync.
- **DCM peer-state read-back.** `presence/dcm_presence.py` *writes* per-worker
  state to Neo4j as `:TaeyInstance` nodes (`neo4j_write_state`) and a
  `neo4j_read_peer_states` reader exists — but **nothing consumes it yet**. Peer
  state is published, not read back into any worker's decisions. Wiring that is
  the next planned step, not a current feature. Neo4j is fully optional today;
  without it the presence worker runs Redis-only.

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
| `taey:soma:vprop` | soma daemon | 8-facet state vector + `coherence` + `heartbeat` + GPU vitals (JSON) |
| `taey:soma:*` (gpu_busy, latency_ms, *_tokens, …) | soma daemon | individual runtime metrics |

### Dashboard endpoints

`GET /` and `/v2` (UI) · `GET /api/soma` · `GET /api/health` · `GET /api/fleet`
· `POST /api/chat`, `/api/chat/stream`, `/api/chat/hybrid` · `WS /ws` · `POST
/api/predict/push` · `GET /api/predict/state` · `GET /api/isma/search` · `GET
/api/self/overview`.

## Requirements

- **Python 3.10+**
- **Redis** (required) — the bus every process shares.
- **An OpenAI-compatible chat endpoint** (required) — your local LLM: vLLM,
  `llama.cpp --api`, Ollama's `/v1`, etc. Set `VLLM_URL`.
- **A hybrid-search memory backend** (optional) — for the memory feature. Set
  `ISMA_URL`. Any service answering `POST {ISMA_URL}` with a `{"tiles": [...]}`
  shape works; without it the memory worker simply returns nothing.
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
uvicorn dashboard.app:app --host 0.0.0.0 --port 5001   # dashboard → http://localhost:5001
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
dashboard/static/               index.html (v2 UI), console.html, hmm.html.
```

## Known issues

- **Interrupt restraint is under-tuned.** Interrupts gate on
  `state in {urgent, memory_activated}` *and* `confidence > 0.809`
  (`prediction_worker.py`). The gate is real but fires more readily than is
  comfortable in practice; tightening the classifier/threshold is open work.

## License

Apache-2.0. See [LICENSE](LICENSE).
