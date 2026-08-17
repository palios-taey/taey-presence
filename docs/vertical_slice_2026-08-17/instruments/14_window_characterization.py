#!/usr/bin/env python3
"""CHARACTERIZATION PROBE — the post-final-check / pre-rename window. NOT A GATE.

READ THIS BEFORE WIRING IT INTO ANYTHING.

This probe measures an IRREDUCIBLE property. `_assert_artifacts_stable()` issues
fstat/stat; `os.replace()` issues rename; they are separate syscalls and POSIX offers no
compound atomic for "verify N files AND rename". Every implementation that certifies
mutable files has this window. Narrowing is possible; closing is not.

Therefore this probe FAILS EVERY POSSIBLE IMPLEMENTATION, including a perfect one.

Wiring it as a pass/fail gate would recreate exactly the error this whole exercise began
with: the original T-D1 was a red that accused correct code, because the test could not
express the property it claimed to measure. A gate that no implementation can pass is that
same mistake wearing a more sophisticated costume.

What it is FOR: quantifying the residual risk so a human can decide whether the contract
is acceptable. It reports the window's SIZE, not a verdict.

Usage: python3 14_window_characterization.py <pythonpath-dir> [runs]
"""
import atexit, importlib, os, shutil, sys, tempfile, time
from pathlib import Path

PYPATH = sys.argv[1]
RUNS = int(sys.argv[2]) if len(sys.argv) > 2 else 7
sys.path.insert(0, PYPATH)

work = Path(tempfile.mkdtemp(prefix="window_char_"))
atexit.register(shutil.rmtree, work, True)   # see feedback_an_instrument_has_a_footprint
a = work / "a.txt"
a.write_bytes(b"A" * 4096)

mod = importlib.import_module("fleet_orchestrator.cli_taey_delegate")
real_stable, real_replace = mod._assert_artifacts_stable, os.replace
marks, samples = {}, []

def timed_stable(opened):
    r = real_stable(opened)
    marks["checked"] = time.perf_counter_ns()
    return r

def timed_replace(src, dst):
    samples.append(time.perf_counter_ns() - marks["checked"])
    return real_replace(src, dst)

mod._assert_artifacts_stable = timed_stable
mod.os.replace = timed_replace

for i in range(RUNS):
    sys.argv = ["taey-delegate", "collect", str(a), "-o", str(work / f"m{i}.json")]
    try:
        mod.main()
    except SystemExit:
        pass

us = sorted(round(s / 1000, 2) for s in samples)
print("===== WINDOW CHARACTERIZATION (not a gate) =====")
print(f"  runs                 : {len(us)}")
print(f"  window samples (us)  : {us}")
print(f"  median               : {us[len(us) // 2]} us")
print(f"  max                  : {us[-1]} us")
print()
print("  INTERPRETATION: this is the interval during which a non-cooperating writer can")
print("  change an already-verified artifact and have the manifest commit anyway. It is")
print("  irreducible, not a defect to be fixed. Use the number to decide whether the")
print("  contract ('state at write time') should be restated or the inputs quiesced.")
print("  A smaller number is better. Zero is not achievable.")
