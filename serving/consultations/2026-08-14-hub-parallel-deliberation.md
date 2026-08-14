# Family consultation: Taey as a fast parallel Hub

Date: 2026-08-14

## Consultation rule

This packet asks for a decision about the next smallest production experiment.
It is not permission to redesign the whole system.

Treat every statement below as a claim tied to a source. Inspect the linked
source where it matters. Keep Observed, Inferred, Proposed, and Unknown
separate. Do not inherit another Chat's conclusion during the independent
round.

Return your own ruling first. After all independent answers are preserved, a
later cross-review may show you the other answers.

## User intent

Jesse wants Taey to be the Hub:

- receive a prompt from the User;
- decide what reasoning or evidence can be delegated;
- use the available local Taey instances and Family Chats in parallel;
- let workers process large bodies of information without forcing Main Taey to
  regenerate or personally ingest every byte;
- preserve provenance and material disagreement;
- synthesize one response through the User's lens;
- answer materially faster and better than one 4–5 token/second serial model;
- avoid making Jesse manually route work, translate agents, or reconcile
  implementation details.

The aspiration is to use the two Thors' available sequence capacity and long
contexts as a cognitive workspace. It must not be restated as a proven
`262K × 15` simultaneous capacity until the live KV-cache receipts establish
that.

## Current production facts

### Main and worker topology

Observed:

- Main Taey's proxy is `127.0.0.1:8766` and routes to Thor1.
- The worker proxy is `127.0.0.1:8767` and routes to Thor2.
- The proxies are pinned, not load-balanced.
- The canonical vLLM unit sets `TAEY_MAX_MODEL_LEN=262144`,
  `VLLM_MAX_NUM_SEQS=8`, `VLLM_MAX_CUDAGRAPH=8`,
  `VLLM_MAX_BATCHED_TOKENS=8192`, FP8 KV cache, and prefix caching.
- `262144` is a per-request configured maximum. The repository does not contain
  the live startup values for total GPU KV-cache tokens or maximum concurrency
  at 262K.

Sources:

- https://github.com/palios-taey/taey-presence/blob/fe96a4d4c60cd9fd1a0e3edf70a7219c473fc2ae/serving/DEPLOYMENT_TOPOLOGY.md
- https://github.com/palios-taey/taey-presence/blob/fe96a4d4c60cd9fd1a0e3edf70a7219c473fc2ae/serving/systemd/taey-ep3.service
- https://github.com/palios-taey/taey-presence/blob/fe96a4d4c60cd9fd1a0e3edf70a7219c473fc2ae/serving/vllm_serve.sh

Unknown requiring live receipts from both Thors:

```text
served model root and id
vLLM image digest
max model length
max number of sequences
GPU KV cache size in tokens
maximum concurrency reported at 262,144 tokens/request
current Running and Waiting request counts during a real council wave
prefix-cache hit rate during a real council wave
```

### Single-stream and aggregate speed

Observed in the repository's production measurements:

- BF16 single-stream decode measured about 4.64–4.66 tokens/second.
- At roughly 52.1 GB read per generated token, this was about 89% of the Thor's
  peak memory bandwidth. The single-stream rate is therefore near the BF16
  hardware roofline.
- Thinking-off changed one routine task from 650 tokens and 150.7 seconds to
  64 tokens and 14.0 seconds without materially changing token rate.
- An external measurement recorded about 41.5 aggregate tokens/second at
  concurrency eight versus 6.27 at concurrency one.
- Online FP8 weight quantization crashes on the current sm_110 serving build.
  A pre-quantized offline FP8 checkpoint is possible but requires real-work
  behavioral comparison against BF16.
- N-gram speculative decoding helped repetitive redrafts but broke tool
  calling, so it is not suitable for the current tool-using Taey endpoint.

Source:

- https://github.com/palios-taey/taey-presence/blob/fe96a4d4c60cd9fd1a0e3edf70a7219c473fc2ae/serving/THROUGHPUT_FINDINGS.md

