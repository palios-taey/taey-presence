#!/usr/bin/env python3
"""Mechanical gates for the Taey knowledge index (SPEC v1 §5).

G1  schema-lint          every compiled entry validates against §2, rigidly.
G2  pointer-crawler      CLOSED-WORLD: walks EVERY string field of EVERY entry. Each
                         field belongs to exactly one permitted string class; a string
                         that does not match its field's class fails the build.
G3  capability liveness  executes every production entry's validate.cmd against the live
                         deployment. Produces `liveness` receipts ONLY — G3 can never
                         produce a `usage` receipt and never marks a capability CONNECTED.

G2 is closed-world on purpose. A gate that walks an enumerated subset of "the pointer
fields" passes anything added later in a field nobody listed — fail-open is precisely the
defect class this exists to kill. So the traversal is schema-driven: it visits every
string reachable in the document and decides a class for it from its path.

Usage:  python3 gates.py [--g1] [--g2] [--g3]   (default: G1 + G2)
        G3 is opt-in because it touches the live deployment.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.json"

# A private/operator-local reference is a disconnection: a downloaded Taey following it
# finds nothing. Names are matched as path segments, not substrings.
# palios-training is public train-how; it remains sections_pending until train-connect
# lands receipt-backed production entries, but it is not a private disconnection.
PRIVATE_REPO_SEGMENTS = {
    "the-conductor", "treasurer", "apply-machine",
    "infra-soul", "linkedin", "isma",
}

URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://", re.I)
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# host:port literal, e.g. mira:8766 or example.com:443 — but not $VAR:port
HOSTPORT_RE = re.compile(r"(?<![\w$/.])(?!localhost\b)[a-z][a-z0-9.-]*\.[a-z]{2,}:\d{2,5}\b", re.I)
ENVREF_RE = re.compile(r"\$\{?[A-Z_][A-Z0-9_]*\}?")


class Failure(list):
    def add(self, path: str, value: str, why: str) -> None:
        self.append({"field": path, "value": value[:160], "why": why})


def walk_strings(node, path="$"):
    """Yield (json_path, string) for every string reachable in the document."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk_strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def classify(field_path: str) -> str:
    """Every field belongs to exactly ONE class. Unknown fields fall to class 3, the
    strictest — an added field is constrained by default rather than unconstrained."""
    if field_path.endswith(".public_url"):
        return "1"
    if field_path == "$.live_url":
        return "1b"
    if re.search(r"\.endpoints\[\d+\]\.(health|env|name)$", field_path):
        return "2"
    return "3"


def has_ip_literal(s: str) -> bool:
    for m in IPV4_RE.findall(s):
        try:
            ipaddress.ip_address(m)
            return True
        except ValueError:
            continue
    return False


