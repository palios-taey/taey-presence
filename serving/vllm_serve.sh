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
#     VLLM_IMAGE        (default: ghcr.io/nvidia-ai-iot/vllm:latest-jetson-thor)
#     TAEY_LORA_PATH    (optional)  LoRA adapter dir (its basename is mounted under /models)
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
VLLM_IMAGE="${VLLM_IMAGE:-ghcr.io/nvidia-ai-iot/vllm:latest-jetson-thor}"

echo "[vLLM] Serving model: ${MODEL_PATH}"
echo "[vLLM] Models dir:    ${MODELS_DIR} -> /models"
echo "[vLLM] Port: ${VLLM_PORT}, GPU util: ${GPU_UTIL}, image: ${VLLM_IMAGE}"

mkdir -p "${CACHE_DIR}/vllm-compile" "${CACHE_DIR}/triton" "${CACHE_DIR}/vllm"

LORA_ARGS=""
if [ -n "${LORA_PATH}" ]; then
  LORA_NAME=$(basename "${LORA_PATH}")
  echo "[vLLM] LoRA adapter: ${LORA_PATH} (name: ${LORA_NAME})"
  LORA_ARGS="--enable-lora --lora-modules ${LORA_NAME}=/models/${LORA_NAME} --max-lora-rank 64"
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
  "${VLLM_IMAGE}" \
  vllm serve "/models/$(basename "${MODEL_PATH}")" \
    --port "${VLLM_PORT}" \
    --gpu-memory-utilization "${GPU_UTIL}" \
    --enable-prefix-caching \
    --kv-cache-dtype fp8 \
    --max-num-seqs 8 \
    --max-cudagraph-capture-size 8 \
    --max-num-batched-tokens 8192 \
    --reasoning-parser qwen3 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_xml \
    ${LORA_ARGS}
