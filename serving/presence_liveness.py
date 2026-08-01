#!/usr/bin/env python3
"""Liveness for the presence workers that have no HTTP surface of their own.

dcm_presence and presence-engine are background processes: nothing to curl, no port. So
their liveness is (a) the process exists and (b) it is still PRODUCING, which is the part
that matters — a hung worker is a running worker, and "the process is up" is exactly the
kind of adjacent fact that reads as health while the capability is dead.

Emits one JSON object so a jq predicate can assert on the BODY, never on an exit code.

Counting is done from the process table with a bracketed pattern AND an interpreter check:
a bare grep matches this script's own command line, and a shell running the pattern inside
an eval matches too — both observed producing phantom processes on this fleet.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

WORKERS = {"dcm-presence": "presence/dcm_presence.py",
           "presence-engine": "presence-engine/engine.py"}


def running(script: str) -> int:
    n = 0
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
            if "python" not in exe:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                if script in fh.read().decode(errors="ignore"):
                    n += 1
        except (OSError, PermissionError):
            continue
    return n


def dcm_face_fresh(max_age_s: int = 900) -> bool:
    """Is dcm_presence still EMITTING, not merely alive? Its face line is the output."""
    try:
        out = subprocess.run(
            ["journalctl", "-u", "taey-dcm-presence", "--since", "-15min",
             "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=20).stdout
        return "[DCM-FACE]" in out
    except Exception:
        return False


def main() -> int:
    result = {"ok": True, "workers": {}}
    for name, script in WORKERS.items():
        n = running(script)
        result["workers"][name] = {"processes": n, "running": n > 0}
        result["ok"] &= n > 0
    result["dcm_emitting"] = dcm_face_fresh()
    result["ok"] = bool(result["ok"] and result["dcm_emitting"])
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
