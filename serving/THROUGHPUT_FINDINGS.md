# Decode throughput on Jetson AGX Thor — measured findings

*Measured 2026-07-25 on the production serving nodes. Every number here is from a real serve with a
real request, not a benchmark harness. Where something is unmeasured it says so.*

## The substrate, and what it means for decode

- **NVIDIA Jetson AGX Thor**, 122 GB unified LPDDR5X shared CPU+GPU, GPU compute capability **11.0**
  (Blackwell-class, sm_110). Vendor peak memory bandwidth **273 GB/s**.
- Served model: dense 27B, 64 layers, GQA 24 query / 4 KV heads, hybrid linear + full attention,
  **55.6 GB in bf16**.
- Autoregressive decode at batch 1 reads **every weight for every token**, so
  `tok/s ≈ memory_bandwidth / weight_bytes`. Decode here is **memory-bandwidth-bound, not
  compute-bound** — the SMs idle waiting on the bus.

**The roofline, now empirically confirmed on this hardware:**

| weights | bytes | theoretical ceiling | measured |
|---|---|---|---|
| bf16 (current) | 55.6 GB | 4.91 tok/s | **4.64–4.66 tok/s (95%)** |
| fp8 | 27.8 GB | 9.82 tok/s | not yet achievable — see below |
| nvfp4 | 13.9 GB | 19.64 tok/s | untested |

We are at ~95% of the bf16 ceiling. **No configuration change can meaningfully beat that** — the
remaining levers are fewer weight bytes, or fewer tokens generated.

## FINDING 1 — the EMC clock was sagging (fixed, in-tree)

The Thors were never running `jetson_clocks`, so the **EMC (memory-controller) clock — which IS
decode bandwidth** — was left to devfreq and dropped on an idle node: **2.75 GHz against a 4.27 GHz
max, 64% of peak bandwidth**, with a `devfreq` kworker burning ~20% CPU doing the scaling.

Measured, same prompt before/after:

| node state | before | after | delta |
|---|---|---|---|
| idle-ish (Thor1) | 3.56 tok/s (198 GB/s) | **4.64 tok/s (258 GB/s)** | **1.30x** |
| saturated (Thor2) | 4.60 tok/s | 4.66 tok/s | +1.3% |

**It does not speed up a busy node — it prevents the sag.** A saturated node's clock is already
boosted; an idle one's has dropped and pays a ramp on the next request. Pinned at serve start via
`ExecStartPre=-/usr/bin/jetson_clocks` (does not survive reboot on its own, which is why it lives in
the unit). Revert on a node: `sudo jetson_clocks --restore <stored.conf>`.

**This also explained a discrepancy** we had been treating as measurement noise: 3.56 vs 4.5 tok/s on
the same weights was an idle node's clock sagging, not jitter. An external reviewer flagged that the
spread was too large to be noise; they were right.

*(Unmeasured hypothesis, deliberately not claimed: this should help BURSTY patterns most — a UI walk
that judges one element, observes, then judges the next, letting the clock sag in each gap. A clean
control will exist naturally once post-pin walks accumulate in the same ledger field.)*

## FINDING 2 — thinking-on burns ~10x the tokens for the same answer (the biggest lever)

Same prompt, same model, both terminated cleanly, comparable answer quality:

| mode | tokens | wall-clock | rate |
|---|---|---|---|
| thinking ON (flag absent) | 650 | 150.7 s | 4.31 tok/s |
| thinking OFF (`enable_thinking: false`) | **64** | **14.0 s** | 4.57 tok/s |

**10.8x wall-clock.** The token *rate* is identical — thinking-on simply generates ~10x the tokens to
reach the same answer, of which 2625 characters were reasoning the task did not need.

**The serving contract behind it:** in this model's `chat_template.jinja`,

```jinja
{%- if enable_thinking is defined and enable_thinking is false %}
    {{- '<think>\n\n</think>\n\n' }}     {# pre-closed — thinking OFF #}
{%- else %}
    {{- '<think>\n' }}                    {# OPEN block — the default when the flag is ABSENT #}
{%- endif %}
```

An **absent** flag does not mean "off" — it takes the else branch and leaves `<think>` **open**, so
the model must fill and close it before answering. Any caller that omits the flag pays the ~10x.
Callers on routine, already-trained work should pass `enable_thinking: false` explicitly.

## FINDING 3 — on-the-fly FP8 quantization is DEAD on this build (negative result)

`--quantization fp8` (online weight quantization) **fails at engine init on sm_110**, both kernel
paths:

| attempt | selected kernel | outcome |
|---|---|---|
| default | `CutlassFP8ScaledMMLinearKernel` | `RuntimeError: Triton Error [CUDA]: unspecified launch failure` |
| `VLLM_TEST_FORCE_FP8_MARLIN=1` | Marlin | failed in `ops.gptq_marlin_repack` |

Root cause (per external research, consistent with what we observed): sm_110 diverges from sm_100 /
sm_121; generic low-precision kernels target the wrong instruction set and either crash or fall back
to unoptimized paths. **Do not retry online FP8 on this image** — the env hooks (`TAEY_QUANTIZATION`,
`VLLM_TEST_FORCE_FP8_MARLIN`, `VLLM_NVFP4_GEMM_BACKEND`) are wired and inert when unset, kept so the
next attempt is a one-line change rather than a rediscovery.

**FP8 is still the right direction — built differently.** A **pre-quantized** checkpoint loads fine
(a 29 GB fp8/e4m3/dynamic build of this architecture already sits on a node and serves), because its
weights are fp8 on disk with a `quantization_config` and never touch the online repack that crashed.
The path is an **offline** quantization of the fine-tune (`llm-compressor` `FP8_DYNAMIC`, no
calibration data required) producing a ~28 GB checkpoint, then serving that.

**Hard gate before any quantized build serves production:** quantizing a *fine-tuned* model can
degrade exactly the judgment the pipeline depends on. It proves itself in production against bf16 on
the same real work — same job scored, same walk judged, outputs compared — not on a benchmark. If
judgment quality moves at all, bf16 stays.

## Ranked levers, by measured or predicted value

| lever | value | status |
|---|---|---|
| `enable_thinking: false` on routine trained paths | **10.8x wall-clock** | measured; client-side flag |
| pin clocks at serve start | 1.30x on an idle node, +1.3% on a busy one | measured; landed in-tree |
| offline FP8 checkpoint | ~2x predicted (9.82 tok/s ceiling) | blocked on an offline build + quality gate |
| ngram speculative decode | 1.3–2x predicted, no behavioral risk | blocked: `numba` absent from the Jetson image |
| nvfp4 | ~4x predicted ceiling | untested; sm_110 kernel support doubtful given FINDING 3 |
| batching independent work | workload-dependent | does not help the sequential walk path |
