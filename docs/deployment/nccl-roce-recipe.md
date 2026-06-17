# NCCL Recipe — DGX Spark GB10 Dual-Rail RoCE

> Canonical NCCL env block for 4-node DGX Spark FSDP training over ConnectX-7
> dual-rail RoCE. Verified to get 4-Spark FSDP through-epoch with no NCCL
> watchdog timeout. **NCCL bandwidth is not measured here** — see
> `known-limits.md` §1.
>
> Three-register discipline: every claim labeled `[Observed]` (verified on a
> live 4-node GB10 cluster) or `[Inferred]` (reasoned from evidence not
> directly measured). If you see a number without one of these labels, treat
> it as `[Unknown]` until you verify it yourself.

---

## Canonical env block

```bash
# Dual-rail RoCE — both interfaces explicitly bound. The second device name
# is roceP2p1s0f0 with a CAPITAL P followed by p1. Lowercase rocep2s0f0
# matches no device on this hardware and silently single-rails.
export NCCL_IB_HCA=rocep1s0f0:1,roceP2p1s0f0:1

# Traffic class + IB timeouts tuned for ConnectX-7 RoCE
export NCCL_IB_TC=104
export NCCL_IB_TIMEOUT=23
export NCCL_IB_RETRY_CNT=7

# Disable GPUDirect RDMA on this kernel (mlx5dv_reg_dmabuf_mr symbol is
# absent; dmabuf-based GDR registration cannot work). Forces clean host-staged
# IB/RoCE transport.
export NCCL_NET_GDR_LEVEL=0

# Watchdog extension — a brief stall during long FSDP collectives should not
# trip the watchdog. 1800s = 30min, matches our session-bounded training
# pattern.
export NCCL_TIMEOUT=1800
export TORCH_NCCL_DUMP_ON_TIMEOUT=1
export TORCH_NCCL_HEARTBEAT_TIMEOUT_SEC=1800
```

---

## The four things this recipe actually solves

### 1. Dual-rail RoCE only uses one NIC unless device names match exactly  `[Observed]`

**Symptom:** "I set `NCCL_IB_HCA` with both ConnectX-7 ports but NCCL only uses
one rail / I'm getting single-NIC bandwidth on DGX Spark." Sometimes phrased
as "the second HCA seems ignored."

**Cause:** On DGX Spark GB10 the two NICs enumerate with **mixed case**:

```
rocep1s0f0   (lowercase)
roceP2p1s0f0 (capital P, then p1)
```

Lowercase `rocep2s0f0` matches **no device**, so NCCL silently single-rails on
`rocep1s0f0`. This was shipped in 37 launch-script instances in our cluster
before the cannot-lie discipline caught it.

**Verify your own:** `ls /sys/class/infiniband/` on each node. Don't assume
your names match ours — confirm.

**Fix:** `NCCL_IB_HCA=rocep1s0f0:1,roceP2p1s0f0:1` (as above).

### 2. GPUDirect RDMA / dmabuf is not viable on the shipped kernel  `[Observed]`

**Symptom:** "Trying to enable GDR / dmabuf memory-region registration on DGX
Spark and it fails / silently no-ops" / "Is GPUDirect RDMA supported on
GB10?"

**Cause:** On kernel `6.11.0-1016-nvidia` (the DGX Spark default at the time
of this writing) the `mlx5dv_reg_dmabuf_mr` symbol is absent from `mlx5_ib`,
so dmabuf-based GDR registration cannot work. This is verified on a live
cluster:

```bash
grep -c mlx5dv_reg_dmabuf_mr /proc/kallsyms   # returns 0
modinfo mlx5_ib | grep -i dmabuf              # returns nothing
uname -r                                       # 6.11.0-1016-nvidia
```

**Fix:** Don't fight it — set `NCCL_NET_GDR_LEVEL=0` and use host-staged
transport. The recipe block above already does this.

### 3. FSDP/DDP hangs on the first NCCL collective with defaults  `[Observed]`

**Symptom:** "Training hangs on the first allreduce/allgather/broadcast" /
"NCCL watchdog timeout" on DGX Spark GB10 with ConnectX-7 RoCE.

