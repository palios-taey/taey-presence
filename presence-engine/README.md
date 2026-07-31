# taey-presence-engine

Pre-submit conversational presence for a local LLM. A daemon watches a user's
*partial, not-yet-sent* input (via a Redis key), and runs LLM mechanisms on it
so the UI can react before the user hits enter.

> Audience note: this README is written to be read by a coding agent (e.g.
> Claude Code) pointed at the repo to assess what it does and what running it
> takes. It states what works, what is a stub, exact dependencies, and exact
> settings. No marketing.

## What it actually does (honest status)

Three modules, two of which are wired into the running loop:

| Module | Status | Behavior |
|---|---|---|
| `prediction/` | **working** | On each settled partial, asks the model to predict the user's next text. Publishes `{prediction, confidence, ghost_active, pivot}` to Redis `presence:prediction`. Anticipatory. |
| `interrupt/` | **working** | Asks the model whether it is confused (→ clarifying question) or has something urgent (→ message), gated by a confidence floor on BOTH paths. Publishes `presence:interrupt` when warranted; deletes the key when not. Reactive. |
| `dcm/` | **PARTIAL / publish-only** | Each instance writes its current state to a shared Neo4j (`PresenceInstance` nodes). `read_peer_states()` is implemented but **not yet wired into the loop** — the engine does not currently read peer state back into prediction/interrupt decisions. So this is inter-instance *telemetry publishing*, not yet inter-instance *coordination*. Treat it as a stub for coordination. |

Retrieval (`retrieval/`) is a support path that feeds the interrupt prompt with
optional context; it is not a headline mechanism.

What this is NOT: not a multi-agent coordination substrate (DCM read-back is
unwired), not a hosted service, not authenticated (see Security below).

## Dependencies

From `pyproject.toml`:
- Python `>= 3.10`
- runtime: `httpx>=0.27,<1.0`, `redis>=5.0` (uses `redis.asyncio`)
- `[dcm]` extra (only if using inter-instance publishing): `neo4j>=5.0`
- `[dev]`: `ruff>=0.4`, `pytest>=8`

External services you must provide (all no-auth, on your own network):
- an OpenAI-compatible chat-completions endpoint (vLLM, etc.)
- Redis
- (optional) Neo4j, for DCM publishing
- (optional) an HTTP search endpoint, for the retrieval/memory path

## Settings (environment)

| Var | Required | Default | Meaning |
|---|---|---|---|
| `MODEL_ENDPOINT` | **yes** (fail-loud) | — | base URL of the OpenAI-compatible server, e.g. `http://localhost:8000` (no `/v1` suffix) |
| `MODEL_NAME` | no | `""` | sent as the `model` field. Empty is fine for single-model servers; **set it if your server rejects requests without a `model`** |
| `REDIS_HOST` | no | `127.0.0.1` | Redis host |
| `REDIS_PORT` | no | `6379` | Redis port |
| `SEARCH_URL` | no | `""` | retrieval endpoint; empty = no memory path |
| `NEO4J_BOLT` | no | `""` | bolt URL for DCM publishing; empty = DCM disabled (engine runs standalone) |
| `INSTANCE_ID` | no | `presence-0` | this instance's id in the DCM graph |

No credentials. See Security.

## Run

```bash
pip install -e .            # add ".[dcm]" if using Neo4j publishing
cp .env.example .env        # set MODEL_ENDPOINT (+ MODEL_NAME if your server needs it)
python3 engine.py
```

### The Redis key contract (how a frontend integrates)

- Frontend WRITES: `presence:partial` (current text), `presence:history` (JSON
  array of `{role, content}`).
- Engine PUBLISHES: `presence:prediction`, `presence:interrupt`,
  `presence:memory` (each JSON, 30s TTL; deleted when a mechanism returns
  nothing so the UI does not render a stale ghost).
- `static/demo.html` is a minimal reference frontend (needs a ~20-line
  `/push` + `/state` shim to bridge the browser to those Redis keys; bring
  your own — it is not included).

## Architecture: M:1 multiplexed singleton

The loop runs `prediction`, `interrupt`, and the memory search concurrently as
coroutines, but all model calls go through one `InferenceGateway` serialized by
an `asyncio.Lock` to a single endpoint. Rationale: a single-process local model
server OOM-kills under concurrent load.

Caveat (do not over-trust): the gateway rejects obvious pool configs
(comma-lists, `[...]`) but **cannot** stop a single URL that fronts a load
balancer / DNS round-robin / k8s Service from fanning out, nor two engines
pointed at the same endpoint. The serialization is a real safeguard within one
engine; it is not an absolute structural guarantee. For real concurrency, run
multiple engines (one endpoint each).

## Security: no auth, isolation REQUIRED

The engine connects to Redis, Neo4j, and the model endpoint **with no
credentials** — it assumes they run on a network you control and isolate. This
is a trusted-LAN convenience, not a security model. The engine reads
`presence:partial`/`presence:history` straight from Redis into model prompts,
so anything that can write Redis controls the prompt.

You MUST bind these services to loopback/private interfaces or firewall them.
Do not expose a no-auth Redis/Neo4j on a public interface. If you cannot
isolate the network, add auth to the services yourself (this code does not).

## Known limitations (audited 2026-06-17)

- DCM read-back is unwired (coordination is a stub; see status table).
- Confidence floors are tunable heuristics, not calibrated.
- `clarity` analog / retrieval quality is only as good as the model + search
  backend you provide.
- No demo metrics are asserted here without measurement.

## License

Apache 2.0 — see `LICENSE`.
