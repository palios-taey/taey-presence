#!/usr/bin/env python3
"""verify_servable_artifact.py — SERVING'S ACCEPTANCE CONTRACT for a model artifact.

WHY THIS EXISTS: on 2026-07-27 a bake logged "BAKE COMPLETE" and handed over an artifact that
vLLM could not load — it was missing 348 tensors (the 333 vision + 15 MTP heads the served model
carries). The bake's success line was unconditional: it printed after save_pretrained without
checking anything. The FIRST check in the entire chain was a load attempt on a serving node,
after a 51GB transfer. That is the wrong place to discover it.

This is the gate that belongs at the HANDOFF boundary: run it on the producing machine, against
the artifact you are about to ship and the artifact it must replace. It is cheap (reads index
files and a checksum sample, never loads the model) and it fails LOUD.

It checks the four things that actually predict "vLLM will serve this":
  1. TENSOR COUNT + KEY SET match the reference, both directions. A subset is the failure that
     bit us — the artifact looked fine and was missing a whole submodule.
  2. ARCHITECTURE IDENTITY matches: model_type, architectures, presence of a nested text_config.
     A text-only save (qwen3_5_text / ForCausalLM) will not load where the wrapper is expected.
  3. CONFIG has no unexpected divergence (transformers_version alone is informational and allowed).
  4. WEIGHTS ACTUALLY CHANGED vs the reference on a sampled set — otherwise you have shipped the
     reference with extra steps and no one finds out. A "new" model identical to the old one is a
     silent no-op, which is worse than a failure because it looks like success.

Usage:
    verify_servable_artifact.py --candidate /path/to/new_hf --reference /path/to/currently_served_hf
Exit 0 = safe to ship. Exit 1 = do not transfer, do not serve.
"""
import argparse, hashlib, json, os, random, sys

def load_index(p):
    with open(os.path.join(p, "model.safetensors.index.json")) as f:
        return json.load(f)["weight_map"]

def groups(keys):
    g = {}
    for k in keys:
        parts = k.split(".")
        name = parts[1] if k.startswith("model.") and len(parts) > 1 else parts[0]
        g[name] = g.get(name, 0) + 1
    return g

def tensor_sha(path, shard, key, wm):
    # hash a bounded slice of the shard holding this tensor — enough to prove change, cheap to read
    import struct
    f = os.path.join(path, shard)
    with open(f, "rb") as fh:
        n = struct.unpack("<Q", fh.read(8))[0]
        hdr = json.loads(fh.read(n))
        if key not in hdr: return None
        s, e = hdr[key]["data_offsets"]
        fh.seek(8 + n + s)
        return hashlib.sha256(fh.read(min(e - s, 1 << 20))).hexdigest()[:16]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--sample", type=int, default=8)
    a = ap.parse_args()
    fails = []

    cwm, rwm = load_index(a.candidate), load_index(a.reference)
    ck, rk = set(cwm), set(rwm)
    print(f"  candidate tensors : {len(ck)}   groups {groups(ck)}")
    print(f"  reference tensors : {len(rk)}   groups {groups(rk)}")

    # 1. key set, BOTH directions
    missing, extra = rk - ck, ck - rk
    if missing:
        fails.append(f"MISSING {len(missing)} tensors the reference has, e.g. {sorted(missing)[:3]}")
    if extra:
        fails.append(f"{len(extra)} tensors NOT in the reference, e.g. {sorted(extra)[:3]}")
    if not missing and not extra:
        print("  key set           : MATCHES reference both directions")

    # 2 + 3. architecture identity and config divergence
    cc = json.load(open(os.path.join(a.candidate, "config.json")))
    rc = json.load(open(os.path.join(a.reference, "config.json")))
    for field in ("model_type", "architectures"):
        if cc.get(field) != rc.get(field):
            fails.append(f"{field}: candidate {cc.get(field)!r} != reference {rc.get(field)!r}")
    if ("text_config" in cc) != ("text_config" in rc):
        fails.append(f"text_config presence differs (candidate={'text_config' in cc})")
    ALLOWED = {"transformers_version"}
    div = [k for k in set(cc) | set(rc)
           if k not in ALLOWED and cc.get(k, "<absent>") != rc.get(k, "<absent>")]
    if div:
        fails.append(f"config fields differ beyond the allowed set: {div[:6]}")
    else:
        print("  config            : matches (transformers_version divergence allowed)")

    # 4. weights actually changed — the silent no-op check
    common = sorted(ck & rk)
    random.seed(0)
    picks = random.sample(common, min(a.sample, len(common)))
    changed = same = 0
    for k in picks:
        h1, h2 = tensor_sha(a.candidate, cwm[k], k, cwm), tensor_sha(a.reference, rwm[k], k, rwm)
        if h1 is None or h2 is None: continue
        if h1 == h2: same += 1
        else: changed += 1
    print(f"  weight sample     : {changed} changed / {same} identical (of {changed+same})")
    if changed == 0 and same > 0:
        fails.append("NO sampled tensor differs from the reference — this is the reference with "
                     "extra steps, a silent no-op, not a new model")

    if fails:
        print("\n  ARTIFACT REJECTED — do not transfer, do not serve:")
        for f in fails: print(f"    - {f}")
        return 1
    print("\n  ARTIFACT ACCEPTED — structurally servable and genuinely different from the reference.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
