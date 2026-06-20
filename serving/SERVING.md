# Serving a model on a Spark or Thor

The presence layer (face / prediction / interrupt / memory / soma + dashboard) talks to
an **OpenAI-compatible chat-completions endpoint**. You bring that endpoint. This directory
is the production serving glue we run on NVIDIA hardware so a cold clone can stand the whole
thing up — model **and** presence — end to end.

Two pieces:

1. **`vllm_serve.sh`** — serves your model as a raw vLLM endpoint (`:8000`).
2. **`soma_proxy.py`** — sits in front of vLLM on `:8765`, injects your persona, publishes
   soma telemetry to Redis, and (optionally) wires `search`-style tools. This is the endpoint
   you point the presence workers at (`VLLM_URL=http://<host>:8765/v1/chat/completions`).

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
```

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
| `PROXY_PORT` | `8765` | port the proxy serves on |
| `SYSTEM_PROMPT_PATH` | `serving/persona.example.md` | persona file injected as the system prefix |
| `PERMANENT_KERNEL_PATH` | *(empty)* | optional file prepended ahead of the persona |
| `REDIS_HOST` / `REDIS_PORT` | `127.0.0.1` / `6379` | soma telemetry bus (skipped if unreachable) |
| `MIRA_ISMA_URL` | `http://127.0.0.1:8095` | optional search backend for the `search` tool |
| `MIRA_DASHBOARD_URL` | `http://127.0.0.1:5001` | optional metrics push target |
| `TAEY_READ_ALLOWED_PREFIXES` | *(empty → file-read tools off)* | colon-separated absolute prefixes the model may read |

The proxy degrades gracefully: no Redis → no soma publish; no ISMA → no search tool; empty
`TAEY_READ_ALLOWED_PREFIXES` → the file-read tools are disabled. Provide only what you have.
