#!/usr/bin/env python3
"""Write-time-binding race oracle (frozen requirement 6) — self-validating.

WHY THIS EXISTS
  v2's T-D1/T-D4 used fixed sleeps. Measured: hashing 600MB takes 0.27s, so a
  `sleep 1` mutation landed 240-416ms AFTER the command had already exited and
  committed. Those tests reported FAIL against code that had done nothing wrong.
  A fixed delay cannot express "during the run".

WHAT CHANGED
  The mutation is now triggered by OBSERVED progress of the tool process
  (/proc/<pid>/io rchar crossing into the large artifact), not by wall-clock.
  rchar is used rather than read_bytes because a page-cached file produces no
  physical reads and read_bytes would stay 0.

THE PART THAT MATTERS
  The oracle asserts its OWN precondition and reports INVALID when unmet. A trial
  only counts if the mutation completed before the manifest was committed. If the
  window was missed, this prints INVALID rather than PASS or FAIL. A test that
  cannot tell you it failed to test anything is how v2 lied.

Usage: python3 10_race_oracle.py <pythonpath-dir> <label>
"""
import atexit, hashlib, json, os, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path

PYPATH, LABEL = sys.argv[1], sys.argv[2]
BIG_MB = 600
# Both implementations read every artifact TWICE (the original's dead double-pass; the
# repair's collect + re-verify). A mutation must land in the SECOND pass to exercise the
# window at all - during the first pass it is simply picked up as the initial value.
# Trigger past one full traversal of the big artifact, not merely "somewhere in it".
TRIGGER_RCHAR = int(BIG_MB * 1.15) << 20

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()

work = Path(tempfile.mkdtemp(prefix="race_oracle_"))
# Registered at creation, not at each exit: this script has EIGHT sys.exit paths plus
# exception paths, and cleaning up at each one is the patch shape that misses the ninth.
# Found by conductor 2026-08-17 after this harness leaked 23 directories / 13G. /tmp and
# /var/spark are the SAME filesystem (/dev/nvme1n1p2), so the leak was consuming the ISMA
# disk headroom weaver had been warning about - an instrument doing real damage to another
# seat's substrate, not merely reporting a wrong verdict.
atexit.register(shutil.rmtree, work, True)
a = work / "a.txt"; big = work / "big.bin"; out = work / "m.json"
a.write_bytes(b"AAAA")
with open(big, "wb") as f:
    f.write(b"\0" * (BIG_MB << 20))
pre = sha(a)

state = {"mut_ns": None, "triggered": False}

def watcher(pid):
    """Mutate a.txt once the tool has demonstrably read into big.bin."""
    while True:
        try:
            with open(f"/proc/{pid}/io") as f:
                rchar = int([l for l in f if l.startswith("rchar")][0].split()[1])
        except (FileNotFoundError, ProcessLookupError, IndexError):
            return                                    # process gone; window missed
        if rchar > TRIGGER_RCHAR:
            a.write_bytes(b"BBBB")
            # Timestamp IMMEDIATELY. An os.sync() here inflated this by ~200ms (a global
            # flush after writing 600MB), which made a valid trial look like it mutated
            # after commit and produced a spurious INVALID.
            state["mut_ns"] = time.time_ns()
            state["rchar"] = rchar
            state["triggered"] = True
            return
        time.sleep(0.001)

env = dict(os.environ, PYTHONPATH=PYPATH, PYTHONDONTWRITEBYTECODE="1")

# ---------------------------------------------------------------------------
# POSITIVE CONTROL (fix for I1, found by conductor-grok 2026-08-17).
# Without this, an impostor that reads its inputs and then exits non-zero for an
# UNRELATED reason, writing nothing, scored PASS: the old logic read
# "non-zero exit + no manifest" as "detected drift". A tool that always fails
# would have greened T-D1 without implementing write-time binding at all.
# The evidence for drift detection is not the failure — it is the DIFFERENCE
# between an undisturbed run and a disturbed one. So: prove the tool succeeds
# when nothing interferes, before a failure under interference can mean anything.
# ---------------------------------------------------------------------------
ctl_out = work / "control.json"
ctl = subprocess.run(
    [sys.executable, "-B", "-m", "fleet_orchestrator.cli_taey_delegate",
     "collect", str(a), str(big), "-o", str(ctl_out)],
    env=env, capture_output=True, text=True)
