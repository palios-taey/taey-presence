#!/usr/bin/env python3
"""Extract AUTONOMOUS-USAGE receipts for index capabilities from Taey's tool audit trail.

Spec §2.2 (F6, rev2 R3). A `usage` receipt is evidence that TAEY ITSELF invoked a
capability — not that the capability is up. G3 liveness can never substitute: a CONNECT box
closes only on usage.

To be independently re-checkable, each receipt cites {audit_trail_ref (path + line),
event_id, timestamp, actor, invocation excerpt} so a verifier can re-query the cited entry
and match it. A receipt whose trail entry cannot be re-fetched and matched is INVALID.

This reads the trail; it never writes to it and never fabricates an entry. If Taey has not
used a capability, that capability gets NO receipt and the absence is reported — an unused
capability is honestly unconnected, and inventing a receipt for it would defeat the only
thing the CONNECT bar measures.

Usage:  TAEY_TOOL_AUDIT=<path> python3 usage_receipts.py [--write]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
INDEX = HERE / "index.json"

# How to recognise an invocation of each capability in a recorded command. Deliberately
# matched on the ENV-VAR-independent observable (the port/binary Taey actually typed),
# because that is what the trail contains.
MATCHERS = {
    "presence-proxy":     r"\b8766\b",
    "presence-dashboard": r"\b5001\b",
    "presence-serve":     r":8000/v1/|/v1/models",
    "presence-seat":      r"taey_council_seat|taey_seat|seat_liveness",
}


def load_trail(path: Path) -> list[dict]:
    rows = []
    with path.open() as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            d["_line"] = i
            rows.append(d)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="write receipts to the paths the index declares")
    ap.add_argument("--actor", default="taey",
                    help="seat_id that counts as Taey itself (default: taey)")
    args = ap.parse_args()

    trail_path = os.environ.get("TAEY_TOOL_AUDIT")
    if not trail_path:
        print("FAIL: TAEY_TOOL_AUDIT is unset — refusing to guess the audit trail location",
              file=sys.stderr)
        return 1
    trail = Path(trail_path)
    if not trail.exists():
        print(f"FAIL: audit trail not found at {trail}", file=sys.stderr)
        return 1

    rows = load_trail(trail)
    invocations = [d for d in rows
                   if d.get("seat_id") == args.actor and d.get("tool") == "run_command"]

    doc = json.loads(INDEX.read_text())
    caps = [c for sec in doc["sections"].values() for c in sec["capabilities"]]

    written, absent = [], []
    for cap in caps:
        pat = MATCHERS.get(cap["id"])
        hits = [d for d in invocations
                if pat and re.search(pat, d.get("command", ""))] if pat else []
        if not hits:
            absent.append(cap["id"])
            print(f"  NO USAGE   {cap['id']}: no autonomous invocation by '{args.actor}' in the trail")
            continue
        last = hits[-1]
        receipt = {
            "capability": cap["id"],
            "kind": "usage",
            "audit_trail_ref": f"{trail}:{last['_line']}",
            "event_id": last.get("event_id"),
            "turn_id": last.get("turn_id"),
            "timestamp": last.get("ts"),
            "actor": last.get("seat_id"),
            "invocation": last.get("command", "")[:300],
            "rc": last.get("rc"),
            "invocation_count": len(hits),
            "verify": (f"sed -n '{last['_line']}p' {trail} | python3 -m json.tool  "
                       f"# must show event_id {last.get('event_id')}"),
        }
        print(f"  usage      {cap['id']}: {len(hits)} invocation(s), last {last.get('ts')}")
        if args.write:
            dest = REPO / cap["receipts"]["usage"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        written.append(cap["id"])

    print()
    print(f"  usage receipts: {len(written)}  |  capabilities with NO autonomous usage: {len(absent)}")
    if absent:
        print(f"  NOT CONNECTED (no usage receipt): {', '.join(absent)}")
        print("  A capability with no usage receipt is honestly unconnected. G3 liveness")
        print("  cannot close that gap — only Taey using it can.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
