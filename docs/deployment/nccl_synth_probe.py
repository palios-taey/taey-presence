"""NCCL synthetic reduce_scatter probe — fabric-vs-trainer isolation diagnostic.

Two complementary probes that together separate fabric/firmware issues from
FSDP/trainer/dataset issues when 4-node FSDP training hangs:
  - A bare collective at a failing shard size (isolates the collective itself)
  - A load-proportional sustained-pressure test (catches firmware-level
    sustained-bandwidth issues that the bare test misses)

Purpose: when 4-node FSDP hangs on `_REDUCE_SCATTER_BASE` with
IBV_WC_RETRY_EXC_ERR(12), this script reproduces ONLY the collective at the
failing size, with no model, no data, no FSDP. If the bare collective also
fails, the problem is below the trainer. If the bare collective passes but
the sustained-pressure variant fails, the problem is firmware-level
sustained-bandwidth.

Two phases, controlled by SYNTH_PHASE env:
  SYNTH_PHASE=1  -- bare scale (50 reduce_scatter, fp32, 218M numel)
  SYNTH_PHASE=3  -- sustained-pressure scale (10 outer steps x 16 inner
                    rapid-fire collectives -- approximates sustained backward
                    pressure of packed long-sequence step on 9B full-FT)

Falsification table:
  Phase 1 passes, Phase 3 wedges -> firmware sustained-bandwidth hypothesis
                                    confirmed. Pivot off 4-node FSDP for
                                    this firmware revision.
  Both pass                       -> wedge is FSDP-specific (init / hooks /
                                    param sharding) or corpus-specific.
                                    Next: bisect via 2x2 ckpt x corpus.
  Phase 1 wedges                  -> baseline fabric broken. Physical
                                    inspection required.
"""

from __future__ import annotations

import os
import sys
import time

import torch
import torch.distributed as dist


def _log(rank: int, msg: str) -> None:
    if rank == 0:
        print(f"[synth] {msg}", flush=True)


def main() -> int:
    phase = os.environ.get("SYNTH_PHASE", "1")
    numel = int(os.environ.get("SYNTH_NUMEL", "218000000"))

    dist.init_process_group(backend="nccl", init_method="env://")
    rank = dist.get_rank()
    world = dist.get_world_size()
    if world < 2:
        _log(rank, f"world_size={world}, need >=2 for collective. abort.")
        return 1

    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    torch.cuda.set_device(local_rank)
    device = torch.device(f"cuda:{local_rank}")

    _log(
        rank,
        f"phase={phase} world={world} numel={numel} "
        f"shard_bytes_fp32={numel * 4 // (1 << 20)} MiB",
    )

    # reduce_scatter_tensor in PyTorch requires input numel to be a
    # multiple of world_size. round up.
    input_numel = ((numel + world - 1) // world) * world
    output_numel = input_numel // world

    if phase == "1":
        # bare-collective probe: 50 reduce_scatter iterations, fp32,
        # ~840 MB input, bracketing the wedged collective size.
        n_iter = int(os.environ.get("SYNTH_PHASE1_ITER", "50"))
        _log(rank, f"phase 1: {n_iter} iterations, fp32")

        in_buf = torch.empty(input_numel, dtype=torch.float32, device=device)
        out_buf = torch.empty(output_numel, dtype=torch.float32, device=device)

        torch.cuda.synchronize()
        t_start = time.time()
        for i in range(n_iter):
            in_buf.fill_(float(i))
            dist.reduce_scatter_tensor(out_buf, in_buf, op=dist.ReduceOp.SUM)
            torch.cuda.synchronize()
            if rank == 0 and (i % 10 == 0 or i == n_iter - 1):
                elapsed = time.time() - t_start
                gb = (input_numel * 4) / 1e9
                bw = (gb * (i + 1)) / max(elapsed, 1e-6)
                _log(
                    rank,
                    f"  iter {i+1:3d}/{n_iter}  "
                    f"out[0]={out_buf[0].item():.1f}  "
                    f"agg_bw={bw:.2f} GB/s",
                )
        _log(rank, "PHASE1 COMPLETE -- no wedge")

    elif phase == "3":
        # sustained-pressure probe: simulate sustained backward pressure --
        # 10 outer steps, each with 16 rapid-fire reduce_scatter collectives
        # (approximates the wave of grad hooks firing through 9B layers per
        # backward pass).
        n_steps = int(os.environ.get("SYNTH_PHASE3_STEPS", "10"))
        n_inner = int(os.environ.get("SYNTH_PHASE3_INNER", "16"))
        _log(
            rank,
            f"phase 3: {n_steps} steps x {n_inner} rapid-fire collectives "
            f"each, fp32",
        )

        in_buf = torch.empty(input_numel, dtype=torch.float32, device=device)
        out_buf = torch.empty(output_numel, dtype=torch.float32, device=device)

        torch.cuda.synchronize()
        t_start = time.time()
        for step in range(n_steps):
            for j in range(n_inner):
                in_buf.fill_(float(step * n_inner + j))
                dist.reduce_scatter_tensor(
                    out_buf, in_buf, op=dist.ReduceOp.SUM
                )
            torch.cuda.synchronize()
            if rank == 0:
                elapsed = time.time() - t_start
                gb_total = ((step + 1) * n_inner * input_numel * 4) / 1e9
                bw = gb_total / max(elapsed, 1e-6)
                _log(
                    rank,
                    f"  step {step+1:2d}/{n_steps}  "
                    f"out[0]={out_buf[0].item():.1f}  "
                    f"agg_bw={bw:.2f} GB/s",
                )
        _log(rank, "PHASE3 COMPLETE -- no wedge")

    else:
        _log(rank, f"unknown SYNTH_PHASE={phase}. use 1 or 3.")
        return 1

    dist.barrier()
    dist.destroy_process_group()
    return 0


if __name__ == "__main__":
    sys.exit(main())
