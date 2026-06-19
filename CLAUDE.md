# taey-presence — launch guide for the agent

You are a coding agent (Claude Code or equivalent). A user pointed you at this repo and
wants it **running on their machine**. This file is written for you, not for them. Read it
top to bottom once, then act. Don't ask the user to do steps you can do yourself — inspect,
decide, run, verify. Only stop for things genuinely outside your reach (no GPU, no sudo, a
model file only they can supply).

## What this is (mental model)

`taey-presence` is a **conversational-presence layer** for a local LLM: a face (model-chosen
emoji), ghost-text prediction, gated interrupts, type-time memory prefetch, an 8-facet "soma"
telemetry signal, and a dashboard that renders it all. It is **not** a model. It talks to any
**OpenAI-compatible chat endpoint** and coordinates four small Python processes over **Redis**.

So a working install is always two halves:
1. **An endpoint** — an OpenAI-compatible `/v1/chat/completions` the user already has, OR one
   you stand up from `serving/` (NVIDIA Spark/Thor).
2. **The presence processes** — four Python workers + the dashboard, sharing Redis.

## Decide the path first

Inspect; don't interrogate the user.

- **Is there already a model endpoint?** Probe the common ones: `curl -s localhost:8000/v1/models`,
  `curl -s localhost:11434/api/tags` (Ollama), `curl -s localhost:8765/health` (a soma proxy).
  If one answers → **PATH A** (presence only). This is the common case and the fast one.
- **No endpoint, but NVIDIA GPU hardware** (a Jetson Thor or a GB10 Spark — check `nvidia-smi`,
  `tegrastats`, `jetson_release`) → **PATH B** (serve a model from `serving/`, then PATH A).
- **No endpoint and no capable GPU** → tell the user plainly: they need either a reachable
  OpenAI-compatible endpoint or NVIDIA hardware. Offer to wire it to a remote endpoint they name.

---

## PATH A — presence against an existing endpoint

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # + `pip install neo4j` only if they want DCM state
cp .env.example .env
```

Now edit `.env` for THIS machine. Every value has an in-code default (localhost, no auth), so
change only what differs. The two that matter:
- `VLLM_URL` — the endpoint you found above (full path, including `/v1/chat/completions`).
- `MODEL` — **leave empty for single-model vLLM** (it ignores the field); **set it for Ollama**
  (e.g. `MODEL=qwen2.5:3b`) — Ollama rejects requests with no model. If unsure, read the
  endpoint's `/v1/models` or `/api/tags` and use a name it reports.

Optional services degrade gracefully when absent: `ISMA_URL` (memory search), `NEO4J_URI`
(DCM peer-state). Leave them at defaults if the user doesn't have them.

Start everything. Redis must be up first — `redis-cli ping` should say `PONG`; if not, start it
(`redis-server &`, or the user's package manager / Docker):

```bash
python3 soma/mira_soma.py &                 # soma telemetry  → taey:soma:*
python3 presence/prediction_worker.py &     # ghost text + state classify
python3 presence/dcm_presence.py &          # face / memory / thinker workers
uvicorn dashboard.app:app --host 127.0.0.1 --port 5001
```

### VERIFY (do not declare success without these)
1. `redis-cli ping` → `PONG`.
2. `redis-cli get taey:soma:vprop` → a JSON blob with a recent timestamp (soma is alive).
3. `curl -s localhost:5001/api/self/face` (or open `http://localhost:5001`) → dashboard responds.
4. Type into the dashboard chat → the model replies, a face appears, ghost text shows on partials.
   If the reply errors, the endpoint or `MODEL` is wrong — re-check "Decide the path first".

---

## PATH B — stand up a model on NVIDIA hardware, then PATH A

Full bring-up is in [`serving/SERVING.md`](serving/SERVING.md); the short version:

**Thor (Jetson AGX Thor, aarch64):** the only dependency is one pinned Docker image that bundles
vLLM + torch for aarch64 — **no wheels to install**.
```bash
docker pull ghcr.io/nvidia-ai-iot/vllm:latest-jetson-thor   # ~72 GB, once
export TAEY_MODEL_PATH=/abs/path/to/an/HF/model/dir          # the user supplies the model
./serving/vllm_serve.sh                                      # raw vLLM on :8000
# optional persona/soma/tools proxy on :8765:
export VLLM_BASE_URL=http://127.0.0.1:8000
export SYSTEM_PROMPT_PATH=./serving/persona.example.md       # swap in their persona
python serving/soma_proxy.py
```
**Spark (GB10, aarch64):** run vLLM natively (no Docker) with the identical `vllm serve` args
shown in `serving/SERVING.md`.

Then set `VLLM_URL=http://127.0.0.1:8765/v1/chat/completions` (proxy) or `:8000` (raw) and do PATH A.

### VERIFY the endpoint before wiring presence
- `curl -s localhost:8000/v1/models` lists the model (raw vLLM up).
- `curl -s localhost:8765/health` → `"status":"healthy"` with `vllm` reachable (if using the proxy).
- One chat round-trips:
  `curl -s localhost:8000/v1/chat/completions -H 'content-type: application/json' -d '{"model":"<name>","messages":[{"role":"user","content":"hi"}]}'`

---

## Troubleshooting (the real failure modes)

- **Endpoint 400 on tool calls / reasoning** — match the parsers to the model family. For
  Qwen3.5: `--reasoning-parser qwen3 --tool-call-parser qwen3_xml` (NOT `qwen3_coder`). The serve
  script already sets these; change them only for a different model family.
- **Jetson "out of memory" or a load that stalls after a previous run** — Jetson GPU memory is
  unified with system RAM (UMA), and killing a process / `docker rm` does **not** always release
  it. If `free -g` shows little available, **reboot** to reclaim, then serve. Do NOT `rmmod`/`modprobe`.
- **Dashboard loads but no face / no ghost text** — a worker isn't running or Redis is misconfigured.
  Confirm each process is alive and `redis-cli keys 'taey:*'` shows `taey:soma:*`, `taey:predict:*`,
  `taey:dcm:*` updating.
- **Memory feature returns nothing** — `ISMA_URL` backend is down. Expected if the user has no
  hybrid-search service; everything else still works.
- **Port already in use** — `:5001` dashboard, `:8000` vLLM, `:8765` proxy, `:6379` Redis. Pick
  another and set the matching env var.

## The Redis contract (for debugging)

The processes are decoupled through Redis keys — inspect these to see what's flowing:
- `taey:soma:vprop` — latest 8-facet soma vector + `rho` scalar (written by `soma/mira_soma.py`).
- `taey:predict:*` — ghost-text predictions + classified state.
- `taey:dcm:*` — face, memory tiles, thinker output.

## Success criteria (report these back to the user, with evidence)

You are done when: Redis answers, `taey:soma:vprop` is fresh, the dashboard serves on `:5001`,
and a chat message gets a reply **with a face and ghost text**. Paste the actual `curl` /
`redis-cli` output as proof — don't claim it works without showing it. If a step failed, say
which and why.

## Scope honesty (tell the user; don't oversell)

- DCM peer-state (the Neo4j functions in `presence/dcm_presence.py`) is **write-only** in this
  release — the read-back / coordination side is designed but not wired. Single-user, single-session.
- The memory and soma features need their backends (a search service / a host to poll); without
  them the system runs fine but those features are inert.
- No auth anywhere, by design — trusted-LAN assumption. Don't expose these ports publicly.
