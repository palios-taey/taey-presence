#!/bin/bash
# Supervisor acceptance v2 for taey-delegate collect.
#
# v1 (05_supervisor_acceptance.sh) reported 14/14 on commit 7538c98f and was wrong in
# four ways, all found by independent audit and confirmed by infra:
#   DEF-8  it always exited 0 - a gate that cannot fail its caller (fixed: final [ fail -eq 0 ])
#   DEF-9  it never asserted happy-path exit status          (fixed: T-HAPPY)
#   DEF-10 it compared a combined "sha bytes" string          (fixed: fields compared separately)
#   DEF-11 it never exercised the setup.py console script     (fixed: T-ENTRY)
# and it was structurally blind to frozen requirement 6       (fixed: T-D1)
# plus the output/input alias case                            (fixed: T-ALIAS)
#
# T-D1 and T-ALIAS are REGRESSION ORACLES: both have been OBSERVED to FAIL on 7538c98f.
# If either reports PASS on 7538c98f, this instrument has broken - it does not mean the
# code was fixed.
#
# Usage: bash 07_supervisor_acceptance_v2.sh <worktree-path>
WT="${1:?usage: $0 <worktree-path>}"
TOOL="env PYTHONPATH=$WT python3 -m fleet_orchestrator.cli_taey_delegate"
R=$(mktemp -d /tmp/vslice_v2_XXXXXX)
pass=0; fail=0
ok(){ echo "  PASS: $1"; pass=$((pass+1)); }
no(){ echo "  FAIL: $1"; fail=$((fail+1)); }
trap 'rm -rf "$R"' EXIT

echo "worktree under test: $WT"
echo "HEAD: $(git -C "$WT" rev-parse HEAD)"
echo

echo "##### T-HAPPY — happy path, exit asserted, fields compared INDEPENDENTLY #####"
printf 'alpha\n' > "$R/h1.txt"; head -c 2048 /dev/urandom > "$R/h2.bin"
$TOOL collect "$R/h1.txt" "$R/h2.bin" -o "$R/happy.json" >/dev/null
rc=$?
[ "$rc" -eq 0 ] && ok "happy path exited 0 (asserted, not just printed)" || no "happy path exit=$rc"
python3 - "$R/happy.json" > "$R/cmp.txt" <<'PY'
import json,hashlib,os,sys
m=json.load(open(sys.argv[1]))
for a in m['artifacts']:
    p=a['path']
    print(os.path.basename(p),
          'sha_ok='  + str(a['sha256']==hashlib.sha256(open(p,'rb').read()).hexdigest()),
          'byte_ok=' + str(a['bytes']==os.path.getsize(p)))
PY
cat "$R/cmp.txt" | sed 's/^/    /'
grep -q 'sha_ok=False'  "$R/cmp.txt" && no "a sha256 disagreed with disk"      || ok "every sha256 reproduces from disk"
grep -q 'byte_ok=False' "$R/cmp.txt" && no "a byte count disagreed with disk"  || ok "every byte count reproduces from disk"

echo; echo "##### T-D1 — WRITE-TIME BINDING (frozen requirement 6) #####"
# v2's original fixed-sleep version of this test was WRONG and passed judgment on code
# that had done nothing: hashing 600MB takes 0.27s, so a sleep-1 mutation landed 240-416ms
# AFTER the command exited and committed. Delegated to the event-synchronized oracle, which
# triggers on observed I/O progress and reports INVALID rather than guessing.
# The mutation is AAAA->BBBB: same byte length, so this also covers the DEF-5 same-size case.
ORACLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/10_race_oracle.py"
d1=2; for attempt in 1 2 3; do
  python3 "$ORACLE" "$WT" "T-D1 attempt $attempt" | sed 's/^/    /'
  d1=${PIPESTATUS[0]}
  [ "$d1" -ne 2 ] && break
  echo "    (invalid trial - window missed, retrying)"
done
case "$d1" in
  0) ok "write-time binding enforced (event-synchronized, valid trial)" ;;
  1) no "exit 0 with a manifest not matching disk at write time (DEF-1)" ;;
  *) echo "  INVALID: could not obtain a valid trial in 3 attempts - NO VERDICT"
     fail=$((fail+1)) ;;
esac

echo; echo "##### T-ALIAS — output path aliases a declared artifact (DEF-2) #####"
printf 'original artifact content' > "$R/alias.txt"
before=$(sha256sum "$R/alias.txt" | awk '{print $1}')
$TOOL collect "$R/alias.txt" -o "$R/alias.txt" >/dev/null 2>&1
rc=$?
after=$(sha256sum "$R/alias.txt" | awk '{print $1}')
if [ "$rc" -ne 0 ]; then ok "refused output path that aliases a declared artifact"
else
  echo "    exit=0  before=$before"; echo "            after =$after"
  [ "$before" = "$after" ] && ok "artifact survived the run" \
                           || no "exit 0 and the declared artifact was DESTROYED by the manifest (DEF-2)"
