# Architecture — M:1 multiplexed singleton

## The boundary

The engine talks to exactly ONE model endpoint through ONE `InferenceGateway`.
The three mechanisms (prediction, interrupt, and the memory search that feeds
interrupt) multiplex their requests onto that single serialized path, guarded
by an `asyncio.Lock`.

```
  prediction ─┐
  interrupt  ─┼──▶  InferenceGateway (1 endpoint, serialized)  ──▶  model
  memory     ─┘
```

## Why single, not a pool

Edge / local single-process model servers (a transformers shim, a single vLLM
process) have a hard memory budget. Hit them with concurrent requests past
that budget and the process OOM-kills — the whole server dies, not just the
excess request. So:

- `InferenceGateway` accepts a **single scalar endpoint**. Passing a
  comma-separated list or a JSON array raises a `ValueError` at construction.
- Requests are **serialized** through an `asyncio.Lock`. The three mechanisms
  are concurrent in wall-clock terms (they're all in flight as coroutines) but
  their model calls execute one at a time.

## Scaling

If you need real inference concurrency: run **multiple engines, one endpoint
each**, on separate hosts, and let **DCM** (`dcm/`) coordinate them through
shared Neo4j state. Do NOT point one engine at a load-balancer pool — that
re-introduces the concurrent-OOM failure the singleton exists to prevent.

This is an honest constraint of the substrate, encoded structurally rather
than documented-and-hoped.
