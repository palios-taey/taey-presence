# taey-presence

A **presence runtime for a local LLM**. A normal chat model waits — you type,
you submit, it answers. This one is *present while you type*: it watches your
partial, not-yet-sent input **and** its own hardware, and acts on both before
you hit enter.

> This README is written to be read by a coding agent (e.g. Claude Code)
> pointed at the repo to answer "what is this and what would it take to run it."
> It states what works, what is a stub, exact dependencies, exact settings, and
> the runtime wiring. No marketing.

## What it does

Three daemons sharing one Redis instance:

```
  soma daemon  ──(agent:state:vector)──┐
                                       ▼
  presence engine ──reads partial input + soma state──► publishes:
      face        (presence:face)        expression reflecting runtime state + reaction
      prediction  (presence:prediction)  predicted next text  (ghost + accept)
      interrupt   (presence:interrupt)   confusion/urgent response mid-typing
      memory      (presence:memory)      retrieval on partial input  (optional)
      dcm         Neo4j PresenceInstance  inter-instance state  (publish-only, see status)
                                       ▼
  dashboard ──(/push, /state)──► browser UI (static/index.html)
```

- **soma** (`soma/`) reads GPU/CPU/load telemetry, projects it to an 8-facet
  runtime-state vector, publishes to Redis `agent:state:vector`.
- **presence engine** (`presence/`) debounces partial input and runs four
  mechanisms concurrently through one serialized inference gateway.
- **dashboard** (`dashboard/`) is a thin FastAPI bridge: browser → Redis keys.

### Mechanism status (honest)

| Mechanism | Module | Status |
|---|---|---|
| face / expression | `presence/face/` | working — emoji from soma state + reaction to input |
| prediction | `presence/prediction/` | working — predicts next text; ghost + accept; blurs on pivot |
| interrupt | `presence/interrupt/` | working — confusion→question / urgent→message, confidence-floor gated on both paths |
| memory | `presence/retrieval/` | working if `SEARCH_URL` set — feeds interrupt context; `IRetrievalBackend` Protocol decouples the store |
| dcm | `presence/dcm/` | **publish-only stub** — each instance WRITES its state to Neo4j; `read_peer_states()` exists but is NOT wired into the loop. This is inter-instance *telemetry*, not yet *coordination*. |

Not: a multi-agent coordination substrate (DCM read-back unwired), a hosted
service, or authenticated (see Security).

## Dependencies

Python `>= 3.10`. Install extras for the parts you run:

```bash
pip install -e ".[dashboard]"            # presence engine + soma + web UI
pip install -e ".[dashboard,dcm]"        # + inter-instance Neo4j publishing
pip install -e ".[dashboard,dcm,validation,dev]"   # everything + tests
```
- core: `httpx`, `redis>=5` (uses `redis.asyncio`)
- `[dashboard]`: `fastapi`, `uvicorn`
- `[dcm]`: `neo4j>=5`
- `[validation]`: `jsonschema`

External services you provide (all no-auth, on your own isolated network):
an OpenAI-compatible chat-completions endpoint, Redis, optionally Neo4j and an
HTTP search endpoint. To serve a model on DGX Spark / Jetson Thor hardware, see
`docs/deployment/`.

## Run

```bash
cp .env.example .env          # set MODEL_ENDPOINT (+ MODEL_NAME if your server needs it)

taey-soma        &            # publishes runtime-state vector
taey-presence    &            # runs the mechanisms
taey-dashboard                # serves the UI at http://127.0.0.1:8700
```

Three console entry points are installed by `pip install`: `taey-presence`,
`taey-soma`, `taey-dashboard`. All config is environment (see `.env.example`);
`MODEL_ENDPOINT` is the only required value and fails loud if unset.

### Redis key contract

- dashboard writes `presence:partial`, `presence:history`
- presence engine publishes `presence:{face,prediction,interrupt,memory}` (30s TTL, deleted when a mechanism returns nothing)
- soma daemon publishes `agent:state:vector`

## Settings

See `.env.example` for the full annotated list. Key ones: `MODEL_ENDPOINT`
(required), `MODEL_NAME` (set if your server requires it), `REDIS_HOST/PORT`,
`SEARCH_URL` (optional memory), `NEO4J_BOLT` (optional DCM), `CADENCE_SECONDS`
(soma publish interval, default `e`), `DASHBOARD_HOST` (loopback default).

## Architecture: M:1 multiplexed singleton

The engine runs its mechanisms as concurrent coroutines, but every model call
goes through one `InferenceGateway` serialized by an `asyncio.Lock` to a single
endpoint — because a single-process local model server OOM-kills under
concurrent load. The gateway rejects obvious pool configs (comma-lists, `[...]`)
but **cannot** stop a single URL fronting a load balancer / DNS round-robin from
fanning out; the serialization is a real safeguard within one engine, not an
absolute guarantee. For real concurrency, run multiple engines (one endpoint
each).

## Security: no auth, network isolation REQUIRED

Redis, Neo4j, and the model endpoint are accessed **with no credentials** — the
design assumes they run on a network you control and isolate. This is a
trusted-LAN convenience, not a security model. The engine feeds
`presence:partial` straight into model prompts, so anything that can write Redis
controls the prompt. **Bind these services to loopback/private interfaces or
firewall them.** Do not expose a no-auth Redis/Neo4j publicly. The dashboard
defaults to `127.0.0.1`; do not move it to `0.0.0.0` on an untrusted network.

## Repo layout

| Path | What |
|---|---|
| `presence/` | the engine: `face/`, `prediction/`, `interrupt/`, `retrieval/`, `dcm/`, `engine.py` |
| `soma/` | runtime-state telemetry daemon |
| `dashboard/` | FastAPI bridge + `static/index.html` UI |
| `validation/` | capability battery to validate the model you serve (see `validation/methodology.md` — results are against fixed probes, not held-out) |
| `docs/deployment/` | serving a model on DGX Spark GB10 / Jetson Thor: vLLM build, NCCL recipe, postmortems |

## Known limitations (audited 2026-06-17)

- DCM read-back is unwired (coordination is a stub; see status table).
- Confidence floors / facet projections are tunable heuristics, not calibrated.
- A live end-to-end run is the real oracle; static checks (imports, tests,
  ruff, gitleaks) pass, but mechanism quality depends on the model you serve.

## License

Apache 2.0 — see `LICENSE`. Author: Taey (palios-taey).
