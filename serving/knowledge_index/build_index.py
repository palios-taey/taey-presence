#!/usr/bin/env python3
"""Compile the Taey knowledge index from per-section sources.

TAEY_KNOWLEDGE_INDEX_SPEC.md v1 §3: the index is COMPILED, never hand-maintained.

The compiled artifact embeds a SOURCE_MANIFEST carrying {repo, path, sha256} for every
section plus the content hash of the compiled body. CI recompiles and compares; a hand
edit to index.json changes the body hash and FAILS the build. That is the whole
enforcement mechanism — it does not rely on anyone remembering the rule.

The index is section-INCREMENTAL (§3, rev2 R4). A partial index is VALID and states its
own incompleteness in sections_present[] / sections_pending[]. Sections are added by
their owning connect box; a section homed in a private repo is never compiled.

Usage:  python3 build_index.py [--check]
        --check recompiles and diffs against the committed index.json without writing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
MANIFEST_DIR = HERE.parent / "manifests"
SECTIONS_DIR = HERE / "sections"
OUT = HERE / "index.json"

INDEX_ID = "taey-knowledge-index"
INDEX_VERSION = 1
REPO_NAME = "palios-taey/taey-presence"
# Class 1b (§5): the ONE live-refresh URL, pinned in the header. Host must be on the
# code-host allowlist, which is derived from the repos the index declares.
LIVE_URL = "https://github.com/palios-taey/taey-presence/raw/main/serving/knowledge_index/index.json"

# §7 migration table: every section the finished index will carry, and which box lands it.
ALL_SECTIONS = {
    "presence": "tp-connect",
    "conductor": "orch-connect",
    "memory": "isma-connect",
    "consult": "consult-connect",
    "train-how": "train-connect",
    "careers": "careers-connect",
    "identity": "corpus-classification",
}

PROCESS_FIELDS = ("PROCESS", "PLAN", "LAUNCH", "EXPECT", "ON FAIL", "NEVER")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical_bytes(obj) -> bytes:
    """Canonical JSON per receipt spec §2: sorted keys, no insignificant whitespace, UTF-8.

    The hash is taken over THESE bytes. Any other serialisation of the same object hashes
    differently, so the canonicalisation is part of the contract rather than a detail.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                          capture_output=True, text=True, timeout=30).stdout.strip()


def head_commit() -> str:
    """The SOURCE commit the build reads.

    Receipt spec: `generated_at_commit` is a PARENT of the commit that will contain the
    built index — never that commit itself. HEAD at build time is exactly that: the commit
    the sources were read at, which the build's own commit will descend from. The
    self-reference is broken by construction, not by convention.
    """
    return git("rev-parse", "HEAD")


def last_commit_touching(paths: list) -> str:
    """The commit of the DEPLOYED ARTIFACT, not the commit carrying the index.

    Same self-reference discipline: an entry attests an artifact that already exists at an
    earlier commit, so this is committable by normal git.
    """
    if not paths:
        return ""
    return git("log", "-1", "--format=%H", "--", *paths)


def parse_capabilities(text: str) -> list[dict]:
    """Every ```json fenced block in the section is one capability entry."""
    out = []
    for block in re.findall(r"```json\s*\n(.*?)\n```", text, re.S):
        try:
            out.append(json.loads(block))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"capability block is not valid JSON: {exc}\n{block[:200]}")
    return out


def parse_processes(text: str) -> list[dict]:
    """Compile the §2.1 authoring format into the structured form G1 validates.

    No prose reaches the compiled index unvalidated (§3): the text format exists for
    humans to author, and this turns it into records.
    """
    out = []
    # A process block starts at PROCESS: and runs to the next PROCESS: or end.
    for chunk in re.split(r"\n(?=PROCESS:)", text):
        if not chunk.strip().startswith("PROCESS:"):
            continue
        rec: dict[str, object] = {}
        current = None
        buf: dict[str, list[str]] = {}
        for line in chunk.splitlines():
            m = re.match(r"^(PROCESS|PLAN|LAUNCH|EXPECT|ON FAIL|NEVER):\s*(.*)$", line)
            if m:
                current = m.group(1)
                buf.setdefault(current, []).append(m.group(2).strip())
            elif current and line.strip():
                buf[current].append(line.strip())
        missing = [f for f in PROCESS_FIELDS if f not in buf]
        if missing:
            raise SystemExit(f"process block missing required field(s) {missing}:\n{chunk[:200]}")
        rec["process"] = " ".join(buf["PROCESS"]).strip()
        rec["plan_ref"] = " ".join(buf["PLAN"]).strip()
        rec["launch"] = [x for x in buf["LAUNCH"] if x]
        rec["expect"] = " ".join(buf["EXPECT"]).strip()
        rec["on_fail"] = " ".join(buf["ON FAIL"]).strip()
        rec["never"] = [x for x in buf["NEVER"] if x]
        out.append(rec)
    return out


