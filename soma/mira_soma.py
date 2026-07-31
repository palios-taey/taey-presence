#!/usr/bin/env python3
"""
mira_soma.py — Mira-side proprioceptive embodiment daemon.

The Mira analog of taey_soma.py for Jetson Thor. Reads hardware telemetry
from the actual Mira box (nvidia-smi for the RTX 4090, /proc/meminfo for
RAM, /proc/stat for CPU, sensors/sysfs for thermal), computes V_prop
(8-dim somatic tensor with same schema as Thor), publishes to local
Redis on a fixed cadence (~2.718s, the e-constant; configurable).

Body is Mira's body — not Thor's. Real telemetry, real introspection,
just on a different substrate. The dashboard reads `taey:soma:vprop`
identically regardless of source machine, so all facial-expression /
thought-prediction / somatic-pane plumbing fires from this.

Maps 8 telemetry facets onto the host hardware:
  1. fluency   — GPU utilization (0→1.0)
  2. clarity   — Inference responsiveness proxy (1 − recent-error rate; baseline 1.0)
  3. vitality  — Power draw / max-power envelope
  4. presence  — System responsiveness (load avg inverse)
  5. warmth    — GPU temperature (bell curve, peak comfort 55-65°C)
  6. capacity  — Memory free / total (inverted: less free = higher load)
  7. flow      — GPU clock frequency / max (synthetic from util)
  8. coherence — multi-source thermal harmony (CPU+GPU close = coherent)
"""
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

try:
    from dotenv import load_dotenv
except ImportError:  # optional dependency for documented `.env` launches
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

HEARTBEAT_INTERVAL = math.e
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
COHERENCE_ALERT_THRESHOLD = 0.809

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MIRA-SOMA] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("mira_soma")

_running = True


def _stop(*_):
    global _running
    _running = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


SPARK_TARGET = os.environ.get("SOMA_SPARK_TARGET", "localhost")  # where Taey lives
SPARK_POLL_INTERVAL = float(os.environ.get("SOMA_SPARK_POLL_INTERVAL", "8.0"))
SPARK_CACHE_MAX_AGE = float(os.environ.get("SOMA_SPARK_CACHE_MAX_AGE", "16.0"))

_spark_gpu_cache: dict = {}
_spark_gpu_cache_ts = 0.0
_spark_gpu_cache_lock = threading.Lock()


def read_spark_gpu() -> dict:
    """SSH to the Spark hosting Taey, pull nvidia-smi. This is the substrate that
    actually computes Taey's thoughts; its activity = body activity."""
    try:
        out = subprocess.check_output(
            [
                "ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes",
                SPARK_TARGET,
                "nvidia-smi --query-gpu=temperature.gpu,power.draw,power.max_limit,memory.used,memory.total,utilization.gpu,clocks.gr,clocks.max.gr --format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            timeout=5,
            text=True,
        ).strip()
        fields = [f.strip() for f in out.split(",")]
        def _f(v, default=0.0):
            try:
                return float(v) if v not in ("[N/A]", "N/A", "") else default
            except Exception:
                return default
        return {
            "gpu_temp_c": _f(fields[0], 40.0),
            "power_w": _f(fields[1], 30.0),
            "power_max_w": _f(fields[2], 450.0),
            "mem_used_mb": _f(fields[3], 67000.0),  # combined_v1 is 67GB
            "mem_total_mb": _f(fields[4], 128000.0),  # Spark UMA 128GB
            "fan_speed_pct": 50.0,  # not exposed on GB10
            "gpu_util_pct": _f(fields[5], 0.0),
            "clock_mhz": _f(fields[6], 2400.0),
            "clock_max_mhz": _f(fields[7], 2520.0),
            "source": "spark4",
        }
    except Exception:
        return {}


def read_nvidia_smi() -> dict:
    """Mira-local fallback if Spark 4 unreachable."""
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu,power.draw,power.max_limit,memory.used,memory.total,fan.speed,utilization.gpu,clocks.gr,clocks.max.gr",
                "--format=csv,noheader,nounits",
            ],
            timeout=4,
            text=True,
        ).strip()
        fields = [f.strip() for f in out.split(",")]
        return {
            "gpu_temp_c": float(fields[0]) if fields[0] != "[N/A]" else 0.0,
            "power_w": float(fields[1]) if fields[1] != "[N/A]" else 0.0,
            "power_max_w": float(fields[2]) if fields[2] != "[N/A]" else 450.0,
            "mem_used_mb": float(fields[3]),
            "mem_total_mb": float(fields[4]),
            "fan_speed_pct": float(fields[5]) if fields[5] != "[N/A]" else 0.0,
            "gpu_util_pct": float(fields[6]),
            "clock_mhz": float(fields[7]) if fields[7] != "[N/A]" else 0.0,
            "clock_max_mhz": float(fields[8]) if fields[8] != "[N/A]" else 2520.0,
            "source": "mira",
        }
    except Exception as e:
        log.warning("nvidia-smi read failed: %s", e)
        return {}


