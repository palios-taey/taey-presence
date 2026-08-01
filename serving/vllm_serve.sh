#!/bin/bash
# vllm_serve.sh -- serve an OpenAI-compatible vLLM endpoint on NVIDIA Jetson AGX Thor (aarch64).
#
# Dependencies: the pinned NVIDIA Jetson vLLM image. It bundles vLLM + torch built
# for aarch64/Jetson -- there are NO wheels to install. Pull it once:
#     docker pull ghcr.io/nvidia-ai-iot/vllm:latest-jetson-thor
#
# Configure via env (only TAEY_MODEL_PATH is required):
#     TAEY_MODEL_PATH   (required)  full host path to an HF model directory
#     TAEY_MODELS_DIR   (default: dirname of TAEY_MODEL_PATH)  host dir mounted at /models
#     TAEY_CACHE_DIR    (default: $HOME/.cache)  host cache root (compile/triton/vllm caches)
#     VLLM_PORT         (default: 8000)
#     VLLM_GPU_UTIL     (default: 0.85)
#     VLLM_IMAGE        (default: a PINNED digest — see below)
#     TAEY_LORA_PATH    (optional)  LoRA adapter dir (its basename is mounted under /models)
#
# IMAGE IS PINNED TO A DIGEST, NOT :latest-jetson-thor. A floating tag lets two nodes
# silently resolve to different vLLM builds at their own pull times, so one can hang under
# load where another does not — a real reproducibility hole. The digest below is the build
# verified in production (serving on the fleet). To roll forward: bump the digest here, then
# verify on EVERY node before merge. Never revert this to a floating tag.
#
# Notes for Spark (GB10) vs Thor (Jetson): this script targets the Jetson Docker image.
# On a GB10 Spark you can run vLLM natively from its own aarch64 wheels instead -- the
# `vllm serve ...` argument block below is identical; drop the `docker run` wrapper and
# point at your local model path. See serving/SERVING.md.
set -euo pipefail

MODEL_PATH="${TAEY_MODEL_PATH:?set TAEY_MODEL_PATH to the full path of your HF model directory}"
MODELS_DIR="${TAEY_MODELS_DIR:-$(dirname "${MODEL_PATH}")}"
CACHE_DIR="${TAEY_CACHE_DIR:-$HOME/.cache}"
LORA_PATH="${TAEY_LORA_PATH:-}"
VLLM_PORT="${VLLM_PORT:-8000}"
GPU_UTIL="${VLLM_GPU_UTIL:-0.85}"
# Concurrency. These were HARDCODED to 8/8/8192 while taey-ep3.service set
# VLLM_MAX_NUM_SEQS=128 / VLLM_MAX_CUDAGRAPH=128 / VLLM_MAX_BATCHED_TOKENS=32768 — the unit
# configured the serve and the script ignored it, so the engine ran at 1/16th the intended
# concurrency. max-num-seqs 8 is exactly seven council seats plus main Taey, i.e. saturated by
# construction: one request got the whole engine and seven had to split it. Defaults below
# preserve the old behaviour for anyone with no env set; the unit supplies the real values.
MAX_NUM_SEQS="${VLLM_MAX_NUM_SEQS:-8}"
MAX_CUDAGRAPH="${VLLM_MAX_CUDAGRAPH:-8}"
MAX_BATCHED_TOKENS="${VLLM_MAX_BATCHED_TOKENS:-8192}"
# Served model id clients address (default: the model dir basename). Set explicitly so
# a redeploy on another Thor keeps the SAME id (e.g. Qwen3.6-27B-FP8) — a default basename
# would silently change the id (lowercased dir name) and break every consumer + the eval harness.
SERVED_NAME="${TAEY_SERVED_NAME:-$(basename "${MODEL_PATH}")}"
MAX_MODEL_LEN="${TAEY_MAX_MODEL_LEN:-16384}"
# Weight quantization. Decode on this hardware is memory-bandwidth-bound — every weight is read
# per generated token — so tokens/sec scales with how many BYTES the weights occupy, not with
# compute. On a bf16 27B (~55.6GB) that measured 3.56 tok/s single-stream on a Jetson AGX Thor.
# `fp8` halves the weight bytes and is hardware-accelerated on Blackwell-class parts (Thor reports
# compute capability 11.0, which has native FP8 tensor cores). Unset = serve the weights as stored.
# A pre-quantized checkpoint carries its own quantization_config and needs no value here.
QUANTIZATION="${TAEY_QUANTIZATION:-}"
VLLM_IMAGE="${VLLM_IMAGE:-ghcr.io/nvidia-ai-iot/vllm@sha256:b587dd56b4cb076209ad5156a626ac75f5a976d0e8e7d1e6a9fccd56d1bd65e8}"

echo "[vLLM] Serving model: ${MODEL_PATH}"
echo "[vLLM] Models dir:    ${MODELS_DIR} -> /models"
echo "[vLLM] Port: ${VLLM_PORT}, GPU util: ${GPU_UTIL}, image: ${VLLM_IMAGE}"

mkdir -p "${CACHE_DIR}/vllm-compile" "${CACHE_DIR}/triton" "${CACHE_DIR}/vllm"