def g2_pointer_crawl(doc: dict) -> Failure:
    f = Failure()
    allow = set(doc.get("code_host_allowlist") or [])
    if not allow:
        f.add("$.code_host_allowlist", "", "empty allowlist — class 1 cannot be checked")

    seen_live = 0
    for path, s in walk_strings(doc):
        cls = classify(path)

        if cls == "1":
            if not s.startswith("https://"):
                f.add(path, s, "class 1 must be an absolute https URL")
            elif s.split("/")[2] not in allow:
                f.add(path, s, f"host not on code-host allowlist {sorted(allow)}")

        elif cls == "1b":
            seen_live += 1
            if not s.startswith("https://"):
                f.add(path, s, "class 1b must be an absolute https URL")
            elif s.split("/")[2] not in allow:
                f.add(path, s, f"live_url host not on code-host allowlist {sorted(allow)}")

        elif cls == "2":
            if URL_RE.search(s) or has_ip_literal(s) or HOSTPORT_RE.search(s):
                f.add(path, s, "class 2 (endpoint) may not carry a URL, host or IP literal — "
                               "relative path or env-var composition only")

        else:  # class 3 — the strictest, and the default for anything unrecognised
            if URL_RE.search(s):
                f.add(path, s, "class 3 may not contain a URL")
            if has_ip_literal(s):
                f.add(path, s, "class 3 may not contain an IP literal")
            if HOSTPORT_RE.search(s):
                f.add(path, s, "class 3 may not contain a host:port literal")
            # absolute paths outside the resolving repo are operator-local by definition
            for tok in re.findall(r"(?<![\w$])/[A-Za-z0-9_./-]{3,}", s):
                if tok.startswith("/v1/") or tok.startswith("/api/"):
                    continue  # URL paths on an env-composed base are fine
                f.add(path, s, f"class 3 may not contain an absolute path ({tok})")
            for seg in re.split(r"[/\s:,'\"]+", s):
                if seg in PRIVATE_REPO_SEGMENTS:
                    f.add(path, s, f"references private/operator-local repo '{seg}' — "
                                   "a downloaded Taey following this finds nothing")

    if seen_live != 1:
        f.add("$.live_url", str(seen_live), "exactly one class 1b field must exist")

    # zero private_slot VALUES anywhere (§2.4)
    for path, s in walk_strings(doc):
        if ".private_slot" in path and path.endswith(".value"):
            f.add(path, s, "private_slot VALUES may never appear in a public artifact")

    # A pointer that passes every class rule and still resolves to nothing is the SAME
    # disconnection this gate exists to prevent. Added 2026-07-31 after the first authored
    # section shipped two pointers to files that did not exist: the class rules were all
    # satisfied, so the gate was green on a broken index. A well-formed pointer to nowhere
    # is indistinguishable from a working one until Taey follows it.
    repo_root = HERE.parent.parent
    for path, s in walk_strings(doc):
        if not re.match(r"^\$\.sections\.[^.]+\.(capabilities|processes)", path):
            continue
        leaf = path.rsplit(".", 1)[-1].split("[")[0]
        cands = []
        if leaf in ("entry_doc", "plan_ref"):
            cands = [s]
        elif leaf in ("cmd", "launch"):
            cands = re.findall(r"(?<![\w/])(?:serving|scripts|presence|dashboard)/[A-Za-z0-9_./-]+", s)
        elif leaf in ("liveness", "usage") and ".receipts." in path:
            cands = [s]
        for c in cands:
            if not (repo_root / c).exists():
                f.add(path, c, "pointer does not resolve in this repo — "
                               "a well-formed pointer to nothing is still a disconnection")
    return f


CAP_REQUIRED = ["id", "kind", "repo", "entry_doc", "bootstrap", "liveness",
                "endpoints", "hardware_tier", "receipts", "status",
                # receipt-spec binding fields (rollout step 2) — all land together,
                # because a receipt cannot sequence before the fields that bind it.
                "artifact_paths", "artifact_commit_sha", "artifact_manifest"]
VALID_LANGS = {"jq", "text"}
PROC_REQUIRED = ["process", "plan_ref", "launch", "expect", "on_fail", "never"]
VALID_KINDS = {"memory", "serve", "orchestrate", "notify", "consult", "train-how"}
VALID_STATUS = {"production", "deprecated"}


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(HERE.parent.parent), *args],
                          capture_output=True, text=True, timeout=30).stdout.strip()


