#!/usr/bin/env python3
"""Consult-display watchdog (infra).

The AT-SPI consult displays (:2-:6 / chatgpt|claude|gemini|grok|perplexity) degrade
under sustained automation: Firefox stays alive but loses AT-SPI registration (blank
page, doc tree collapses to raw=1, "Firefox not found" at navigate). A process-alive
check misses this, so we reuse taeys-hands' OWN readiness probe as the source of truth
and restart only on SUSTAINED degradation via the canonical systemd path.

Safety by design:
  * Source of truth = taeys-hands `scripts/display_readiness_check.py` (exit 0=ready, 2=degraded).
  * Restart only after FAIL_THRESHOLD consecutive degraded probes (transient blips don't trigger).
  * Per-display restart cap per hour; over the cap -> escalate, never restart-loop.
  * DISARMED by default: with no ARMED flag file it observes + logs + would-restart, but does NOT
    restart. Arming waits on taeys-hands confirming a mid-consult display is never seen as degraded
    (or a consult-in-progress lock to honor). `touch ~/.taey/display_watchdog_ARMED` to arm.
  * Canonical restart only: `systemctl --user restart taey-xvfb@N taey-display-N` (never restart_display.sh).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import redis as _redis
except ImportError:  # fail-open: no redis-py -> lock check disabled, probe as before
    _redis = None

PLATFORMS = ["chatgpt", "claude", "gemini", "grok", "perplexity"]
READINESS_CLI = "/home/mira/taeys-hands/scripts/display_readiness_check.py"
STATE_PATH = Path.home() / ".taey" / "display_watchdog_state.json"
ARMED_FLAG = Path.home() / ".taey" / "display_watchdog_ARMED"
LOG_PATH = Path.home() / ".taey" / "display_watchdog.log"
PAUSE_DIR = Path.home() / ".taey"

FAIL_THRESHOLD = 6          # consecutive degraded probes before a restart
MAX_RESTARTS_PER_HOUR = 3   # per display; over this -> escalate, do not restart
PROBE_TIMEOUT_S = 35
PAUSE_MAX_AGE = int(os.environ.get("PAUSE_MAX_AGE", "1800"))


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except OSError as exc:
        _log(f"WARN could not save state: {exc}")


def _probe(platform: str) -> dict:
    """Run taeys-hands' readiness CLI.

    Returns {ready, display, tree, issues, layer_failed, error}. Only a
    parseable L1 verdict is restart-worthy; probe timeouts/usage/errors stay
    inconclusive and must never be promoted into restart signals.
    """
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        proc = subprocess.run(
            ["python3", READINESS_CLI, platform, "--json"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ready": False, "display": None, "tree": None,
                "issues": ["probe timed out"], "layer_failed": None, "error": "timeout"}
    # exit code is the primary signal; JSON (after any diagnostic lines) adds detail.
    ready = proc.returncode == 0
    verdict = {}
    m = re.search(r"\{.*\}", proc.stdout, re.DOTALL)
    if m:
        try:
            verdict = json.loads(m.group(0))
        except ValueError:
            pass
    return {
        "ready": verdict.get("ready", ready),
        "display": verdict.get("display"),
        "tree": verdict.get("tree"),
        "issues": verdict.get("issues") or ([] if ready else [f"exit={proc.returncode}"]),
        "layer_failed": verdict.get("layer_failed"),
        "error": None if (verdict or proc.returncode in (0, 2)) else (proc.stderr.strip()[:200] or "probe error"),
    }


def _display_num(display: str | None) -> str | None:
    if not display:
        return None
    m = re.search(r"(\d+)", display)
    return m.group(1) if m else None


def _platform_display_num(platform: str) -> str | None:
    try:
        return str(PLATFORMS.index(platform) + 2)
    except ValueError:
        return None


def _consult_lock_active(platform: str) -> bool:
    """True if a consult holds the DISPLAY dispatch-lock for this platform's
    display — the canonical taeys-hands signal (Redis key ``taey:plan_active::{N}``,
    set once with a 3600s TTL at consult start and held for the FULL consult per
    ``primitives.acquire_display_lock``; verified live held across a >90s consult's
    response-wait). When it is held we MUST skip the AT-SPI probe: probing an
    actively-consulted display runs taeys-hands' readiness CLI (an AT-SPI tree
    query) concurrently with the consult engine's own AT-SPI snapshot, which makes
    Firefox momentarily unfindable and crashes the consult ("Firefox not found").
    This is the real bug — the watchdog read only file pause-flags that the live
    consult path never sets, while the true consult-active signal is this lock.
    Fail-open (return False) if redis-py is missing or Redis is unreachable — a
    consult cannot run without acquiring this lock, so probing is safe then.
    """
    if _redis is None:
        return False
    display_num = _platform_display_num(platform)
    if not display_num:
        return False
    try:
        client = _redis.Redis(host="127.0.0.1", port=6379,
                              socket_timeout=2, socket_connect_timeout=2)
        return bool(client.exists(f"taey:plan_active::{display_num}"))
    except Exception as exc:  # redis down / timeout / any client error -> fail-open
        _log(f"CONSULT-LOCK-CHECK-FAILED {platform} ({exc}); proceeding to probe")
        return False


def _pause_flag_paths(platform: str) -> list[Path]:
    paths = [PAUSE_DIR / f"display_watchdog_pause_{platform}"]
    display_num = _platform_display_num(platform)
    if display_num:
        paths.append(PAUSE_DIR / f"display_watchdog_pause_{display_num}")
    return paths


def _fresh_pause_flag(platform: str, now: float) -> Path | None:
    fresh: Path | None = None
    for path in _pause_flag_paths(platform):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age <= PAUSE_MAX_AGE:
            fresh = path
            break
        _log(f"STALE-PAUSE ignored {platform} flag={path.name} age_s={int(age)} max_age_s={PAUSE_MAX_AGE}")
    return fresh


def _restart(display_num: str) -> bool:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    cmd = ["systemctl", "--user", "restart",
           f"taey-xvfb@{display_num}", f"taey-display-{display_num}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _notify(target: str, msg: str) -> None:
    try:
        subprocess.run(["taey-notify", target, msg], timeout=15,
                       capture_output=True, text=True)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _prune_restarts(times: list[float], now: float) -> list[float]:
    return [t for t in times if now - t < 3600]


def _is_l1_failure(verdict: dict) -> bool:
    if verdict.get("layer_failed") == "L1":
        return True
    return any(str(issue).startswith("L1") for issue in verdict.get("issues") or [])


def _failure_class(verdict: dict) -> str:
    if verdict.get("ready"):
        return "ready"
    if _is_l1_failure(verdict):
        return "L1"
    return "L2" if verdict.get("layer_failed") == "L2" else "inconclusive"


def _observe_reason(verdict: dict) -> str:
    layer = verdict.get("layer_failed")
    if layer == "L2":
        return "L2"
    if verdict.get("error"):
        return f"inconclusive:{verdict['error']}"
    return "inconclusive"


def run_once(dry_run: bool = False) -> int:
    armed = ARMED_FLAG.exists() and not dry_run
    state = _load_state()
    now = time.time()
    degraded_count = 0

    for platform in PLATFORMS:
        st = state.setdefault(platform, {
            "consecutive_l1_failures": 0,
            "consecutive_inconclusive": 0,
            "inconclusive_alerted": False,
            "restarts": [],
            "inconclusive_notifies": [],
            "pending_restart": False,
            "cap_alerted": False,
        })
        if "consecutive_l1_failures" not in st:
            st["consecutive_l1_failures"] = st.pop("consecutive_degraded", 0)
        st.setdefault("consecutive_inconclusive", 0)
        st.setdefault("inconclusive_alerted", False)
        st.setdefault("inconclusive_notifies", [])
        st.setdefault("pending_restart", False)
        st.setdefault("cap_alerted", False)
        pause_flag = _fresh_pause_flag(platform, now)
        # A live consult is signalled EITHER by a manual file pause-flag OR by the
        # canonical Redis dispatch-lock the consult engine holds for the full run.
        # Either one means: do NOT probe (the AT-SPI probe would contend with the
        # consult's own AT-SPI access and crash it).
        consult_lock = pause_flag is None and _consult_lock_active(platform)
        if pause_flag is not None or consult_lock:
            reason = (f"flag={pause_flag.name}" if pause_flag is not None
                      else f"redis-lock=taey:plan_active::{_platform_display_num(platform)}")
            if st["pending_restart"]:
                _log(f"ABORTED-RESTART {platform} (consult active during grace) {reason}")
                st["pending_restart"] = False
            st["cap_alerted"] = False
            _log(f"PAUSED {platform} (consult in progress) {reason} "
                 f"holding_l1={st['consecutive_l1_failures']}")
            continue
        verdict = _probe(platform)
        failure_class = _failure_class(verdict)

        if failure_class == "ready":
            if st["consecutive_l1_failures"]:
                _log(f"OK {platform} recovered (tree={verdict['tree']})")
            st["consecutive_l1_failures"] = 0
            st["consecutive_inconclusive"] = 0
            st["inconclusive_alerted"] = False
            st["pending_restart"] = False
            st["cap_alerted"] = False
            continue

        degraded_count += 1
        if failure_class == "L2":
            if st["consecutive_l1_failures"]:
                _log(f"RESET {platform} l1_counter={st['consecutive_l1_failures']} reason=L2")
            st["consecutive_l1_failures"] = 0
            st["consecutive_inconclusive"] = 0
            st["inconclusive_alerted"] = False
            st["pending_restart"] = False
            st["cap_alerted"] = False
            _log(f"OBSERVE {platform} reason=L2 display={verdict['display']} "
                 f"tree={verdict['tree']} issues={verdict['issues']} error={verdict['error']}")
            continue

        if failure_class == "inconclusive":
            st["consecutive_inconclusive"] += 1
            _log(f"OBSERVE {platform} reason={_observe_reason(verdict)} display={verdict['display']} "
                 f"tree={verdict['tree']} issues={verdict['issues']} error={verdict['error']} "
                 f"consecutive_inconclusive={st['consecutive_inconclusive']} "
                 f"holding_l1={st['consecutive_l1_failures']}")
            if st["consecutive_inconclusive"] < FAIL_THRESHOLD or st["inconclusive_alerted"]:
                continue
            st["inconclusive_notifies"] = _prune_restarts(st["inconclusive_notifies"], now)
            if len(st["inconclusive_notifies"]) >= MAX_RESTARTS_PER_HOUR:
                _log(f"CAP {platform}: {len(st['inconclusive_notifies'])} inconclusive escalations/hr reached — NOT notifying")
                continue
            st["inconclusive_notifies"].append(now)
            st["inconclusive_alerted"] = True
            _notify("infra", f"display-watchdog: {platform} probe inconclusive for "
                             f"{st['consecutive_inconclusive']} sustained cycles; watchdog is observing only "
                             f"(reason={_observe_reason(verdict)}, holding L1 streak={st['consecutive_l1_failures']}).")
            continue

        st["consecutive_inconclusive"] = 0
        st["inconclusive_alerted"] = False
        st["consecutive_l1_failures"] += 1
        _log(f"L1 {platform} display={verdict['display']} "
             f"consecutive={st['consecutive_l1_failures']} tree={verdict['tree']} "
             f"issues={verdict['issues']}")

        if st["consecutive_l1_failures"] < FAIL_THRESHOLD:
            continue  # sustained guard: wait for more consecutive L1 failures

        dnum = _display_num(verdict["display"])
        if not dnum:
            _log(f"SKIP {platform}: cannot resolve display number; escalating")
            _notify("infra", f"display-watchdog: {platform} degraded but display unknown; manual check")
            continue

        st["restarts"] = _prune_restarts(st["restarts"], now)
        if len(st["restarts"]) >= MAX_RESTARTS_PER_HOUR:
            _log(f"CAP {platform}: {len(st['restarts'])} restarts/hr reached — escalating, NOT restarting")
            # De-duplicate: a sustained cap is NOT news every 90s. Notify ONCE per degraded
            # episode and stay quiet until the state actually changes (the recovery paths clear
            # this flag). An alert that fires every cycle trains every seat to ignore the
            # channel, which is exactly how a real alert gets missed later.
            if not st.get("cap_alerted"):
                st["cap_alerted"] = True
                _notify("taeys-hands", f"display-watchdog: {platform} (:{dnum}) hit restart cap "
                                       f"({MAX_RESTARTS_PER_HOUR}/hr) and is still degraded — needs a human look. "
                                       f"(further cap alerts for this episode suppressed until state changes)")
                _notify("infra", f"display-watchdog: {platform} restart cap hit; hard-degraded.")
            continue

        if not st["pending_restart"]:
            st["pending_restart"] = True
            _log(f"PENDING-RESTART {platform} (:{dnum}) sustained-L1 x{st['consecutive_l1_failures']}, NO pause flag")
            grace_msg = (
                f"⚠️ watchdog will restart {platform} (:{dnum}) next cycle (≈90s) — "
                f"sustained-L1 x{st['consecutive_l1_failures']} with NO consult pause-flag. "
                f"If a consult is LIVE, set the flag NOW to abort."
            )
            _notify("infra", f"display-watchdog: {grace_msg}")
            _notify("taeys-hands", f"display-watchdog: {grace_msg}")
            continue

        if not armed:
            reason = "dry-run" if dry_run else "DISARMED (no ARMED flag)"
            _log(f"WOULD-RESTART {platform} (:{dnum}) — {reason}; sustained L1 x{st['consecutive_l1_failures']}")
            continue

        _log(f"RESTART {platform} (:{dnum}) — sustained L1 x{st['consecutive_l1_failures']}")
        ok = _restart(dnum)
        st["restarts"].append(now)
        st["consecutive_l1_failures"] = 0
        st["pending_restart"] = False
        _notify("infra", f"display-watchdog: RESTARTED {platform} (:{dnum}) — unflagged sustained-L1 confirmed after grace.")
        _notify("taeys-hands", f"display-watchdog: auto-restarted {platform} (:{dnum}) after "
                               f"{FAIL_THRESHOLD} sustained-L1 probes — {'OK' if ok else 'restart FAILED'}.")
        if not ok:
            _log(f"RESTART FAILED {platform} (:{dnum})")
            _notify("infra", f"display-watchdog: restart of {platform} (:{dnum}) FAILED.")

    _save_state(state)
    return degraded_count


def main() -> int:
    ap = argparse.ArgumentParser(description="Consult-display AT-SPI watchdog")
    ap.add_argument("--dry-run", action="store_true",
                    help="probe + log intended actions, never restart, never write ARMED actions")
    ap.add_argument("--loop", type=float, metavar="SECS", default=0.0,
                    help="run continuously every SECS (default: one cycle and exit)")
    args = ap.parse_args()

    armed = ARMED_FLAG.exists()
    _log(f"watchdog start (armed={armed} dry_run={args.dry_run} "
         f"threshold={FAIL_THRESHOLD} cap={MAX_RESTARTS_PER_HOUR}/hr)")
    if args.loop <= 0:
        return 0 if run_once(dry_run=args.dry_run) == 0 else 0
    while True:
        run_once(dry_run=args.dry_run)
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