def _conforming_receipt_sha(rel: str | None):
    """sha256 of the receipt blob — but ONLY if it is a conforming v2 receipt.

    The receipt spec declares old-format liveness receipts (stdout_excerpt/rc) NON-CONFORMING
    by definition; they are recompiled at rollout step 4. Hashing one now would bind the
    index to a receipt that can never pass its own check — a binding that looks complete and
    is guaranteed wrong. Null says "not yet compiled", which is true and is distinguishable
    from a mismatch.
    """
    if not rel:
        return None
    f = REPO_ROOT / rel
    if not f.is_file():
        return None
    raw = f.read_bytes()
    try:
        if json.loads(raw).get("receipt_version") != 2:
            return None
    except json.JSONDecodeError:
        return None
    return sha256_bytes(raw)


ORACLE_FILE = REPO_ROOT / "serving" / "validate_presence.sh"


def load_liveness_oracle() -> dict:
    """Read the liveness assertions from their SINGLE AUTHORED SOURCE — the validate suite.

    The index does not author predicates. It compiles the assertions the suite already
    makes, so the thing that runs them and the thing that binds them cannot disagree.
    """
    if not ORACLE_FILE.is_file():
        raise SystemExit(f"liveness oracle missing: {ORACLE_FILE}")
    txt = ORACLE_FILE.read_text()
    m = re.search(r"<<'ORACLE'[^\n]*\n(.*?)\nORACLE\b", txt, re.S)
    if not m:
        raise SystemExit("no ORACLE heredoc found in the validate suite")
    out = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            raise SystemExit(f"oracle line is not 4 TAB-separated fields: {line[:80]}")
        sid, lang, probe, pred = parts
        out[sid] = {"probe_cmd": probe, "expect": {"lang": lang, "predicate": pred}}
    return out


def enrich_capability(cap: dict, src_commit: str, manifest_dir: Path,
                      oracle: dict | None = None) -> None:
    """Fill the receipt-spec binding fields that are DERIVED, never authored.

    Every one of these is read from git or computed from file bytes. A hand-written value
    here would be a claim about a commit instead of a reading of one — and the receipt
    chain's whole guarantee is that each hash is computed from the content it attests.
    """
    cap.setdefault("repo", {})["pinned_sha"] = src_commit

    # Liveness is COMPILED from the suite, never authored in the section.
    if oracle is not None:
        if cap["id"] not in oracle:
            raise SystemExit(f"{cap['id']}: no liveness assertion in the oracle "
                             f"(serving/validate_presence.sh) — a production capability "
                             f"cannot bind a predicate nobody authored")
        cap["liveness"] = oracle[cap["id"]]

    paths = cap.get("artifact_paths") or []
    cap["artifact_commit_sha"] = last_commit_touching(paths)

    # Canonical-JSON manifest of the entry's deployed artifacts: {path, sha256} each.
    entries = []
    for rel in sorted(paths):
        f = REPO_ROOT / rel
        if not f.is_file():
            raise SystemExit(f"{cap['id']}: artifact_paths names a missing file: {rel}")
        entries.append({"path": rel, "sha256": sha256_bytes(f.read_bytes())})
    blob = canonical_bytes({"surface_id": cap["id"], "artifacts": entries})
    mf = manifest_dir / f"{cap['id']}.artifacts.json"
    mf.write_bytes(blob)
    cap["artifact_manifest"] = {
        # Always the COMMITTED location, even when building into a temp dir for --check.
        "path": str((MANIFEST_DIR / mf.name).relative_to(REPO_ROOT)),
        "sha256": sha256_bytes(blob),
    }

    # The receipt blob's own hash. Rollout step 2 lands the FIELD; step 4 compiles the
    # receipts. Null means "not yet compiled" and is distinguishable from a wrong hash —
    # an empty string would silently equal a missing file's digest in a sloppy comparison.
    rc = cap.setdefault("receipts", {})
    rc["liveness_sha256"] = _conforming_receipt_sha(rc.get("liveness"))