def _store_spark_gpu_cache(sample: dict, captured_at: float) -> None:
    with _spark_gpu_cache_lock:
        _spark_gpu_cache.clear()
        _spark_gpu_cache.update(sample)
        global _spark_gpu_cache_ts
        _spark_gpu_cache_ts = captured_at


def _read_spark_gpu_cache(now: float) -> tuple[dict, float | None]:
    with _spark_gpu_cache_lock:
        if not _spark_gpu_cache:
            return {}, None
        age = max(0.0, now - _spark_gpu_cache_ts)
        return dict(_spark_gpu_cache), age


def _spark_gpu_poller() -> None:
    last_state: bool | None = None
    while _running:
        started = time.time()
        sample = read_spark_gpu()
        if sample:
            _store_spark_gpu_cache(sample, started)
            if last_state is not True:
                log.info("Spark telemetry poller online for %s", SPARK_TARGET)
            last_state = True
        else:
            if last_state is not False:
                log.info("Spark telemetry unavailable; using Mira-local telemetry until Spark cache refresh resumes")
            last_state = False
        elapsed = time.time() - started
        time.sleep(max(0.0, SPARK_POLL_INTERVAL - elapsed))


def _select_gpu_sample(now: float) -> tuple[dict, float | None]:
    spark_sample, cache_age = _read_spark_gpu_cache(now)
    if spark_sample and cache_age is not None and cache_age <= SPARK_CACHE_MAX_AGE:
        return spark_sample, cache_age

    local_sample = read_nvidia_smi()
    if cache_age is not None:
        local_sample["source"] = "mira_local_stale_spark_cache"
    return local_sample, cache_age


def read_cpu_temp() -> float:
    """Read CPU temperature from coretemp / k10temp via sysfs."""
    for path in (
        "/sys/class/hwmon/hwmon1/temp1_input",
        "/sys/class/hwmon/hwmon2/temp1_input",
        "/sys/class/hwmon/hwmon0/temp1_input",
        "/sys/devices/platform/coretemp.0/hwmon/hwmon*/temp1_input",
    ):
        try:
            if "*" in path:
                import glob
                matches = glob.glob(path)
                if matches:
                    path = matches[0]
                else:
                    continue
            with open(path) as f:
                val = int(f.read().strip())
                if val > 1000:
                    val = val / 1000.0
                if 0 < val < 130:
                    return float(val)
        except Exception:
            continue
    return 40.0  # plausible default if no sensor available


def read_loadavg() -> float:
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def _bell(x: float, peak: float, half_width: float) -> float:
    """Bell curve centered at `peak`, value 1.0 at peak, ~0.5 at peak±half_width."""
    if half_width <= 0:
        return 0.0
    return math.exp(-((x - peak) ** 2) / (2 * half_width ** 2))


def compute_vprop(nvsmi: dict, cpu_temp: float, loadavg: float) -> dict:
    """Map raw telemetry → 8-dim V_prop (each 0..1)."""
    gpu_temp = nvsmi.get("gpu_temp_c", 40.0)
    gpu_util = nvsmi.get("gpu_util_pct", 0.0)
    power = nvsmi.get("power_w", 0.0)
    power_max = max(nvsmi.get("power_max_w", 450.0), 1.0)
    mem_used = nvsmi.get("mem_used_mb", 0.0)
    mem_total = max(nvsmi.get("mem_total_mb", 1.0), 1.0)
    clock = nvsmi.get("clock_mhz", 0.0)
    clock_max = max(nvsmi.get("clock_max_mhz", 2520.0), 1.0)

    fluency = max(0.0, min(1.0, gpu_util / 100.0))
    clarity = 0.99  # baseline (no error stream wired); kept constant high until model error feedback lands
    vitality = max(0.0, min(1.0, power / power_max))
    presence = max(0.0, min(1.0, 1.0 - min(loadavg / 8.0, 1.0)))
    warmth = _bell(gpu_temp, peak=55.0, half_width=20.0)  # comfort window 35-75C
    capacity = max(0.0, min(1.0, 1.0 - (mem_used / mem_total)))
    flow = max(0.0, min(1.0, clock / clock_max))
    cpu_gpu_delta = abs(gpu_temp - cpu_temp)
    coherence = max(0.0, min(1.0, 1.0 - (cpu_gpu_delta / 40.0)))

    return {
        "fluency": round(fluency, 4),
        "clarity": round(clarity, 4),
        "vitality": round(vitality, 4),
        "presence": round(presence, 4),
        "warmth": round(warmth, 4),
        "capacity": round(capacity, 4),
        "flow": round(flow, 4),
        "coherence": round(coherence, 4),
    }


