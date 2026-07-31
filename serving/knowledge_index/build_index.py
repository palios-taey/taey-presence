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
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
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


def build() -> dict:
    if not SECTIONS_DIR.is_dir():
        raise SystemExit(f"no sections directory at {SECTIONS_DIR}")
    section_files = sorted(SECTIONS_DIR.glob("*.md"))
    if not section_files:
        raise SystemExit("no section sources found — the index cannot be empty")

    manifest, sections, present = [], {}, []
    for path in section_files:
        raw = path.read_bytes()
        text = raw.decode()
        name = path.stem
        sections[name] = {
            "capabilities": parse_capabilities(text),
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

    compiled = build()
    rendered = json.dumps(compiled, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not OUT.exists():
            print("FAIL: index.json is not committed", file=sys.stderr)
            return 1
        if OUT.read_text() != rendered:
            print("FAIL: index.json does not match a recompile of its sources.\n"
                  "      Either a section changed without rebuilding, or index.json was\n"
                  "      hand-edited. Run: python3 build_index.py", file=sys.stderr)
            return 1
        n_caps = sum(len(s["capabilities"]) for s in compiled["sections"].values())
        n_procs = sum(len(s["processes"]) for s in compiled["sections"].values())
        print(f"ok   index.json matches its sources "
              f"({len(compiled['sections_present'])} section(s), {n_caps} capabilities, {n_procs} processes)")
        return 0

    OUT.write_text(rendered)
    print(f"wrote {OUT.relative_to(HERE.parent.parent)}  "
          f"sections_present={compiled['sections_present']}  "
          f"pending={[p['section'] for p in compiled['sections_pending']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
