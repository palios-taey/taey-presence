#!/usr/bin/env python3
"""bake_lora.py — merge a LoRA adapter into safetensors shards, and REFUSE to claim success
unless the merge demonstrably happened.

    W_new = W_base + (alpha / r) * B @ A

WHY MERGE INSTEAD OF SERVING THE ADAPTER. vLLM can serve a LoRA adapter dynamically, but only for
the module types its LoRA path implements. On a HYBRID-ATTENTION model that is a real limit:

    ValueError: base_model.model.model.layers.0.linear_attn.in_proj_qkv.lora_A.default.weight
    is unsupported LoRA weight

Observed 2026-07-27 serving module-4 against cpt_refresh_v3_servable. The adapter was fine — 704
tensors, all lora_B non-zero — but 192 of them target `linear_attn` (in_proj_qkv / out_proj) across
48 linear-attention layers, against 128 targeting `self_attn` across 16 full-attention layers.
vLLM has no kernel for the former. MERGED WEIGHTS ARE JUST WEIGHTS, so merging sidesteps the
restriction entirely and needs no retrain: the output is a structurally identical base model that
vLLM loads exactly as it loads the unmodified one.

WHAT THIS ADDS OVER THE HAND-COPIED VERSIONS. The script this derives from ended with an
unconditional `print("BAKE COMPLETE")` — it announced success after writing, having checked
nothing. That is the same defect a production audit found in BAKE_TO_HF, and it is how an
artifact missing 348 tensors once shipped. The failure mode that matters here is not a crash; it
is a merge that resolves ZERO targets, writes a perfect copy of the base, and reports completion.
Nothing is wrong in the output except that nothing happened to it.

So this refuses on:
  * any adapter target that does not resolve to a real base key (mapping drift)
  * any resolved target lacking a complete A+B pair
  * an applied count that does not equal the number of resolved pairs
  * zero applications
and prints COMPLETE only after all of them pass. Exit 1 means DO NOT SERVE THE OUTPUT.

Even then, run the acceptance gate on the result before shipping it — it checks the properties
this script cannot see (tensor count and key set versus a reference, architecture identity, and
that sampled weights actually differ from the reference):

    python3 serving/verify_servable_artifact.py --candidate $OUTPUT_PATH --reference <served model>

Env: BASE_MODEL, LORA_PATH, OUTPUT_PATH (all required — no operator-path defaults, so a missing
value fails loudly instead of silently baking into somebody's home directory).

Run it inside the pinned serving image; a Jetson host python has neither torch nor safetensors:
    docker run --rm --runtime nvidia --ipc=host \
      -v /path/serve-models:/models -v $PWD/serving/bake_lora.py:/bake.py \
      -e BASE_MODEL=/models/<base> -e LORA_PATH=/models/<adapter> -e OUTPUT_PATH=/models/<out> \
      <pinned-digest> python3 /bake.py
Stop the vLLM serve first. It holds ~92% of unified memory and the merge will OOM under it.
"""
import glob
import json
import os
import shutil
import sys

import torch
from safetensors.torch import load_file, save_file


def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"FATAL: {name} is required. Refusing to guess a path.")
    return v


BASE = env("BASE_MODEL")
LORA = env("LORA_PATH")
OUT = env("OUTPUT_PATH")


def get_base_key(lora_key):
    """Adapter key -> base weight key.

    Adapter: base_model.model.model.layers.N.self_attn.q_proj.lora_A.default.weight
    Base:    model.language_model.layers.N.self_attn.q_proj.weight

    The explicit target list covers BOTH attention families on a hybrid model. `linear_attn`
    entries are the ones vLLM will not serve as an adapter and merging exists to absorb.
    """
    targets = (
        ".self_attn.q_proj.",
        ".linear_attn.in_proj_qkv.",
        ".linear_attn.in_proj_z.",
        ".linear_attn.out_proj.",
    )
    if any(t in lora_key for t in targets):
        k = lora_key.replace("base_model.model.model.", "model.language_model.", 1)
    else:
        k = lora_key.replace("base_model.model.model.", "model.language_model.")
    for a, b in (
        (".lora_A.default.weight", ".weight"),
        (".lora_B.default.weight", ".weight"),
        (".lora_A.weight", ".weight"),
        (".lora_B.weight", ".weight"),
    ):
        k = k.replace(a, b)
    return k


