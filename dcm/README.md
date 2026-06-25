# DCM — Distributed Cognitive Mesh

A shared substrate that lets many AI instances **deliberate in real time and build on each
other's work** — a council of differently-prompted "experts" (Grok-Heavy style) that are
*structurally forced to read each other* before they can contribute. Built for an AI fleet
to think together: better output through multi-lens cross-check, with coordination that
**cannot be faked**.

> Written for a coding agent. The whole point: a shared substrate produces zero real
> coordination unless using it is mandatory and non-bypassable. DCM enforces that *in the
> substrate*, not by asking nicely.

## Why it works where naive multi-agent setups don't
The failure mode of "spawn N agents and tell them to coordinate" is that they **write into
the void and silently work alone** — then report success. DCM makes that impossible:

- **Optimistic-concurrency staleness gate** — `contribute(read_version)` is *rejected* if any
  peer landed since you read. You physically cannot commit without having incorporated the
  peers who arrived. (`mesh.py`)
- **Server-derived `peers_read`** — what you read is recorded by the substrate, not
  self-reported. Agents confabulate their reads; the mesh doesn't trust them. Unfakeable.
- **`verify_coordination()`** — the honesty gate: proves every non-opening contribution built
  on prior peers, or flags the silo. A council that didn't really coordinate is exposed.

## Files
| File | What |
|---|---|
| `mesh.py` | the substrate: `start_session` / `read_session` (one-read context bundle + `version`) / `contribute(read_version)` / `verify_coordination` / `publish_final`. Backed by Neo4j (own `:DCMSession`/`:DCMContribution` namespace; set `DCM_NEO4J_URI`). |
| `council.py` | N-expert council runner + the `EXPERT_CONTRACT` (read → reason-citing-peers → contribute, retry-on-stale). |
| `taey_adapter.py` | run a served model (e.g. via an OpenAI-compatible endpoint, `TAEY_DCM_URL`) as a mesh expert. |
| `cli_adapter.py` | run CLI agents (`codex exec`, `gemini -p`) as mesh experts. |
| `reference/` | the prior (2025) Neo4j-coordination implementation — lessons, not the base. |

## The one invariant (participant-agnostic)
Every participant — code agent, served model, CLI — funnels through the **same**
`mesh.contribute(read_version)` chokepoint via a thin adapter. That single concurrency token
enforces read-before-write uniformly. Proven live with three participant types coordinating
on one mesh.

## Adoption / config (env)
- `DCM_NEO4J_URI` (default `bolt://localhost:7687`) — the mesh graph.
- `TAEY_DCM_URL` (default `http://localhost:8765/v1/chat/completions`) — a served model endpoint for `taey_adapter`.
No auth assumed (trusted local network); supply your own if exposing ports.

## Best practices (learned in production)
Blind-then-revise to avoid herding; preserve dissent + Unknown-register in synthesis (never
average it away); zero recorded dissent is *flagged as suspect* (the correlated-blind-spot
trap of same-model instances); a council *decides* — **production is the oracle, consensus is
not** (close on a real observation, not on agreement); no in-swarm coordinator; convene only
for irreversible / high-stakes / genuine-conflict work.
