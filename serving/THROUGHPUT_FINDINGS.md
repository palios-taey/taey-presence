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

**The roofline, empirically confirmed on this hardware.** Note the byte count: 55.6 GB is the
ON-DISK size, but the vision tower is not read during text decode and the input embedding is a
one-row gather, so the real per-token traffic is **~41-46 GB** (see Corrections below).

| weights | on-disk | real bytes/token | tuned ceiling | measured |
|---|---|---|---|---|
| bf16 (current) | 55.6 GB | ~43.5 GB | **~5.2 tok/s** | **4.64–4.66 tok/s (~87%)** |
| fp8 | ~28 GB | ~22 GB | ~10 tok/s | not achievable on this build — FINDING 3 |
| nvfp4 | ~14 GB | ~11 GB | ~20 tok/s | untested; sm_110 support doubtful |

We are at **~87% of the tuned bf16 ceiling** (an earlier revision of this document said 95%, computed
against the inflated on-disk byte count). **No configuration change beats that by much** — the
remaining levers are fewer weight bytes, fewer tokens generated, or more tokens per weight-read
(speculative decoding / concurrency).

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

## FINDING 4 — MAXN power mode (small, and there is a reason it is small)

The Thors default to the **120W** power mode; **MAXN (nvpmodel ID 0)** is available and applies
without a reboot. Measured on Thor1 with clocks already pinned:

| mode | tok/s |
|---|---|
| 120W | 4.30 |
| MAXN | 4.45 |

**+3.5% from a single measurement — suggestive, not established** (repeat runs did not complete;
treat as provisional). The reason it is small is physical and worth stating: `jetson_clocks` had
already pinned EMC to its 4266 MHz max, so **memory bandwidth — the actual bottleneck — was already
maxed**. MAXN raises the *compute* power ceiling, and compute is not what limits a memory-bound
decode. The two levers overlap; the clock pin captured most of it.

## FINDING 5 — speculative decoding is NOT blocked (correcting an earlier conclusion)

A previously recorded conclusion held that ngram speculative decode was unavailable because `numba`
is absent from the Jetson vLLM image, and therefore "not a near-term lever." That checked whether
numba was MISSING and never whether it was INSTALLABLE. Verified inside the pinned image:

```
python3 -c "import numba"      -> ModuleNotFoundError   (genuinely absent)
pip download numba --no-deps   -> numba-0.66.0-cp312-cp312-...aarch64.whl
                                  Successfully downloaded numba
```

**A wheel exists for this arch/python.** So it is a *derived-image build step*, not a dead end: build
`FROM` the pinned digest with numba installed, pin the derived image by its own digest (preserving the
pinned-digest discipline), then serve with an ngram `--speculative-config`. Do not pip-install at
container start — that is unpinned and fragile.

**Why this outranks quantization on this model:** speculative decoding is the only multiplicative
lever that is **output-identical to the target model by construction** — the target verifies every
token — so it costs *exactly zero* fidelity. Every quantization option trades some, and on a
behavioral fine-tune that trade is the one we cannot afford. External measurement on this hardware
class: 6.27 -> 16.19 tok/s at concurrency 1 with EAGLE-3. ngram needs no draft model at all.

## Corrections to earlier analysis in this document

- **Bytes/token was overstated ~20%.** 55.6 GB is the ON-DISK size. The vision tower is not read
  during text decode and the input embedding is a one-row gather, so the real per-token figure is
  ~41-46 GB. Against an externally calibrated ~225 GB/s achieved on this silicon, the tuned bf16
  ceiling is **~5.2 tok/s** and the measured 4.64 is **~87% of it — more headroom than the 95%
  claimed above**, which was computed against an inflated byte count.
- **Concurrency was dismissed too quickly.** "Batching cannot mask single-stream latency" is true for
  one sequential walk and false for the operation: a backlog of scored rows against a
  handful-per-day output is **throughput-limited, not latency-limited**. External measurement on this
  hardware: 41.5 tok/s at concurrency 8 vs 6.27 at concurrency 1 (~6.6x aggregate). `--max-num-seqs 8`
  is already set, so the engine is ready and the CLIENT is serializing.
- **A quantization safety note that outranks throughput:** on this architecture class, older builds
  reportedly **silently miscomputed** where the current one refuses to launch. The FP8 crash in
  FINDING 3 was therefore the *safe* failure mode. Silent miscompute is invisible to a tok/s
  measurement and fatal to a behavioral fine-tune — which is why any quantized build must prove
  itself by output-diff against bf16 on real work, never by a throughput number.

## Ranked levers, by measured or predicted value

| lever | value | status |
|---|---|---|
| `enable_thinking: false` on routine trained paths | **10.8x wall-clock** | measured; client-side flag |
| pin clocks at serve start | 1.30x on an idle node, +1.3% on a busy one | measured; landed in-tree |
| ngram / EAGLE speculative decode | 1.3–2.5x predicted, **zero fidelity cost by construction** | unblocked: derived image + numba (FINDING 5) |
| client-side concurrency for independent work | up to ~6.6x aggregate on this hardware | engine already configured; client serializes |
| offline FP8 checkpoint | ~2x predicted | needs offline build + a production output-diff quality gate |
| MAXN power mode | +3.5% provisional | applied on the bench (FINDING 4) |
| nvfp4 | ~4x predicted ceiling | untested; sm_110 kernel support doubtful given FINDING 3 |
| batching independent work | workload-dependent | does not help the sequential walk path |