def main():
    cfg = json.load(open(os.path.join(LORA, "adapter_config.json")))
    lora = load_file(os.path.join(LORA, "adapter_model.safetensors"), device="cpu")
    r, alpha = cfg["r"], cfg["lora_alpha"]
    scale = alpha / r
    # Echo the INPUTS. Without this the run is not self-documenting: a merged artifact carries no
    # record of which base it was built on, and at bf16 the per-element deltas can sit at or below
    # representable resolution, so reconstructing the base numerically afterwards does NOT reliably
    # discriminate — both candidate reconstructions land inside rounding noise. Observed 2026-07-27
    # trying to confirm a merge base after the fact. The log is the provenance; write it down.
    print(f"[bake] BASE_MODEL  = {BASE}")
    print(f"[bake] LORA_PATH   = {LORA}")
    print(f"[bake] OUTPUT_PATH = {OUT}")
    print(f"[bake] adapter records base_model_name_or_path = {cfg.get('base_model_name_or_path')}")
    print(f"[bake] LoRA r={r} alpha={alpha} scale={scale}  tensors={len(lora)}")

    modules = {}
    for k, v in lora.items():
        bk = get_base_key(k)
        if ".lora_A." in k:
            modules.setdefault(bk, {})["A"] = v
        elif ".lora_B." in k:
            modules.setdefault(bk, {})["B"] = v

    index = json.load(open(os.path.join(BASE, "model.safetensors.index.json")))["weight_map"]
    base_keys = set(index)

    # PRE-FLIGHT. Catch a mapping that resolves to nothing BEFORE writing 50GB, not after.
    unresolved = sorted(k for k in modules if k not in base_keys)
    incomplete = sorted(k for k in modules if modules[k].keys() != {"A", "B"})
    if unresolved:
        print(f"[bake] {len(unresolved)} adapter target(s) do not resolve to a base key:", file=sys.stderr)
        for k in unresolved[:5]:
            print(f"         {k}", file=sys.stderr)
        sys.exit("FATAL: key mapping does not match this base. Refusing to bake a no-op.")
    if incomplete:
        sys.exit(f"FATAL: {len(incomplete)} target(s) lack a complete A+B pair, e.g. {incomplete[0]}")
    expected = len(modules)
    print(f"[bake] {expected} target matrices, all resolved to real base keys, all A+B complete")

    os.makedirs(OUT, exist_ok=True)
    applied = 0
    for shard in sorted(set(index.values())):
        weights = load_file(os.path.join(BASE, shard), device="cpu")
        hit = 0
        for wkey in list(weights):
            if wkey in modules:
                t = weights[wkey]
                A = modules[wkey]["A"].to(torch.float32)
                B = modules[wkey]["B"].to(torch.float32)
                delta = (scale * (B @ A)).to(t.dtype)
                if delta.shape != t.shape:
                    sys.exit(f"FATAL: shape mismatch {wkey}: base {tuple(t.shape)} vs delta {tuple(delta.shape)}")
                weights[wkey] = t + delta
                applied += 1
                hit += 1
        save_file(weights, os.path.join(OUT, shard))
        print(f"[bake] {shard}: {hit} applied")
        del weights

    for f in glob.glob(os.path.join(BASE, "*")):
        if not f.endswith(".safetensors"):
            shutil.copy2(f, os.path.join(OUT, os.path.basename(f)))

    # The completion line is EARNED, never unconditional.
    if applied != expected:
        sys.exit(f"FATAL: applied {applied} of {expected} expected matrices. Output is incomplete — do not serve it.")
    if applied == 0:
        sys.exit("FATAL: zero applications. The output is a copy of the base — do not serve it.")

    print(f"[bake] applied {applied}/{expected} matrices into {OUT}")
    print("[bake] NEXT: python3 serving/verify_servable_artifact.py "
          f"--candidate {OUT} --reference {BASE}")
    print("BAKE COMPLETE")


if __name__ == "__main__":
    main()