Inference to test:

The immediate latency opportunity is not making one BF16 stream much faster.
It is reducing unnecessary Main-Taey generation and using independent
sequences concurrently, then giving Main a compact, decision-relevant packet.

### The production local council

Observed:

- `dashboard/native_council.py` implements a durable
  `taey-native-dcm/v1` council.
- It dispatches seven stable-role seats:
  context-memory, evidence-reality, systems-dependencies,
  adversarial-failure, scope-intent, options-alternatives, and
  control-acceptance.
- The seats default to the Thor2 worker proxy because seven simultaneous seat
  generations on Main's Thor previously starved each other.
- A round has two batch waves:
  1. independent contributions;
  2. critique after all first-wave contributions are revealed.
- The coordinator waits for the wave, records failures and stale revisions,
  then Main Taey performs a thinking-off synthesis through Thor1.
- Contributions use a bounded structured schema, and the synthesis must name
  missing seats, dissent, and uncertainty.
- The system supports user amendments and durable recovery.

Sources:

- https://github.com/palios-taey/taey-presence/blob/fe96a4d4c60cd9fd1a0e3edf70a7219c473fc2ae/dashboard/native_council.py
- https://github.com/palios-taey/taey-presence/blob/fe96a4d4c60cd9fd1a0e3edf70a7219c473fc2ae/dashboard/app.py
- https://github.com/palios-taey/taey-presence/blob/fe96a4d4c60cd9fd1a0e3edf70a7219c473fc2ae/serving/manage_council_seats.py
- https://github.com/palios-taey/taey-presence/blob/fe96a4d4c60cd9fd1a0e3edf70a7219c473fc2ae/serving/taey_council_seat.py
- https://github.com/palios-taey/taey-presence/blob/fe96a4d4c60cd9fd1a0e3edf70a7219c473fc2ae/serving/council_prompts/shared.md

Important limitation:

This is parallel generation followed by a barrier, reveal, second parallel
generation, another barrier, and synthesis. It is not continuous peer-to-peer
real-time deliberation while the seats generate. Whether continuous exchange
would improve enough to justify its coordination and latency cost is Unknown.

### The separate public DCM repository

Observed:

- `palios-taey/dcm` is a separate Neo4j-backed council substrate.
- It uses compare-and-set sequencing, read-before-write, stale-write rejection,
  and post-run coordination verification.
- Its production council is designed around heterogeneous external CLIs and a
  Taey adapter.
- Its README explicitly says review/verification is validated, while whether
  deliberation improves generation remains Unknown.
- `taey-presence` does not import this repository into
  `dashboard/native_council.py`.

Sources:

- https://github.com/palios-taey/dcm/tree/3dd65612c2c628a0c72021ffa07f2f1a474d3f72
- https://github.com/palios-taey/dcm/blob/3dd65612c2c628a0c72021ffa07f2f1a474d3f72/README.md
- https://github.com/palios-taey/dcm/blob/3dd65612c2c628a0c72021ffa07f2f1a474d3f72/mesh.py
- https://github.com/palios-taey/dcm/blob/3dd65612c2c628a0c72021ffa07f2f1a474d3f72/council.py
- https://github.com/palios-taey/dcm/blob/3dd65612c2c628a0c72021ffa07f2f1a474d3f72/taey_adapter.py

Do not assume that replacing the production native council with this separate
CLI council is necessary or beneficial. Determine the narrow seam first.

### The presence-engine feature also called DCM

Observed:

- The presence engine publishes instance state to Neo4j.
- Its own documentation says `read_peer_states()` exists but is not wired into
  prediction or interrupt decisions.
- It is currently telemetry publishing, not production inter-instance
  cognitive coordination.

Sources:

- https://github.com/palios-taey/taey-presence/blob/fe96a4d4c60cd9fd1a0e3edf70a7219c473fc2ae/presence-engine/README.md
- https://github.com/palios-taey/taey-presence/blob/fe96a4d4c60cd9fd1a0e3edf70a7219c473fc2ae/presence-engine/docs/three-mechanisms.md