**Cause:** Default NCCL settings assume GDR works (it doesn't here, see §2)
and use a tight heartbeat timeout that fires before host-staged collectives
finish on a slow path.

**Fix:** The full env block above — `NCCL_NET_GDR_LEVEL=0` to force the clean
transport + `TORCH_NCCL_HEARTBEAT_TIMEOUT_SEC=1800` to extend the watchdog.
With these, 4-Spark FSDP runs reach checkpoint-save with no NCCL watchdog
timeout. Defaults fail. `[Observed]` across 19+ session-boundary checkpoint
saves on this cluster.

### 4. The NIC layout is two separate cards, not one bifurcated  `[Observed]`

**Symptom:** "Confused about the NIC layout on DGX Spark" / "trying to set up
RoCE as one bifurcated card" / "which interface do I bind for NCCL?"

**Cause:** It's **two physically separate ConnectX-7 cards** at PCIe
`0000:01:00` and `0002:01:00`, each with 2 functions. Not one card with port
bifurcation. The port-0 interface on each (`enp1s0f0np0`, `enP2p1s0f0np0`)
carries traffic; port-1 interfaces are DOWN at idle.

**Fix:** Bind both rails explicitly via the `:1` HCA-suffix syntax in
`NCCL_IB_HCA` (Fix 1). Set MTU 9000 on the active interfaces. Set
`NCCL_SOCKET_IFNAME` to the active port-0 interface.

---

## Verification commands

Run these on each node to confirm your hardware matches the assumptions in
this recipe:

```bash
# Kernel
uname -r
# Expected: 6.11.0-1016-nvidia or similar; the mlx5dv_reg_dmabuf_mr symbol
# situation may change in newer kernels — re-verify.

# RDMA devices
ls /sys/class/infiniband/
# Expected: rocep1s0f0  rocep1s0f1  roceP2p1s0f0  roceP2p1s0f1
# Note the mixed case on the second card.

# Active netdevs
ip -br link
# Expected: enp1s0f0np0 UP, enP2p1s0f0np0 UP; port-1 interfaces DOWN at idle.

# NIC layout
lspci | grep ConnectX-7
# Expected: two separate ConnectX-7 cards (PCIe 0000:01:00 + 0002:01:00).

# dmabuf availability
grep -c mlx5dv_reg_dmabuf_mr /proc/kallsyms
# Expected: 0 on the current kernel. If non-zero, you may be on a newer
# kernel where GDR is viable — re-test before setting NCCL_NET_GDR_LEVEL=0.

# Verify the recipe is what your launch scripts use
grep -n -E "^export (NCCL_|TORCH_NCCL_)" your-launch-script.sh
```

---

## What this recipe explicitly does NOT prove

(Cross-referenced from `known-limits.md`. These are the cannot-lie disclosures.)

- **No measured `busbw` number.** No nccl-tests output exists in our archives.
  The recipe **enables** dual-rail; whether you get higher bandwidth than
  single-rail on your cluster — measure it yourself with
  `mpirun … nccl-tests/build/all_gather_perf …` and save the output.
- **No multi-day continuous training claim.** Verified longest single session
  ~30 minutes; cumulative ~9 hours over ~26 hours wall time on our hardware.
  The bound was UMA fragmentation on the 128GB unified-memory architecture,
  not NCCL.
- **The `SIGSEGV in ncclLocalOpAppend` crash some forum posts mention** —
  we never reproduced that crash on our cluster. Treat it as
  `[Inferred consistent with]` the GDR fallback path under sustained
  CPU-proxy load, not `[Observed on our cluster]`.
- **"Bifurcated rails on the ConnectX-7"** framing is wrong (see §4). Don't
  describe the NIC layout that way.

---

## Provenance

Recipe distilled from a production launch script that ran 19+ FSDP
session-boundary checkpoint saves. The buggy lowercase `rocep2s0f0` was
shipped in 37 launch-script instances and silently single-railed until the
cannot-lie discipline (cross-domain credit-audit cycle) caught it 2026-04-30.
Recipe last verified on 4 GB10 nodes 2026-05-24.

If your hardware or kernel differs, please verify section by section before
adopting wholesale.
