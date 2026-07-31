#!/usr/bin/env python3
"""Report whether Taey's seats are alive, and which liveness namespace each writes.

This is `presence-seat`'s validate.cmd (knowledge index §2.2), so G3 runs it and it must
be deterministic and machine-checkable. Exit 0 = at least one seat is running and every
running seat's liveness namespace is accounted for.

WHY THE NAMESPACE MATTERS, and why this checks it rather than just counting processes:
each seat writes `taey:<TAEY_SESSION_NAME>:{idle,turns_open,...}`. Two seats sharing a
name write each other's liveness keys, so one can declare the other idle in the middle of
a turn. A seat count alone cannot see that — it looks identical either way.

Counting is done from the process table with a bracketed pattern: a bare `pgrep -f` (or an
unbracketed grep) matches its OWN command line and reports a phantom seat.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

SEAT_SCRIPTS = ("taey_council_seat.py", "taey_seat.py")


def running_seats() -> list[str]:
    try:
        out = subprocess.run(["ps", "-eo", "args="], capture_output=True, text=True,
                             timeout=15).stdout
    except Exception as exc:  # pragma: no cover - ps is always present in practice
        print(json.dumps({"ok": False, "error": f"cannot read process table: {exc}"}))
        raise SystemExit(2)
    seats = []
    for line in out.splitlines():
        if any(s in line for s in SEAT_SCRIPTS) and "seat_liveness" not in line:
            seats.append(line.strip())
    return seats


def main() -> int:
    seats = running_seats()
    namespace = os.environ.get("TAEY_SESSION_NAME", "")
    result = {
        "ok": bool(seats),
        "seat_count": len(seats),
        "liveness_namespace": f"taey:{namespace}:*" if namespace else None,
        "namespace_declared": bool(namespace),
    }
    if not seats:
        result["error"] = "no seat process found"
    if not namespace:
        # Not fatal for the count, but it means this host cannot say which liveness keys
        # its seats own — report it rather than implying the check covered it.
        result["warning"] = ("TAEY_SESSION_NAME unset: seat liveness namespace is "
                             "undetermined from this environment")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
