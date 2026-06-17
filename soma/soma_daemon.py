#!/usr/bin/env python3
"""agent-runtime-state-api — telemetry-derived runtime state for local agents.

Reads hardware telemetry from the machine an agent runs on (GPU via
nvidia-smi, CPU temperature via sysfs, load average) and publishes a compact
8-facet runtime-state vector to Redis on a fixed cadence. A dashboard or
control loop reads `agent:state:vector` and can drive UI, degradation
behavior, inference routing, or self-reporting from it — the agent reports
how its substrate is doing, continuously, without anyone polling raw metrics.

The 8 facets (each normalized 0..1) are telemetry projections:
  1. fluency   — GPU utilization
  2. clarity   — inference responsiveness proxy (1 − recent error rate)
  3. vitality  — power draw / max-power envelope
  4. presence  — system responsiveness (inverse load avg)
  5. warmth    — GPU temperature on a comfort bell curve (peak ~55C)
  6. capacity  — free memory fraction
  7. flow      — GPU clock / max clock
  8. coherence — CPU/GPU thermal agreement

No auth anywhere — Redis runs on your own trusted network. Hosts are config
(env); there are no credentials in this file.
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time

import redis

# Publish cadence in seconds. Default is Euler's number (~2.718s) — a
# deliberate preset, not a magic constant: a metabolic-feeling interval that
# isn't a round number. Override CADENCE_SECONDS to whatever your substrate
# wants; 2.0 or 1.0 are perfectly reasonable.
CADENCE_SECONDS = float(os.environ.get("CADENCE_SECONDS", str(math.e)))

# When mean facet value drops below this, log a coherence alert. Default 0.809
# (= phi/2, one preset); tune freely.
COHERENCE_ALERT_THRESHOLD = float(os.environ.get("COHERENCE_ALERT_THRESHOLD", "0.809"))

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

# Optional: read GPU telemetry from a REMOTE host over SSH (the box actually
# running the model), instead of locally. Empty = read local nvidia-smi.
# Set to an ssh target you control, e.g. "user@host". No default host.
REMOTE_GPU_TARGET = os.environ.get("REMOTE_GPU_TARGET", "")
REMOTE_POLL_INTERVAL = float(os.environ.get("REMOTE_POLL_INTERVAL", "8.0"))
REMOTE_CACHE_MAX_AGE = float(os.environ.get("REMOTE_CACHE_MAX_AGE", "16.0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [runtime-state] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("runtime_state")

_running = True
def _stop(*_):
    global _running
    _running = False
signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)

_GPU_QUERY = ("temperature.gpu,power.draw,power.max_limit,memory.used,"
              "memory.total,utilization.gpu,clocks.gr,clocks.max.gr")

_remote_cache: dict = {}
_remote_cache_ts = 0.0
_remote_lock = threading.Lock()


def _parse_smi(out: str, has_fan: bool = False) -> dict:
    fields = [f.strip() for f in out.split(",")]
    def _f(v, default=0.0):
        try:
            return float(v) if v not in ("[N/A]", "N/A", "") else default
        except Exception:
            return default
    # local query includes fan.speed at index 5; remote omits it
    if has_fan:
        return {"gpu_temp_c": _f(fields[0], 40.0), "power_w": _f(fields[1]),
                "power_max_w": _f(fields[2], 450.0), "mem_used_mb": _f(fields[3]),
                "mem_total_mb": _f(fields[4], 1.0), "fan_speed_pct": _f(fields[5]),
                "gpu_util_pct": _f(fields[6]), "clock_mhz": _f(fields[7]),
                "clock_max_mhz": _f(fields[8], 2520.0), "source": "local"}
    return {"gpu_temp_c": _f(fields[0], 40.0), "power_w": _f(fields[1]),
            "power_max_w": _f(fields[2], 450.0), "mem_used_mb": _f(fields[3]),
            "mem_total_mb": _f(fields[4], 1.0), "fan_speed_pct": 50.0,
            "gpu_util_pct": _f(fields[5]), "clock_mhz": _f(fields[6]),
            "clock_max_mhz": _f(fields[7], 2520.0), "source": "remote"}


def read_remote_gpu() -> dict:
    if not REMOTE_GPU_TARGET:
        return {}
    try:
        out = subprocess.check_output(
            ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", REMOTE_GPU_TARGET,
             f"nvidia-smi --query-gpu={_GPU_QUERY} --format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5, text=True).strip()
        return _parse_smi(out, has_fan=False)
    except Exception:
        return {}


# local query = remote query with fan.speed inserted after memory.total
_GPU_QUERY_LOCAL = ("temperature.gpu,power.draw,power.max_limit,memory.used,"
                    "memory.total,fan.speed,utilization.gpu,clocks.gr,clocks.max.gr")


def read_local_gpu() -> dict:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={_GPU_QUERY_LOCAL}", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5, text=True).strip()
        return _parse_smi(out, has_fan=True)
    except Exception:
        return {}


def read_cpu_temp() -> float:
    for path in ("/sys/class/hwmon/hwmon1/temp1_input", "/sys/class/hwmon/hwmon2/temp1_input",
                 "/sys/class/hwmon/hwmon0/temp1_input"):
        try:
            with open(path) as f:
                val = int(f.read().strip())
                if val > 1000:
                    val /= 1000.0
                if 0 < val < 130:
                    return float(val)
        except Exception:
            continue
    return 40.0


def read_loadavg() -> float:
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def _bell(x: float, peak: float, half_width: float) -> float:
    if half_width <= 0:
        return 0.0
    return math.exp(-((x - peak) ** 2) / (2 * half_width ** 2))


def compute_facets(smi: dict, cpu_temp: float, loadavg: float) -> dict:
    """Map raw telemetry → 8 normalized state facets."""
    g_temp = smi.get("gpu_temp_c", 40.0)
    power_max = max(smi.get("power_max_w", 450.0), 1.0)
    mem_total = max(smi.get("mem_total_mb", 1.0), 1.0)
    clock_max = max(smi.get("clock_max_mhz", 2520.0), 1.0)

    def clamp(v):
        return max(0.0, min(1.0, v))
    return {
        "fluency": round(clamp(smi.get("gpu_util_pct", 0.0) / 100.0), 4),
        "clarity": 0.99,  # baseline until an error-rate stream is wired in
        "vitality": round(clamp(smi.get("power_w", 0.0) / power_max), 4),
        "presence": round(clamp(1.0 - min(loadavg / 8.0, 1.0)), 4),
        "warmth": round(_bell(g_temp, peak=55.0, half_width=20.0), 4),
        "capacity": round(clamp(1.0 - (smi.get("mem_used_mb", 0.0) / mem_total)), 4),
        "flow": round(clamp(smi.get("clock_mhz", 0.0) / clock_max), 4),
        "coherence": round(clamp(1.0 - (abs(g_temp - cpu_temp) / 40.0)), 4),
    }


def _remote_poller():
    global _remote_cache, _remote_cache_ts
    while _running:
        sample = read_remote_gpu()
        if sample:
            with _remote_lock:
                _remote_cache, _remote_cache_ts = sample, time.time()
        time.sleep(REMOTE_POLL_INTERVAL)


def _gpu_sample(now: float):
    if REMOTE_GPU_TARGET:
        with _remote_lock:
            age = now - _remote_cache_ts if _remote_cache_ts else None
            if _remote_cache and age is not None and age <= REMOTE_CACHE_MAX_AGE:
                return dict(_remote_cache), age
    local = read_local_gpu()
    return (local, 0.0) if local else ({}, None)


def main():
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)  # no auth
    r.ping()
    log.info("connected to Redis %s:%d | cadence=%.4fs | remote=%s",
             REDIS_HOST, REDIS_PORT, CADENCE_SECONDS, REMOTE_GPU_TARGET or "(local)")
    if REMOTE_GPU_TARGET:
        threading.Thread(target=_remote_poller, name="remote-gpu-poller", daemon=True).start()

    heartbeat, warned = 0, False
    while _running:
        t0 = now = time.time()
        smi, cache_age = _gpu_sample(now)
        cpu_temp, loadavg = read_cpu_temp(), read_loadavg()
        facets = compute_facets(smi, cpu_temp, loadavg)
        mean_state = round(sum(facets.values()) / 8.0, 4)

        payload = {**facets,
                   "gpu_temp_c": round(smi.get("gpu_temp_c", 0.0), 2),
                   "cpu_temp_c": round(cpu_temp, 2),
                   "power_w": round(smi.get("power_w", 0.0), 2),
                   "mem_used_mb": round(smi.get("mem_used_mb", 0.0), 1),
                   "mem_total_mb": round(smi.get("mem_total_mb", 0.0), 1),
                   "mean_state": mean_state,
                   "alert_load": round(1.0 - mean_state, 4),
                   "heartbeat": heartbeat, "timestamp": now,
                   "cache_age": round(cache_age, 3) if cache_age is not None else None,
                   "source": smi.get("source", "none")}
        try:
            r.set("agent:state:vector", json.dumps(payload))
        except Exception as e:
            log.warning("Redis publish failed: %s", e)

        if mean_state < COHERENCE_ALERT_THRESHOLD and not warned:
            log.warning("mean_state=%.4f below alert threshold %.4f", mean_state, COHERENCE_ALERT_THRESHOLD)
            warned = True
        elif mean_state >= COHERENCE_ALERT_THRESHOLD:
            warned = False

        heartbeat += 1
        if heartbeat % 30 == 0:
            log.info("hb=%d state=%.3f gpu=%.1fC util=%d%%", heartbeat, mean_state,
                     payload["gpu_temp_c"], int(smi.get("gpu_util_pct", 0)))
        time.sleep(max(0.0, CADENCE_SECONDS - (time.time() - t0)))
    log.info("stopping (heartbeat=%d)", heartbeat)


if __name__ == "__main__":
    main()
