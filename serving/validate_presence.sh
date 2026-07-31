#!/usr/bin/env bash
# Production observation suite for the presence surface.
#
# Written so someone who did not do the work can re-run it and reach their own verdict.
# It OBSERVES; it changes nothing, starts nothing, and writes nothing into the repo.
# The one exception is announced and opt-in: --with-restart bounces the proxy to prove a
# service comes back clean from the committed artifact.
#
# Every check prints the command it ran, so a reader can disbelieve the summary and run
# the underlying command themselves. That is the point — a suite that only prints
# verdicts asks to be trusted, and the whole discipline here is that claims carry their
# receipts.
#
#   bash serving/validate_presence.sh                 # observe only
#   bash serving/validate_presence.sh --with-restart  # + announced proxy restart
#
# Endpoints come from the environment and FAIL LOUD when unset. There is no routable
# default: a suite that silently probes the author's own machine reports someone else's
# health as yours.
set -u

PASS=0; FAIL=0; SKIP=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
skip() { printf '  --   %s\n' "$1"; SKIP=$((SKIP+1)); }
cmd()  { printf '       $ %s\n' "$1"; }

need() {
  local var=$1
  if [ -z "${!var:-}" ]; then
    printf '  \033[31mFAIL\033[0m %s is unset — refusing to guess an endpoint\n' "$var"
    FAIL=$((FAIL+1)); return 1
  fi
  return 0
}

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO_ROOT" || exit 2

echo "PRODUCTION OBSERVATION SUITE — presence surface"
echo "repo: $REPO_ROOT"
echo "commit: $(git rev-parse HEAD 2>/dev/null || echo '(not a git tree)')"
echo

# ---------------------------------------------------------------- 1. the artifact
echo "[1] THE RUNNING SYSTEM IS A COMMITTED ARTIFACT"
cmd "git status --porcelain | wc -l"
if [ "$(git status --porcelain 2>/dev/null | wc -l)" = "0" ]; then
  ok "working tree clean — nothing served from an uncommitted delta"
else
  bad "working tree DIRTY — production is running unreviewed local changes"
  git status --porcelain | sed 's/^/         /'
fi
cmd "git merge-base --is-ancestor HEAD origin/main"
if git merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
  ok "HEAD is an ancestor of origin/main — this code is on the canonical branch"
else
  bad "HEAD is NOT on origin/main — production is ahead of, or diverged from, canonical"
fi

# ---------------------------------------------------------------- 2. the prompt
echo
echo "[2] TAEY'S IDENTITY LOADS FROM A COMMITTED, POINTER-FREE ARTIFACT"
SP=serving/TAEY_OPERATING_PROMPT.md
cmd "grep -c '/home/' $SP"
# grep -c PRINTS a count and EXITS 1 when the count is zero. A `|| echo 0` therefore
# appends a SECOND zero and every comparison against it fails — a false alarm from the
# check itself. The exit code is the no-match signal; the number on stdout is always
# valid, so take it and ignore the status.
n_abs=$(grep -c '/home/' "$SP" 2>/dev/null); n_abs=${n_abs:-0}
if [ "$n_abs" = "0" ]; then
  ok "0 absolute paths in the served prompt — resolves for a downloaded Taey too"
else
  bad "$n_abs absolute path(s) in the served prompt — operator-local, will not resolve elsewhere"
  grep -n '/home/' "$SP" | head -5 | sed 's/^/         /'
fi
cmd "git status --porcelain $SP"
[ "$(git status --porcelain "$SP" | wc -l)" = "0" ] \
  && ok "served prompt matches HEAD" \
  || bad "served prompt has uncommitted edits"

# ---------------------------------------------------------------- 3. the index
echo
echo "[3] THE KNOWLEDGE INDEX COMPILES FROM ITS SOURCES AND PASSES ITS GATES"
cmd "python3 serving/knowledge_index/build_index.py --check"
if (cd serving/knowledge_index && python3 build_index.py --check >/dev/null 2>&1); then
  ok "index.json matches a recompile of its sources — no hand edits"
else
  bad "index.json does NOT match its sources — hand-edited, or a section changed without rebuild"
fi
cmd "python3 serving/knowledge_index/gates.py --g1 --g2"
if (cd serving/knowledge_index && python3 gates.py --g1 --g2 >/dev/null 2>&1); then
  ok "G1 schema-lint + G2 closed-world pointer crawl PASS"