def build(*, src_commit: str | None = None, manifest_dir: Path | None = None) -> dict:
    if not SECTIONS_DIR.is_dir():
        raise SystemExit(f"no sections directory at {SECTIONS_DIR}")
    section_files = sorted(SECTIONS_DIR.glob("*.md"))
    if not section_files:
        raise SystemExit("no section sources found — the index cannot be empty")

    # In CHECK mode the caller pins this to the commit the committed index RECORDED.
    # Re-deriving it from HEAD is the self-reference paradox codex found: once the
    # index-containing commit exists, HEAD *is* that commit, so the honest parent value
    # would fail the check and the forbidden self-referential value would pass it. The
    # checker must never learn the commit from the working tree.
    src_commit = src_commit or head_commit()
    oracle = load_liveness_oracle()
    mdir = manifest_dir or MANIFEST_DIR
    mdir.mkdir(parents=True, exist_ok=True)

    manifest, sections, present = [], {}, []
    for path in section_files:
        raw = path.read_bytes()
        text = raw.decode()
        name = path.stem
        caps = parse_capabilities(text)
        for cap in caps:
            enrich_capability(cap, src_commit, mdir, oracle)
        sections[name] = {
            "capabilities": caps,
            "processes": parse_processes(text),
        }
        present.append(name)
        manifest.append({
            "section": name,
            "repo": REPO_NAME,
            "path": str(path.relative_to(HERE.parent.parent)),
            "sha256": sha256_bytes(raw),
        })

    pending = [
        {"section": s, "lands_at": box}
        for s, box in sorted(ALL_SECTIONS.items())
        if s not in present
    ]

    body = {
        "index_id": INDEX_ID,
        "version": INDEX_VERSION,
        # The SOURCE commit this build read — a parent of the commit that will contain
        # this file, never that commit itself (receipt spec §3 self-reference audit).
        "generated_at_commit": src_commit,
        "live_url": LIVE_URL,
        "code_host_allowlist": sorted({
            entry["repo"]["public_url"].split("/")[2]
            for sec in sections.values()
            for entry in sec["capabilities"]
        } | {LIVE_URL.split("/")[2]}),
        "sections_present": sorted(present),
        "sections_pending": pending,
        "sections": sections,
    }
    # The body hash covers everything except the manifest that carries it.
    body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return {**body, "source_manifest": {
        "sections": manifest,
        "compiled_body_sha256": sha256_bytes(body_bytes),
    }}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="recompile and compare against the committed index.json; write nothing")
    args = ap.parse_args()

    # DO NOT build before branching. An unconditional build() here writes manifests into
    # the REPO, which silently repaired the very hand-edit --check exists to detect — the
    # read-only check block below never saw the forgery because this line had already
    # overwritten it. Check mode must reach its own pinned, temp-dir build untouched.
    if args.check:
        if not OUT.exists():
            print("FAIL: index.json is not committed", file=sys.stderr)
            return 1
        committed = json.loads(OUT.read_text())

        # Recompile PINNED to the commit the committed index recorded — never HEAD.
        recorded = committed.get("generated_at_commit") or ""
        if not recorded:
            print("FAIL: index.json has no generated_at_commit to check against", file=sys.stderr)
            return 1

        # Build into a THROWAWAY manifest dir. Check mode must not write into the repo:
        # the previous version rebuilt manifests in place before comparing, so a
        # hand-edited manifest was silently repaired and the check passed. A checker that
        # repairs what it is meant to detect is worse than no checker.
        with tempfile.TemporaryDirectory() as td:
            expected = build(src_commit=recorded, manifest_dir=Path(td))
            rendered_expected = json.dumps(expected, indent=2, sort_keys=True) + "\n"
            if OUT.read_text() != rendered_expected:
                print("FAIL: index.json does not match a recompile of its sources at the\n"
                      f"      recorded generated_at_commit ({recorded[:12]}). Either a section\n"
                      "      changed without rebuilding, or index.json was hand-edited.",
                      file=sys.stderr)
                return 1
            # REHASH the COMMITTED manifests against the freshly computed ones.
            for cap in (c for sec in expected["sections"].values() for c in sec["capabilities"]):
                committed_mf = REPO_ROOT / cap["artifact_manifest"]["path"]
                fresh_mf = Path(td) / Path(cap["artifact_manifest"]["path"]).name
                if not committed_mf.is_file():
                    print(f"FAIL: manifest missing from the repo: {cap['artifact_manifest']['path']}",
                          file=sys.stderr)
                    return 1
                if sha256_bytes(committed_mf.read_bytes()) != sha256_bytes(fresh_mf.read_bytes()):
                    print(f"FAIL: committed manifest {cap['artifact_manifest']['path']} does not\n"
                          "      match a recompile — hand-edited, or its artifacts changed.",
                          file=sys.stderr)
                    return 1

        n_caps = sum(len(s["capabilities"]) for s in expected["sections"].values())
        n_procs = sum(len(s["processes"]) for s in expected["sections"].values())
        print(f"ok   index.json + manifests match their sources at recorded commit "
              f"{recorded[:12]} ({len(expected['sections_present'])} section(s), "
              f"{n_caps} capabilities, {n_procs} processes)")
        return 0

    compiled = build()
    rendered = json.dumps(compiled, indent=2, sort_keys=True) + "\n"
    OUT.write_text(rendered)
    print(f"wrote {OUT.relative_to(HERE.parent.parent)}  "
          f"sections_present={compiled['sections_present']}  "
          f"pending={[p['section'] for p in compiled['sections_pending']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