# Speculative decoding. Decode here is memory-bandwidth-bound (every weight read per token), so the
# other way to go faster is MORE TOKENS PER WEIGHT-READ: draft several tokens, then have the target
# verify them in one pass. `ngram` drafts by matching repeated n-grams already in the prompt+output,
# which suits long generations over large repetitive contexts (guide redrafts, packet-heavy composes).
#
# USE method=ngram_gpu ON THIS IMAGE. Verified by import inside the pinned base: ngram_proposer_gpu
# loads fine, while the CPU ngram_proposer raises ModuleNotFoundError: numba (absent from the Jetson
# image). ngram_gpu needs no extra dependency, so this needs NO derived image and the digest pin stands.
#
# LOSSLESS BY CONSTRUCTION: drafted tokens are verified by the target through the same rejection
# sampler the non-speculative path uses (gpu_model_runner imports both RejectionSampler and
# NgramProposerGPU), so output matches what the model would have produced. Verify empirically with a
# greedy byte-identity check before trusting it — do not inherit the claim.
#
# Value is a JSON object, e.g.:
#   TAEY_SPECULATIVE_CONFIG='{"method":"ngram_gpu","num_speculative_tokens":5,"prompt_lookup_min":2,"prompt_lookup_max":8}'
# Unset = no speculative decoding (today's behaviour, byte-for-byte).
SPECULATIVE_CONFIG="${TAEY_SPECULATIVE_CONFIG:-}"

SPEC_ARGS=""
if [ -n "${SPECULATIVE_CONFIG}" ]; then
  echo "[vLLM] Speculative decoding: ${SPECULATIVE_CONFIG}"
  SPEC_ARGS="--speculative-config ${SPECULATIVE_CONFIG}"
fi

QUANT_ARGS=""
if [ -n "${QUANTIZATION}" ]; then
  echo "[vLLM] Weight quantization: ${QUANTIZATION}"
  QUANT_ARGS="--quantization ${QUANTIZATION}"
fi

# Quantization kernel-backend overrides, forwarded ONLY when actually set. vLLM VALIDATES these
# against an enum, so passing an empty value is NOT the same as not passing it -- `-e VAR=""`
# makes the engine abort with `Invalid value '' for VLLM_NVFP4_GEMM_BACKEND`. Build the -e flags
# conditionally so an unset override is genuinely absent from the container environment.
QUANT_ENV_ARGS=""
if [ -n "${VLLM_TEST_FORCE_FP8_MARLIN:-}" ]; then
  QUANT_ENV_ARGS="${QUANT_ENV_ARGS} -e VLLM_TEST_FORCE_FP8_MARLIN=${VLLM_TEST_FORCE_FP8_MARLIN}"
fi
if [ -n "${VLLM_NVFP4_GEMM_BACKEND:-}" ]; then
  QUANT_ENV_ARGS="${QUANT_ENV_ARGS} -e VLLM_NVFP4_GEMM_BACKEND=${VLLM_NVFP4_GEMM_BACKEND}"
fi

# LoRA adapters. TAEY_LORA_PATH takes ONE path or a COMMA-SEPARATED LIST — vLLM accepts several
# --lora-modules entries, and each becomes its own served model id addressable by name. Serving two
# adapters side by side against the same base is how one is evaluated against another (or against
# the bare base) without a restart between measurements, which otherwise makes the comparison a
# before/after across a process boundary rather than a true A/B.
# A single path behaves exactly as before.
LORA_ARGS=""
if [ -n "${LORA_PATH}" ]; then
  LORA_MODULES=""
  IFS=',' read -ra _LORA_LIST <<< "${LORA_PATH}"
  for _lp in "${_LORA_LIST[@]}"; do
    _lp="$(echo "$_lp" | xargs)"          # tolerate spaces after commas
    [ -n "$_lp" ] || continue
    _ln=$(basename "$_lp")
    echo "[vLLM] LoRA adapter: ${_lp} (name: ${_ln})"
    LORA_MODULES="${LORA_MODULES} ${_ln}=/models/${_ln}"
  done
  [ -n "$LORA_MODULES" ] && LORA_ARGS="--enable-lora --lora-modules${LORA_MODULES} --max-lora-rank 64"
fi

exec docker run \
  --name taey-vllm \
  --runtime nvidia \
  --network host \
  --ipc=host \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  --health-cmd="curl -sf http://localhost:${VLLM_PORT}/v1/models || exit 1" \
  --health-interval=30s \
  --health-timeout=10s \
  --health-retries=3 \
  --health-start-period=600s \
  -v "${MODELS_DIR}:/models" \
  -v "${CACHE_DIR}/vllm-compile:/root/.cache/vllm-compile" \
  -v "${CACHE_DIR}/triton:/root/.triton/cache" \
  -v "${CACHE_DIR}/vllm:/root/.cache/vllm" \
  -e TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas \
  -e TRITON_CACHE_DIR=/root/.triton/cache \
  -e TORCHINDUCTOR_CACHE_DIR=/root/.cache/vllm-compile/inductor \
  -e TORCHINDUCTOR_FX_GRAPH_CACHE=1 \
  -e TORCHINDUCTOR_AUTOGRAD_CACHE=1 \
  -e VLLM_CACHE_ROOT=/root/.cache/vllm \
  ${QUANT_ENV_ARGS} \
  "${VLLM_IMAGE}" \
  vllm serve "/models/$(basename "${MODEL_PATH}")" \
    --served-model-name "${SERVED_NAME}" \
    --host 0.0.0.0 \
    --port "${VLLM_PORT}" \
    --max-model-len "${MAX_MODEL_LEN}" \
    --gpu-memory-utilization "${GPU_UTIL}" \
    --enable-prefix-caching \
    --kv-cache-dtype fp8 \
    --max-num-seqs ${VLLM_MAX_NUM_SEQS:-8} \
    --max-cudagraph-capture-size ${VLLM_MAX_CUDAGRAPH:-8} \
    --max-num-batched-tokens ${VLLM_MAX_BATCHED_TOKENS:-8192} \
    --reasoning-parser qwen3 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_xml \
    ${QUANT_ARGS} \
    ${SPEC_ARGS} \
    ${LORA_ARGS}