if ctl.returncode != 0 or not ctl_out.exists():
    print(f"===== RACE ORACLE — {LABEL} =====")
    print(f"  control run exit     : {ctl.returncode}  manifest={ctl_out.exists()}")
    print(f"  control stderr       : {ctl.stderr.strip()[:120]}")
    print("  VERDICT: INVALID — the tool cannot even succeed with nothing interfering, "
          "so a hard-fail under mutation would prove nothing. No conclusion drawn.")
    sys.exit(2)
ctl_out.unlink()

proc = subprocess.Popen(
    [sys.executable, "-B", "-m", "fleet_orchestrator.cli_taey_delegate",
     "collect", str(a), str(big), "-o", str(out)],
    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
t = threading.Thread(target=watcher, args=(proc.pid,)); t.start()
stdout, stderr = proc.communicate()
end_ns = time.time_ns(); t.join()

post = sha(a)
manifest_mtime_ns = out.stat().st_mtime_ns if out.exists() else None
recorded = None
if out.exists():
    try:
        recorded = [x["sha256"] for x in json.load(open(out))["artifacts"]
                    if x["path"].endswith("a.txt")][0]
    except Exception:
        recorded = "<unparseable>"

print(f"===== RACE ORACLE — {LABEL} =====")
print(f"  exit code            : {proc.returncode}")
print(f"  stderr               : {stderr.strip()[:120]}")
print(f"  mutation triggered   : {state['triggered']}")
print(f"  sha before / after   : {pre[:16]}… / {post[:16]}…")
print(f"  manifest written     : {out.exists()}")
print(f"  manifest records     : {(recorded or '-')[:16]}…")

# --- validity gate: did we actually test the thing? ---
if not state["triggered"]:
    print("  VERDICT: INVALID — mutation never fired; no conclusion drawn"); sys.exit(2)
if manifest_mtime_ns is not None and state["mut_ns"] > manifest_mtime_ns:
    late_ms = (state['mut_ns'] - manifest_mtime_ns) / 1e6
    print(f"  VERDICT: INVALID — mutation landed {late_ms:.1f}ms AFTER the manifest "
          f"was committed; this is the v2 defect, no conclusion drawn"); sys.exit(2)
if proc.returncode == 0 and recorded == post and post != pre:
    # The tool recorded the POST-mutation value, so it read the file after the mutation:
    # the mutation preceded collection instead of landing inside the window. Calling this
    # PASS would be a false green - the window was never exercised.
    print("  VERDICT: INVALID — mutation landed BEFORE the artifact was first hashed; "
          "the window was never exercised, no conclusion drawn"); sys.exit(2)
# I2 (conductor-grok): the previous label here read "mutation preceded commit, and
# followed first hash", which claimed more than the trigger establishes. The rchar
# threshold proves aggregate I/O progress, NOT that the mutation landed between the
# first and second CONTENT hash of a.txt specifically. At a 1.15x trigger the window
# exercised is typically the fingerprint stable-check, not the re-hash equality path.
# State only what was measured.
print(f"  precondition         : control run succeeded; mutation fired at "
      f"rchar={state.get('rchar', 0) / 1e6:.0f}MB and preceded commit ✓")
print(f"  window exercised     : post-first-read drift detection (fingerprint and/or "
      f"re-hash path; this oracle does not distinguish which)")

# --- the actual verdict ---
if proc.returncode != 0:
    if out.exists():
        print("  VERDICT: FAIL — hard-failed but a manifest was still written"); sys.exit(1)
    print("  VERDICT: PASS — detected mid-run drift, refused to write a manifest"); sys.exit(0)
if recorded == post:
    print("  VERDICT: PASS — exit 0 and the manifest matches disk at write time"); sys.exit(0)
print(f"  VERDICT: FAIL — exit 0 with a manifest recording {recorded[:16]}… "
      f"while disk holds {post[:16]}… (requirement 6 violated)")
sys.exit(1)