def g0_commit_fields(doc: dict, pre_commit: bool = False) -> tuple:
    """The commit fields must attest an EARLIER commit than the one carrying this index.

    This is the structural half of the self-reference audit. Without it, a build that
    recorded its own containing commit would look identical to an honest one — and the
    checker cannot tell the difference by recomputation alone, because recomputing from
    HEAD is exactly the paradox: after the index is committed, HEAD IS the containing
    commit, so the forbidden value verifies and the honest value fails.
    """
    f = Failure()
    gen = doc.get("generated_at_commit") or ""
    if not gen:
        f.add("$.generated_at_commit", "", "missing")
        return f, False

    # The commit that contains the committed index file. Empty while it is uncommitted,
    # which is a legitimate pre-commit state and not a violation.
    rel = str(INDEX.relative_to(HERE.parent.parent))

    # A DIRTY index.json CANNOT BE VERIFIED, and the safe path is the DEFAULT.
    #
    # The commit that will contain an uncommitted index does not exist yet, so `git log -1`
    # returns the PREVIOUS commit and the ancestry comparison is off by one. Skipping
    # silently was the defect codex found: it printed an ordinary `ok`, indistinguishable
    # from a real verification, so a perpetually-dirty tree would green forever and nothing
    # in the merge path enforced the committed-object check.
    #
    # This is the SKIP-NEVER-COUNTS-AS-A-PASS law from our own validate suite, applied to
    # our own gate. Unverifiable is now FAIL by default; --pre-commit is the only skip
    # path, it must be asked for, and it announces itself in the summary.
    if _git("status", "--porcelain", "--", rel):
        if pre_commit:
            return f, True
        f.add("$.generated_at_commit", "(index.json is dirty)",
              "commit ancestry CANNOT BE VERIFIED against an uncommitted index — the "
              "containing commit does not exist yet. Commit the index and re-run, or pass "
              "--pre-commit to skip this gate VISIBLY. A silent skip here would let a "
              "self-referential commit field reach main unchecked.")
        return f, False

    containing = _git("log", "-1", "--format=%H", "--", rel)
    if not containing:
        f.add("$.generated_at_commit", "(index.json is not committed)",
              "no containing commit exists to verify ancestry against; commit it or pass "
              "--pre-commit")
        return f, False

    def check(field: str, val: str) -> None:
        if not val:
            f.add(field, "", "missing")
            return
        if val == containing:
            f.add(field, val, "EQUALS the commit containing this index — a field may never "
                              "attest the commit that carries it (self-reference)")
            return
        anc = subprocess.run(["git", "-C", str(HERE.parent.parent), "merge-base",
                              "--is-ancestor", val, containing], capture_output=True, timeout=30)
        if anc.returncode != 0:
            f.add(field, val, f"is not an ancestor of the index-containing commit "
                              f"{containing[:12]} — it attests a commit this index cannot descend from")

    check("$.generated_at_commit", gen)
    for name, sec in (doc.get("sections") or {}).items():
        for cap in sec.get("capabilities", []):
            check(f"sections.{name}.{cap.get('id')}.repo.pinned_sha",
                  (cap.get("repo") or {}).get("pinned_sha", ""))
            check(f"sections.{name}.{cap.get('id')}.artifact_commit_sha",
                  cap.get("artifact_commit_sha", ""))
    return f, False


