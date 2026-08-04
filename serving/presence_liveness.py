#!/usr/bin/env python3
"""Liveness for the presence workers that have no HTTP surface of their own.

dcm_presence and presence-engine are background processes: nothing to curl, no port. So
their liveness is (a) the process exists and (b) its event loop still emits a Redis
heartbeat. Demand-driven face/prediction output cannot be a liveness signal because an
idle user correctly produces no output.

Emits one JSON object so a jq predicate can assert on the BODY, never on an exit code.

Counting is done from the process table with a bracketed pattern AND an interpreter check:
a bare grep matches this script's own command line, and a shell running the pattern inside
an eval matches too — both observed producing phantom processes on this fleet.
"""
from __future__ import annotations

import json
import os
import sys
import time

import redis

WORKERS = {"dcm-presence": "presence/dcm_presence.py",
           "presence-engine": "presence-engine/engine.py"}
HEARTBEATS = {
    "dcm-presence": "taey:presence:heartbeat:dcm-presence",
    "presence-engine": "taey:presence:heartbeat:presence-engine",
}
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))


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


def heartbeat_status(key: str, max_age_s: int = 15) -> dict:
    try:
        client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        emitted_at = float(client.get(key))
        age_s = time.time() - emitted_at
        return {
            "heartbeat_age_s": round(age_s, 3),
            "heartbeat_fresh": 0 <= age_s <= max_age_s,
        }
    except (redis.RedisError, TypeError, ValueError):
        return {"heartbeat_age_s": None, "heartbeat_fresh": False}


def main() -> int:
    result = {"ok": True, "workers": {}}
    for name, script in WORKERS.items():
        n = running(script)
        status = {
            "processes": n,
            "running": n > 0,
            **heartbeat_status(HEARTBEATS[name]),
        }
        result["workers"][name] = status
        result["ok"] &= status["running"] and status["heartbeat_fresh"]
    result["ok"] = bool(result["ok"])
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