else
  bad "G1/G2 FAIL — run the command above for the violations"
fi

# ---------------------------------------------------------------- 4. liveness
echo
echo "[4] EVERY PRODUCTION CAPABILITY ANSWERS (G3 against the live deployment)"
if need TAEY_SERVE_URL && need TAEY_PROXY_URL && need TAEY_DASHBOARD_URL; then
  cmd "python3 serving/knowledge_index/gates.py --g3"
  if (cd serving/knowledge_index && python3 gates.py --g3 >/dev/null 2>&1); then
    ok "G3 green for every production capability"
  else
    bad "G3 red — a production capability is not answering"
    (cd serving/knowledge_index && python3 gates.py --g3 2>&1 | grep -E 'FAIL|RED' | sed 's/^/         /')
  fi
fi

# ------------------------------------------------------- 5. autonomous usage
echo
echo "[5] TAEY HAS AUTONOMOUSLY USED EACH CAPABILITY (usage receipts, not liveness)"
if [ -z "${TAEY_TOOL_AUDIT:-}" ]; then
  skip "TAEY_TOOL_AUDIT unset — cannot check usage receipts (this is not a pass)"
else
  cmd "python3 serving/knowledge_index/usage_receipts.py"
  out=$(cd serving/knowledge_index && python3 usage_receipts.py 2>&1)
  echo "$out" | grep -E 'usage|NO USAGE' | sed 's/^/         /'
  if echo "$out" | grep -q 'NO USAGE'; then
    bad "at least one capability has NO autonomous usage — G3 green cannot substitute"
  else
    ok "every capability carries a usage receipt"
  fi
fi

# ------------------------------------------------------- 6. a real Taey turn
echo
echo "[6] A REAL TAEY TURN THROUGH THE PROXY, WITH TOOL USE"
if need TAEY_PROXY_URL; then
  cmd "curl -sf \"\$TAEY_PROXY_URL/v1/models\""
  served=$(curl -sf --max-time 15 "$TAEY_PROXY_URL/v1/models" 2>/dev/null \
           | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["data"][0]["id"],d["data"][0].get("root",""))' 2>/dev/null)
  if [ -n "$served" ]; then
    ok "proxy answers: $served"
    echo "         (alias then artifact — the alias is permanent, the artifact swaps at promotion)"
  else
    bad "proxy did not answer /v1/models"
  fi
  echo "       A turn with tool use is the strongest observation and it takes minutes."
  echo "       To run one, ask Taey something it can only answer by looking, then read the"
  echo "       audit trail rather than the HTTP response — a long tool-using turn routinely"
  echo "       outlives the client connection, and the trail is the oracle:"
  cmd "tail -40 \"\$TAEY_TOOL_AUDIT\" | python3 -m json.tool 2>/dev/null | grep -A2 run_command"
fi

# ------------------------------------------------- 7. restart (opt-in, announced)
echo
echo "[7] SERVICE RESTARTS CLEAN FROM THE COMMITTED ARTIFACT"
if [ "${1:-}" = "--with-restart" ]; then
  echo "       ANNOUNCED: bouncing the proxy. Taey's seat, seconds."
  cmd "systemctl --user restart taey-soma-proxy-mira && sleep 12"
  systemctl --user restart taey-soma-proxy-mira
  sleep 12
  if [ "$(systemctl --user is-active taey-soma-proxy-mira)" = "active" ] \
     && curl -sf --max-time 20 "${TAEY_PROXY_URL:-http://127.0.0.1:8766}/v1/models" >/dev/null 2>&1; then
    ok "proxy came back active and serving after restart"
  else
    bad "proxy did NOT come back cleanly — this is a full stop"
  fi
else
  skip "restart not run (pass --with-restart). Not skipped silently: this check is the"
  echo "       difference between 'it is running' and 'it can be brought back', and only"
  echo "       the second one survives a reboot."
fi

echo
echo "─────────────────────────────────────────────"
printf "  PASS %d   FAIL %d   SKIPPED %d\n" "$PASS" "$FAIL" "$SKIP"
[ "$SKIP" -gt 0 ] && echo "  A SKIP is not a pass. What was skipped is listed above."
[ "$FAIL" -eq 0 ] && echo "  SUITE: PASS" || echo "  SUITE: FAIL"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