def g1_schema(doc: dict) -> Failure:
    f = Failure()
    for name, sec in (doc.get("sections") or {}).items():
        for cap in sec.get("capabilities", []):
            where = f"sections.{name}.capabilities[{cap.get('id','?')}]"
            for k in CAP_REQUIRED:
                if k not in cap:
                    f.add(where, k, "missing required capability field")
            if cap.get("kind") not in VALID_KINDS:
                f.add(where, str(cap.get("kind")), f"kind must be one of {sorted(VALID_KINDS)}")
            if cap.get("status") not in VALID_STATUS:
                f.add(where, str(cap.get("status")), f"status must be one of {sorted(VALID_STATUS)}")
            for ep in cap.get("endpoints", []):
                if "env" not in ep:
                    f.add(where, str(ep), "endpoints must be env-var-named (never a hardcoded default)")
            for r in ("liveness", "usage"):
                if r not in (cap.get("receipts") or {}):
                    f.add(where, r, "capability must declare both liveness and usage receipt paths")
            if "liveness_sha256" not in (cap.get("receipts") or {}):
                f.add(where, "receipts.liveness_sha256",
                      "binding field missing (may be null until receipts compile, but the key must exist)")
            if not (cap.get("repo") or {}).get("pinned_sha"):
                f.add(where, "repo.pinned_sha", "binding field missing or empty")
            if not cap.get("artifact_commit_sha"):
                f.add(where, "artifact_commit_sha", "binding field missing or empty")
            am = cap.get("artifact_manifest") or {}
            if not am.get("path") or not am.get("sha256"):
                f.add(where, "artifact_manifest", "must carry both path and sha256")
            # Liveness must be an EXECUTABLE predicate, never prose (receipt spec §6).
            lv = cap.get("liveness") or {}
            if not lv.get("probe_cmd"):
                f.add(where, "liveness.probe_cmd", "missing")
            exp = lv.get("expect") or {}
            if exp.get("lang") not in VALID_LANGS:
                f.add(where, str(exp.get("lang")),
                      f"liveness.expect.lang must be one of {sorted(VALID_LANGS)} — "
                      "prose expectations are non-conforming by definition")
            if not exp.get("predicate"):
                f.add(where, "liveness.expect.predicate", "missing")
            else:
                pred = exp["predicate"]
                if exp.get("lang") == "jq":
                    r = subprocess.run(["jq", "-e", pred], input="{}", capture_output=True,
                                       text=True, timeout=10)
                    if r.returncode > 1:
                        f.add(where, pred, f"invalid jq predicate: {r.stderr.strip()[:90]}")
                elif exp.get("lang") == "text":
                    r = subprocess.run(["grep", "-qE", pred], input="", capture_output=True,
                                       text=True, timeout=10)
                    if r.returncode > 1:
                        f.add(where, pred, "invalid POSIX ERE")
                if re.search(r"\bstatus_code\b|\bhttp_code\b|^\s*2\d\d\s*$", pred):
                    f.add(where, pred, "predicate references a status code — the probe-shape "
                                       "law is BODIES, NOT CODES: a 200 carrying an error "
                                       "object is not liveness")
        for proc in sec.get("processes", []):
            where = f"sections.{name}.processes[{proc.get('process','?')[:40]}]"
            for k in PROC_REQUIRED:
                if k not in proc or not proc[k]:
                    f.add(where, k, "missing required process field")
    return f


def g3_liveness(doc: dict, write: bool = False) -> tuple[Failure, list[dict]]:
    """Execute every production capability's validate.cmd against the live deployment.

    Produces liveness receipts ONLY. A green G3 says the capability is up right now; it
    says NOTHING about whether Taey has ever used it (§2.2 F6).
    """
    f, receipts = Failure(), []
    for name, sec in (doc.get("sections") or {}).items():
        for cap in sec.get("capabilities", []):
            if cap.get("status") != "production":
                continue
            lv = cap.get("liveness") or {}
            cmd = lv.get("probe_cmd")
            if not cmd:
                f.add(cap.get("id", "?"), "", "production capability has no liveness.probe_cmd")
                continue
            missing = [m.strip("${}") for m in ENVREF_RE.findall(cmd)
                       if not os.environ.get(m.strip("${}"))]
            if missing:
                f.add(cap["id"], cmd, f"required env unset: {sorted(set(missing))} — "
                                      "fail loud rather than assume a default")
                continue
            # Commands are REPO-RELATIVE (§4 resolution rule), so they run from the repo
            # root — not from wherever the gate happens to be invoked. Running them from
            # the gate's own directory made a correct command look like a broken
            # capability, which is the worst kind of gate failure: it blames the thing
            # being measured for a defect in the measurement.
            # Execution contract (receipt spec §6): 30s timeout, stdin closed, stderr
            # discarded for the predicate. Non-zero exit, timeout, parse failure, or a
            # failed predicate are ALL not-live — no partial credit.
            try:
                proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                                      timeout=30, cwd=HERE.parent.parent, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                f.add(cap["id"], cmd, "probe timed out (30s) — not-live")
                continue
            ok = proc.returncode == 0
            if not ok:
                f.add(cap["id"], cmd, f"probe exited {proc.returncode}: {proc.stderr[:120]}")

            # THE PREDICATE IS THE VERDICT, not the exit status. A probe can exit 0 and
            # return an error body; that is not liveness.
            exp = lv.get("expect") or {}
            lang, pred = exp.get("lang"), exp.get("predicate")
            if ok and pred:
                if lang == "jq":
                    pr = subprocess.run(["jq", "-e", pred], input=proc.stdout,
                                        capture_output=True, text=True, timeout=10)
                    if pr.returncode != 0:
                        ok = False
                        f.add(cap["id"], pred, "probe answered but the liveness predicate "
                                               "FAILED — exit 0 is not liveness")
                elif lang == "text":
                    pr = subprocess.run(["grep", "-qE", pred], input=proc.stdout,
                                        capture_output=True, text=True, timeout=10)
                    if pr.returncode != 0:
                        ok = False
                        f.add(cap["id"], pred, "stdout did not match the anchored ERE")
            receipt = {
                "capability": cap["id"], "kind": "liveness", "ok": ok,
                "cmd": cmd, "rc": proc.returncode,
                "stdout_excerpt": proc.stdout[:200],
                "index_version": doc.get("version"),
                "note": ("G3 liveness only: this says the capability answered just now. "
                         "It says NOTHING about whether Taey has ever used it, and it can "
                         "never close a CONNECT box (spec 2.2 F6)."),
            }
            receipts.append(receipt)
            dest = HERE.parent.parent / (cap.get("receipts") or {}).get("liveness", "")
            if write and dest.name:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return f, receipts