fi

# T-D4 (same-size concurrent rewrite) REMOVED as a separate test. Its fixed-sleep form had
# the identical defect as the old T-D1 (mutation measured 240ms after exit), and its actual
# subject - a same-length content swap mid-run - is exactly what T-D1's AAAA->BBBB oracle
# now exercises under a verified ordering. A second, weaker test of the same property would
# add noise, not coverage.

echo; echo "##### T-REG — NON-REGULAR FILES (DEF-6, informational) #####"
mkfifo "$R/fifo" 2>/dev/null
$TOOL collect "$R/fifo" -o "$R/m3.json" >/dev/null 2>&1
[ $? -ne 0 ] && ok "FIFO rejected" || no "FIFO accepted"
$TOOL collect /dev/zero -o "$R/m4.json" >/dev/null 2>&1
[ $? -ne 0 ] && ok "/dev/zero rejected" || no "/dev/zero accepted"

echo; echo "##### T-PERM — DIRECTORY, UNREADABLE, DANGLING SYMLINK #####"
mkdir -p "$R/adir"
$TOOL collect "$R/adir" -o "$R/m5.json" >/dev/null 2>&1
[ $? -ne 0 ] && ok "directory rejected" || no "directory accepted"
printf 'secret' > "$R/noread.txt"; chmod 000 "$R/noread.txt"
$TOOL collect "$R/noread.txt" -o "$R/m6.json" >/dev/null 2>&1
[ $? -ne 0 ] && ok "unreadable file rejected" || no "unreadable file accepted"
chmod 644 "$R/noread.txt"
ln -s "$R/nonexistent_target" "$R/dangling"
$TOOL collect "$R/dangling" -o "$R/m7.json" >/dev/null 2>&1
[ $? -ne 0 ] && ok "dangling symlink rejected" || no "dangling symlink accepted"

echo; echo "##### T-MISSING — failure must not touch a prior manifest #####"
printf 'one' > "$R/k1.txt"; printf 'two' > "$R/k2.txt"
$TOOL collect "$R/k1.txt" "$R/k2.txt" -o "$R/keep.json" >/dev/null 2>&1
b_sha=$(sha256sum "$R/keep.json" | awk '{print $1}'); b_mt=$(stat -c %Y.%N "$R/keep.json")
rm "$R/k2.txt"
$TOOL collect "$R/k1.txt" "$R/k2.txt" -o "$R/keep.json" >/dev/null 2>&1
[ $? -ne 0 ] && ok "missing file -> non-zero exit" || no "missing file exited 0"
a_sha=$(sha256sum "$R/keep.json" | awk '{print $1}'); a_mt=$(stat -c %Y.%N "$R/keep.json")
[ "$b_sha" = "$a_sha" ] && ok "prior manifest content unchanged" || no "prior manifest content changed"
[ "$b_mt" = "$a_mt" ]   && ok "prior manifest mtime unchanged"   || no "prior manifest mtime moved"

echo; echo "##### T-ERR — ERROR CONTRACT (DEF-4: bare RuntimeError leaks a traceback?) #####"
printf 'ok' > "$R/e.txt"
err=$($TOOL collect "$R/e.txt" -o /nonexistent_dir_xyz/deep/m.json 2>&1 >/dev/null)
echo "$err" | grep -q 'Traceback' && no "unhandled traceback leaked instead of the ERROR contract" \
                                  || ok "error path honored the ERROR contract"

echo; echo "##### T-ENTRY — setup.py console script actually resolves (DEF-11) #####"
# Install from a COPY: `pip install -e` writes *.egg-info into the source tree, and an
# instrument must not mutate the artifact it is measuring (it is gitignored here, so this
# contaminated the worktree without ever showing in git status).
cp -r "$WT" "$R/srccopy" 2>/dev/null
if python3 -m venv "$R/venv" >/dev/null 2>&1 && \
   "$R/venv/bin/pip" install -q -e "$R/srccopy" --no-deps >/dev/null 2>&1; then
  "$R/venv/bin/taey-delegate" collect "$R/h1.txt" -o "$R/entry.json" >/dev/null 2>&1
  [ $? -eq 0 ] && [ -e "$R/entry.json" ] && ok "installed console script taey-delegate works" \
                                         || no "console script installed but failed to run"
else
  echo "  SKIP: venv/editable-install unavailable in this environment (entry point unproven)"
fi

echo; echo "===== v2 TOTAL: $pass passed, $fail failed ====="
echo "NOTE: on 7538c98f, T-D1 and T-ALIAS are EXPECTED TO FAIL. Passes there would mean"
echo "      this instrument stopped working, not that the defects were fixed."
[ "$fail" -eq 0 ]