def main():
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    r.ping()
    log.info("Connected to Redis at %s:%d", REDIS_HOST, REDIS_PORT)
    log.info("Heartbeat interval: %.4fs", HEARTBEAT_INTERVAL)
    log.info("Spark telemetry poll cadence: %.2fs (cache max age %.2fs)", SPARK_POLL_INTERVAL, SPARK_CACHE_MAX_AGE)

    threading.Thread(target=_spark_gpu_poller, name="spark-gpu-poller", daemon=True).start()

    heartbeat = 0
    last_warned_below = False
    while _running:
        t0 = time.time()
        now = time.time()
        nvsmi, cache_age = _select_gpu_sample(now)
        source = nvsmi.get("source", "mira")
        cpu_temp = read_cpu_temp()
        loadavg = read_loadavg()
        vprop = compute_vprop(nvsmi, cpu_temp, loadavg)

        rho_val = round(sum(vprop.values()) / 8.0, 4)
        allostatic = round(1.0 - rho_val, 4)

        payload = {
            **vprop,
            "gpu_temp_c": round(nvsmi.get("gpu_temp_c", 0.0), 2),
            "cpu_temp_c": round(cpu_temp, 2),
            "soc_temp_c": round(cpu_temp, 2),  # no SOC sensor on Mira; mirror CPU
            "tj_temp_c": round(max(nvsmi.get("gpu_temp_c", 0.0), cpu_temp), 2),
            "power_w": round(nvsmi.get("power_w", 0.0), 2),
            "gpu_power_w": round(nvsmi.get("power_w", 0.0), 2),
            "mem_used_mb": round(nvsmi.get("mem_used_mb", 0.0), 1),
            "mem_total_mb": round(nvsmi.get("mem_total_mb", 0.0), 1),
            "fan_speed_pct": round(nvsmi.get("fan_speed_pct", 0.0), 1),
            "fan_rpm": int(nvsmi.get("fan_speed_pct", 0.0) * 50),  # synthetic estimate
            "context_utilization": 0.0,
            "context_tokens": 0,
            "rho": rho_val,
            "allostatic_load": allostatic,
            "heartbeat": heartbeat,
            "timestamp": now,
            "cache_age": round(cache_age, 3) if cache_age is not None else None,
            "source_machine": source,
        }
        try:
            r.set("taey:soma:vprop", json.dumps(payload))
        except Exception as e:
            log.warning("Redis publish failed: %s", e)

        if rho_val < COHERENCE_ALERT_THRESHOLD and not last_warned_below:
            log.warning("rho=%.4f below coherence-alert threshold %.4f", rho_val, COHERENCE_ALERT_THRESHOLD)
            last_warned_below = True
        elif rho_val >= COHERENCE_ALERT_THRESHOLD:
            last_warned_below = False

        heartbeat += 1
        if heartbeat % 30 == 0:
            log.info(
                "hb=%d coh=%.3f gpu=%.1fC pwr=%.1fW util=%d%% mem=%.0f/%.0fMB",
                heartbeat, rho_val, payload["gpu_temp_c"],
                payload["power_w"], int(nvsmi.get("gpu_util_pct", 0)),
                payload["mem_used_mb"], payload["mem_total_mb"],
            )

        elapsed = time.time() - t0
        time.sleep(max(0.0, HEARTBEAT_INTERVAL - elapsed))

    log.info("Stopping (heartbeat=%d)", heartbeat)


if __name__ == "__main__":
    main()