def report(title: str, f: Failure) -> bool:
    if not f:
        print(f"  ok   {title}")
        return True
    print(f"  FAIL {title} — {len(f)} violation(s)")
    for v in f[:25]:
        print(f"         {v['field']}\n           value: {v['value']}\n           why:   {v['why']}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--g1", action="store_true")
    ap.add_argument("--g2", action="store_true")
    ap.add_argument("--g3", action="store_true")
    ap.add_argument("--pre-commit", action="store_true",
                    help="the ONLY way to skip G0 ancestry on a dirty index; the skip is "
                         "printed and the summary reports PASS-WITH-SKIP, never bare PASS")
    ap.add_argument("--write-receipts", action="store_true",
                    help="persist liveness receipts; OFF by default so measuring never "
                         "mutates the tree being measured")
    a = ap.parse_args()
    if not (a.g1 or a.g2 or a.g3):
        a.g1 = a.g2 = True

    if not INDEX.exists():
        print("FAIL: index.json not built — run build_index.py", file=sys.stderr)
        return 1
    doc = json.loads(INDEX.read_text())
    ok = True

    print("KNOWLEDGE-INDEX GATES")
    skipped = 0
    if a.g1:
        g0f, g0_skipped = g0_commit_fields(doc, pre_commit=a.pre_commit)
        if g0_skipped:
            print("  SKIP G0 commit-field ancestry: SKIPPED (pre-commit mode) — the index is "
                  "dirty, so ancestry was NOT verified")
            skipped += 1
        else:
            ok &= report("G0 commit-field ancestry (self-reference audit)", g0f)
        ok &= report("G1 schema-lint", g1_schema(doc))
    if a.g2:
        ok &= report("G2 pointer-crawler (closed-world)", g2_pointer_crawl(doc))
    if a.g3:
        f, receipts = g3_liveness(doc, write=a.write_receipts)
        ok &= report("G3 capability liveness", f)
        for r in receipts:
            print(f"         {'green' if r['ok'] else 'RED  '} {r['capability']}: {r['stdout_excerpt'][:90]}")

    if not ok:
        print("GATES: FAIL")
    elif skipped:
        print(f"GATES: PASS-WITH-SKIP ({skipped} gate(s) not run — a SKIP is not a PASS)")
    else:
        print("GATES: PASS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
