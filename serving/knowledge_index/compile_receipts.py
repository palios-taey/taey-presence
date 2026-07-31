#!/usr/bin/env python3
"""Compile v2 production receipts — one per `status: production` index entry.

TAEY_PRODUCTION_RECEIPT_SPEC §3: receipts are COMPILED, never hand-maintained. Every
field is copied from the compiled index or read from the repo, so a receipt cannot assert
anything the index does not already bind. Hand-writing one would let it claim a gate set,
an artifact, or a predicate of its own choosing — which is the whole thing the chain
exists to prevent.

THE SELF-REFERENCE IS BROKEN BY CONSTRUCTION: the receipt never stores the SHA of the
commit containing it. Its location authority is the pinned fetch (`entry.receipts.liveness`
at `entry.repo.pinned_sha`, plus blob-hash equality). What it stores is
`artifact_commit_sha` — the commit of the artifact it ATTESTS, an earlier commit, and
therefore committable by ordinary git.

Usage:  python3 compile_receipts.py [--check]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
INDEX = HERE / "index.json"
GATES_MANIFEST = "serving/gates_manifest.json"


def canonical_bytes(obj) -> bytes:
    """Same canonicalisation the index uses: sorted keys, no insignificant whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def head_commit() -> str:
    """The head THIS COMPILER read (spec v2.4 §3).

    Not the index's generated_at_commit. v2.4 dropped that equality after it was proven
    unsatisfiable: receipts must be fetchable at pinned_sha, so they are committed BEFORE
    the index build head exists, so they cannot contain that head's sha. R2 now requires
    only that this value be an ancestor of (or equal to) pinned_sha — which the compile-
    then-commit-then-build order satisfies in a single pass.
    """
    return subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=30).stdout.strip()


def build_receipt(cap: dict, compiled_at_commit: str, section: str) -> dict:
    """Every field is DERIVED from the index entry. Nothing here is authored."""
    return {
        "receipt_version": 2,
        "surface_id": cap["id"],
        "repo": cap["repo"]["name"],
        # The commit of the DEPLOYED ARTIFACT this receipt attests — never the commit
        # containing this receipt file.
        "artifact_commit_sha": cap["artifact_commit_sha"],
        "artifact_manifest_sha256": cap["artifact_manifest"]["sha256"],
        # Read at artifact_commit_sha by the checker, so the manifest must EXIST there.
        # It is in every entry's artifact_paths for exactly that reason.
        "gates_manifest_ref": GATES_MANIFEST,
        "liveness": {
            "probe_cmd": cap["liveness"]["probe_cmd"],
            "expect": cap["liveness"]["expect"],
        },
        "index_entry_ref": f"sections.{section}.capabilities[{cap['id']}]",
        # The head THIS compiler read. v2.4: NOT the index's generated_at_commit —
        # R2 requires only ancestor-or-equal to pinned_sha.
        "compiled_at_commit": compiled_at_commit,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify committed receipts match a recompile; write nothing")
    args = ap.parse_args()

    if not INDEX.exists():
        print("FAIL: index.json not built", file=sys.stderr)
        return 1
    doc = json.loads(INDEX.read_text())
    gen = head_commit()
    if not gen:
        print("FAIL: cannot read HEAD", file=sys.stderr)
        return 1

    if not (REPO_ROOT / GATES_MANIFEST).is_file():
        print(f"FAIL: gates manifest missing at {GATES_MANIFEST}", file=sys.stderr)
        return 1
    gm = json.loads((REPO_ROOT / GATES_MANIFEST).read_text())
    if not gm.get("required_contexts"):
        print("FAIL: gates manifest has an EMPTY required_contexts — an empty manifest is a "
              "REFUSE by spec, so committing one would be shipping a guaranteed failure",
              file=sys.stderr)
        return 1

    written, mismatched = [], []
    for section, sec in doc["sections"].items():
        for cap in sec["capabilities"]:
            if cap.get("status") != "production":
                continue
            dest = REPO_ROOT / cap["receipts"]["liveness"]
            if args.check:
                # Recompile against the receipt's OWN recorded compiled_at_commit, not the
                # current HEAD. HEAD moves with every later commit, so comparing against it
                # would make --check fail on receipts that are perfectly valid — the field
                # legitimately records an EARLIER head (v2.4). Everything else must still
                # derive identically.
                if not dest.is_file():
                    mismatched.append((cap["id"], "missing"))
                    continue
                try:
                    recorded = json.loads(dest.read_text()).get("compiled_at_commit") or gen
                except json.JSONDecodeError:
                    mismatched.append((cap["id"], "is not valid JSON"))
                    continue
                expected = json.dumps(build_receipt(cap, recorded, section),
                                      indent=2, sort_keys=True) + "\n"
                if dest.read_text() != expected:
                    mismatched.append((cap["id"], "does not match a recompile"))
                continue
            receipt = build_receipt(cap, gen, section)
            blob = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(blob)
            written.append((cap["id"], hashlib.sha256(blob.encode()).hexdigest()))

    if args.check:
        if mismatched:
            for sid, why in mismatched:
                print(f"FAIL: receipt for {sid} {why}", file=sys.stderr)
            return 1
        print("ok   all production receipts match a recompile")
        return 0

    for sid, sha in written:
        print(f"wrote receipt {sid}  sha256={sha[:16]}")
    print(f"{len(written)} receipt(s). Rebuild the index so receipts.liveness_sha256 fills.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
