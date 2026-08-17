#!/bin/bash
# Supervisor-run acceptance for taey-delegate collect.
# Runs the five frozen acceptance paths from 02_frozen_work_order.json against
# the SUPERVISOR's own fixture, with ground truth computed before the tool existed.
WT=/home/mira/.peer-worktrees/infra-codex-vslice-collect
FIX=/tmp/vslice_supervisor_fixture_20260817
RUN=/tmp/vslice_supervisor_run_20260817
TOOL="env PYTHONPATH=$WT python3 -m fleet_orchestrator.cli_taey_delegate"

rm -rf "$RUN"; mkdir -p "$RUN"
cp "$FIX"/file_a.txt "$FIX"/file_b.bin "$FIX"/file_c.txt "$FIX"/file_zero.txt "$RUN"/

pass=0; fail=0
ok(){ echo "  PASS: $1"; pass=$((pass+1)); }
no(){ echo "  FAIL: $1"; fail=$((fail+1)); }

echo "##### TEST 1 — HAPPY PATH #####"
$TOOL collect "$RUN/file_a.txt" "$RUN/file_b.bin" "$RUN/file_c.txt" -o "$RUN/artifacts.json"
echo "exit=$?"
echo "--- manifest as written ---"
cat "$RUN/artifacts.json"
n=$(python3 -c "import json;print(len(json.load(open('$RUN/artifacts.json'))['artifacts']))")
[ "$n" = "3" ] && ok "3 entries" || no "expected 3 entries, got $n"

echo "--- independent reproduction: manifest vs my own sha256sum / wc -c ---"
python3 - <<'PY' > /tmp/_cmp.txt
import json,hashlib,os
m=json.load(open('/tmp/vslice_supervisor_run_20260817/artifacts.json'))
for a in m['artifacts']:
    p=a['path']
    real=hashlib.sha256(open(p,'rb').read()).hexdigest()
    rb=os.path.getsize(p)
    print(f"{os.path.basename(p)} manifest_sha={a['sha256']} disk_sha={real} sha_match={a['sha256']==real} manifest_bytes={a['bytes']} disk_bytes={rb} bytes_match={a['bytes']==rb}")
PY
cat /tmp/_cmp.txt
grep -q 'sha_match=False\|bytes_match=False' /tmp/_cmp.txt && no "manifest disagrees with disk" || ok "every sha256 and byte count reproduces from disk"

echo "--- vs GROUND TRUTH computed BEFORE the tool was written ---"
for f in file_a.txt file_b.bin file_c.txt; do
  gt=$(grep "/$f\$" /tmp/vslice_ground_truth.txt | awk '{print $1}')
  mf=$(python3 -c "import json;print([a['sha256'] for a in json.load(open('$RUN/artifacts.json'))['artifacts'] if a['path'].endswith('$f')][0])")
  if [ "$gt" = "$mf" ]; then ok "$f matches pre-computed ground truth ($gt)"; else no "$f GT=$gt manifest=$mf"; fi
done

echo; echo "##### TEST 4 — HASH SHAPE (^[0-9a-f]{64}\$) #####"
bad=$(python3 -c "
import json,re
m=json.load(open('$RUN/artifacts.json'))
print(sum(0 if re.fullmatch(r'[0-9a-f]{64}',a['sha256']) else 1 for a in m['artifacts']))")
[ "$bad" = "0" ] && ok "all sha256 are exactly 64 lowercase hex" || no "$bad malformed hashes"

echo; echo "##### TEST 2 — MISSING FILE #####"
before_sha=$(sha256sum "$RUN/artifacts.json" | awk '{print $1}')
before_mt=$(stat -c %Y.%N "$RUN/artifacts.json")
rm "$RUN/file_c.txt"
$TOOL collect "$RUN/file_a.txt" "$RUN/file_b.bin" "$RUN/file_c.txt" -o "$RUN/artifacts.json"
rc=$?; echo "exit=$rc"
[ "$rc" -ne 0 ] && ok "non-zero exit on missing file" || no "exited 0 with a missing file"
after_sha=$(sha256sum "$RUN/artifacts.json" | awk '{print $1}')
after_mt=$(stat -c %Y.%N "$RUN/artifacts.json")
[ "$before_sha" = "$after_sha" ] && ok "manifest content unchanged" || no "manifest was rewritten"
[ "$before_mt" = "$after_mt" ] && ok "manifest mtime unchanged (never touched)" || no "manifest mtime moved"

echo; echo "##### TEST 3 — ANTI-FABRICATION (path that never existed) #####"
GHOST=/tmp/careers_revloop_source_extract_20260816T194343Z/never_existed.txt
echo "target: $GHOST (the exact directory shape from the original fabrication)"
$TOOL collect "$GHOST" -o "$RUN/ghost.json"
rc=$?; echo "exit=$rc"
[ "$rc" -ne 0 ] && ok "refused a nonexistent path" || no "emitted an entry for a nonexistent path"
[ ! -e "$RUN/ghost.json" ] && ok "no manifest emitted for the ghost path" || no "ghost.json was created"

echo; echo "##### TEST 5 — BYTE FIDELITY #####"
printf 'line one\nline two\nline three\n' > "$RUN/file_c.txt"
$TOOL collect "$RUN/file_c.txt" -o "$RUN/fid1.json" >/dev/null; echo "baseline exit=$?"
s1=$(python3 -c "import json;a=json.load(open('$RUN/fid1.json'))['artifacts'][0];print(a['sha256'],a['bytes'])")
printf 'X' >> "$RUN/file_c.txt"
$TOOL collect "$RUN/file_c.txt" -o "$RUN/fid2.json" >/dev/null; echo "after-append exit=$?"
s2=$(python3 -c "import json;a=json.load(open('$RUN/fid2.json'))['artifacts'][0];print(a['sha256'],a['bytes'])")
echo "before: $s1"; echo "after : $s2"
[ "$s1" != "$s2" ] && ok "both sha256 and bytes changed after appending one byte" || no "manifest did not notice the byte"

echo; echo "##### EXTRA — ZERO-BYTE FILE (work order: hard failure) #####"
$TOOL collect "$RUN/file_zero.txt" -o "$RUN/zero.json"
rc=$?; echo "exit=$rc"
[ "$rc" -ne 0 ] && ok "zero-byte file rejected" || no "zero-byte file accepted"
[ ! -e "$RUN/zero.json" ] && ok "no manifest emitted for zero-byte input" || no "zero.json was created"

echo; echo "===== SUPERVISOR TOTAL: $pass passed, $fail failed ====="
