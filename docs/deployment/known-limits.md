# Known Limits

> What this kit does NOT claim. Maintained as a cannot-lie discipline
> document — if you find a claim elsewhere in this repo that contradicts
> something here, the entry here is the truth and that claim is a bug
> (please file an issue).

---

## 1. NCCL bandwidth on the corrected dual-rail config is NOT YET MEASURED

The NCCL recipe in `nccl-roce-recipe.md` is verified to **enable** dual-rail
RoCE (vs. silent single-rail with the buggy lowercase device name) and to
**stabilize 4-Spark FSDP through-epoch training** (vs. NCCL watchdog timeouts
with defaults).

It does NOT yet have an attached `busbw` measurement. No nccl-tests output
file exists in our archives. Production training to date ran on the buggy
single-rail form. The corrected form has not been benchmarked.

Earlier drafts of related forum posts cited specific bandwidth numbers
(e.g. "22.9 GB/s") that turned out to have been fabricated from synthetic
training data. Those numbers were scrubbed. **Do not cite a bandwidth number
from this kit. Measure your own with
`mpirun … nccl-tests/build/all_gather_perf …` and save the output.**

## 2. The two vLLM PRs integrated here are OPEN, NOT MERGED

The fix logic distributed in this kit comes from:

- [vllm-project/vllm#36325](https://github.com/vllm-project/vllm/pull/36325) —
  FLA Hopper/TMA misclassification on SM12x; author **Rks2302**; state OPEN at
  time of this kit's first publication.
- [vllm-project/vllm#31740](https://github.com/vllm-project/vllm/pull/31740) —
  SM121/GB10 (DGX Spark) Blackwell-class GPU support; author **seli-equinix**;
  state OPEN at time of this kit's first publication.

**Neither PR is by me.** I integrated and validated them on real GB10
hardware before NVIDIA's official DGX Spark vLLM Docker image landed in
March 2026. The honest framing for hiring readers: I am the one who picked
the candidate upstream patches, ran them in production, characterized their
behavior on the actual hardware, and built the surrounding recipe + rescue
bank. I am not the one who wrote the patches.

If you read a claim in this repo or in associated public posts that says "I
contributed upstream PRs" without further qualification, **that is wrong**.
The honest claim is "I validated bleeding-edge external upstream candidate
patches on novel hardware in production before the official path existed."

Once the PRs land upstream and ship in a vLLM release, large parts of this
kit become unnecessary. That is the point.

## 3. Single-host inference + multi-node FSDP training only

- **Inference serving** in this kit is **single-host, single-process per
  host**. There is no multi-host inference coordination claim. Concurrent
  request handling on a single-process server beyond its memory budget will
  OOM-kill the process; the example systemd unit handles auto-recovery but
  does not multi-instance load-balance.
- **Training** via this kit's NCCL recipe is verified to work on a 4-node
  GB10 FSDP cluster. Multi-node training is the use case the recipe
  stabilizes.

## 4. Verified longest continuous session: ~30 minutes

Per `feedback_2hr_reboot_cycle.md` in our internal logs, every training
session on this hardware reboots after ~200 steps (~30 min of compute) due
to UMA fragmentation on the 128GB unified-memory architecture.

- Longest verified single continuous session: ~30 minutes
- Cumulative campaign training: ~9 hours of compute spread over ~26 hours
  wall time

If you see claims of "multi-day continuous training" or "10M+ collectives" or
"36-72 hour stability" attributed to this work, those are **wrong**. The
honest claim is "stable through-epoch training with bounded sessions and
clean session-boundary saves."

## 5. The SIGSEGV in `ncclLocalOpAppend` we did NOT reproduce

Some NVIDIA forum posts mention a `SIGSEGV in ncclLocalOpAppend` under
sustained load on GB10. **We never reproduced that crash on our cluster.**

Acceptable framing if you're triaging a dev report: "The SIGSEGV in
ncclLocalOpAppend you reported is consistent with the GDR fallback path
under sustained CPU-proxy load." That credits the original observer and
doesn't overclaim.

Unacceptable framing: "We hit the same crash." We didn't.

## 6. The 4-node cluster was our development environment, not a hosted service

This kit is shipped from a working production stack but the cluster itself
is not a service anyone else can reach. Operator-specific paths (`/home/.../`),
hostnames (`thor1`, `spark2`, etc.), and IP addresses (`10.0.0.x`) have been
stripped from this repo. If you find one that leaked, please file an issue —
that's a bug.

## 7. Audit harness / capability battery is in a sibling repo

The 45+ probe capability battery used to validate the MoE serving stack is
in `edge-llm-validation-harness` (forthcoming). The training recipes — MoE
LoRA, DPO refinement, Config A2 keystone attention — are in
`palios-taey/research` (training-stack/). This repo is the iron substrate
only; the validation and training stacks build on top.

## 8. The "+1.9pp DPO" claim that appears in the related research repo

The sibling research repo (`palios-taey/research`, training-stack/) cites a
+1.9 percentage point pass-rate improvement from a Config A2 keystone
attention LoRA + DPO refinement.

**Cannot-lie register translation** (per the methodology disclosure in
`research/training-stack/README.md`):

> +1.9pp is `[Observed against fixed in-house probes]`, NOT
> `[Observed against held-out independent generalization data]`. There is no
> held-out test set — the 163 probes are the full eval surface. DPO training
> pairs are distinct from audit probes (no train-on-test leakage in the
> conventional sense) BUT probes + DPO corpus were authored by the same
> team (confounder a reader should know about). Independent verification
> path: clone the audit harness, run audit_pipeline.py against your own
> bake.

This kit does not republish that claim with stronger framing than the source
repo allows. If you see a stronger claim in our public artifacts, it is a
bug — see `postmortems/cannot-lie-roundtrip-2026-06-15.md` for how that
specific risk was caught and addressed in 17 minutes via in-team verification
discipline.