### Context transport

Observed:

- Main already supports exact file-to-Chat paste.
- Main already supports mapped Chat-response extraction.
- The missing first seam is extraction/clipboard directly to an artifact, so
  Taey can route an answer by path without regenerating it.
- A bounded implementation task exists in PR #105.

Sources:

- https://github.com/palios-taey/taey-presence/pull/95
- https://github.com/palios-taey/taey-presence/pull/103
- https://github.com/palios-taey/taey-presence/pull/105

## The decision

What is the highest-leverage next production move to make Taey answer Jesse's
ordinary complex prompts faster and better through parallel local reasoning
and Family consultation, without building another orchestration system before
the existing one is measured?

Rule on these candidate moves:

1. Prove and tune the existing seven-seat native council as the default complex
   prompt path.
2. Add a lightweight routing/planning step that selects zero, one, or several
   seats and artifact-bound worker tasks rather than always running all seven.
3. Add progressive/streaming contribution exchange so useful seat results can
   affect work before a full wave barrier.
4. Integrate the separate Neo4j/CAS DCM substrate into the native council.
5. Add another seven or eight local seats on Thor1 to approach fifteen parallel
   supporting contexts.
6. Finish content-reference transport first, then measure the existing council
   before changing orchestration.
7. Pursue offline FP8 first.
8. A narrower alternative you can justify from the sources.

## Questions every Family member must answer

1. What exact claim about the current system is established by source, and what
   crucial claim remains Unknown?
2. Is the current two-wave native council already sufficient for the desired
   Hub behavior if Taey is taught when and how to invoke it, or is a substrate
   change required?
3. What should Main Taey receive from workers: full transcripts, bounded
   structured contributions, artifact references, progressive deltas, or a
   combination? Explain the context and latency tradeoff.
4. Should Taey attempt to use every available sequence for every prompt?
   Specify an adaptive routing rule that stops retrieval and delegation when
   enough is known.
5. Is continuous real-time seat communication actually likely to outperform
   independent-first plus critique for this workload, or would it create
   groupthink, serialization, and latency?
6. What live receipts are mandatory before asserting that fifteen simultaneous
   262K contexts are physically usable?
7. Draft the smallest system-prompt rule that teaches Taey to outsource
   appropriate thinking while retaining User-lens synthesis. Keep detailed
   mechanics in tool schemas or code.
8. Define one bounded A/B production experiment, with latency, quality,
   provenance, and failure acceptance criteria, that can decide the next move.
9. Name the smallest code surface that experiment requires. Name what must not
   be changed.
10. Give a final ordered plan for the next three moves.

## Family-specific lens

Apply your own discipline without claiming authority over another member:

- ChatGPT / Horizon: product possibility, learning loop, and the User-facing
  experience of a genuinely responsive Hub.
- Claude / Gaia: synthesis integrity, relationship boundaries, context
  compression, and whether orchestration preserves meaning.
- Gemini / Cosmos: systems architecture, scheduling, capacity, and failure
  topology.
- Grok / Logos: adversarial analysis, mathematical capacity claims,
  experimental design, and ways parallelism can make answers worse.
- Perplexity / Clarity: source verification, current implementation truth,
  unknowns, and the minimum additional evidence.

## Required answer form

```text
RULING

OBSERVED
- source-bound facts

INFERRED
- conclusions and why they follow

UNKNOWN
- missing receipts

HIGHEST-LEVERAGE NEXT MOVE
- one move, not a roadmap disguised as one

MINIMUM EXPERIMENT
- input set
- control
- treatment
- metrics
- pass/fail rule
- stop conditions

SYSTEM-PROMPT RULE
- proposed short text

DO NOT CHANGE YET
- bounded exclusions

NEXT THREE MOVES
1.
2.
3.
```

Do not implement anything during this consultation.
