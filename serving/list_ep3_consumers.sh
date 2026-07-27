#!/usr/bin/env bash
# list_ep3_consumers.sh — enumerate who depends on an ep3 serving endpoint, by READING configs.
#
# WHY THIS EXISTS: over two days, four separate consumers were missed when bouncing a serving
# endpoint — each time by notifying the consumers someone remembered rather than the ones that
# actually resolve the endpoint. A remembered list is not a list. This makes the consumer set a
# command you RUN at swap time, so "who does this break" is read from the things that implement
# the dependency instead of recalled by whoever is awake.
#
# It reports three classes, because they need different handling:
#   PINNED-NO-FAILOVER : hard-coded to one host, cannot redirect -> STOP it for the window
#   REDIRECTABLE       : host in env/config -> can be repointed at the other Thor
#   WEIGHTS-WATCHER    : does not send inference, but READS the served-weights root and gates on
#                        it -> still needs pre-bounce notice (this class was missed entirely)
#
# Usage:  ./list_ep3_consumers.sh [host-substring]     e.g. ./list_ep3_consumers.sh 10.0.0.8
# Exit 0 always — this informs a decision, it does not gate one.
set -uo pipefail
TARGET="${1:-}"
HL() { printf '\n\033[1m%s\033[0m\n' "$*"; }
match() { [ -z "$TARGET" ] || grep -q -- "$TARGET" <<<"$1"; }

HL "== systemd units referencing an ep3 endpoint =="
for scope in "--user" ""; do
  # shellcheck disable=SC2086
  systemctl $scope list-units --type=service --all --no-legend 2>/dev/null | awk '{print $1}' | while read -r u; do
    [ -n "$u" ] || continue
    env=$(systemctl $scope show "$u" -p Environment 2>/dev/null)
    case "$env" in
      *EP3_BASE*|*EP3_OVERSEER*|*:8000*|*ep3*)
        ep=$(tr ' ' '\n' <<<"$env" | grep -oE 'https?://[0-9.]+:[0-9]+[^ ]*' | sort -u | tr '\n' ' ')
        [ -z "$ep" ] && continue
        match "$ep" || continue
        scopetag=$([ -n "$scope" ] && echo user || echo system)
        # a unit naming exactly one host with no alternate cannot fail over
        n=$(tr ' ' '\n' <<<"$ep" | grep -c 'http')
        cls=$([ "$n" -le 1 ] && echo "PINNED-NO-FAILOVER" || echo "REDIRECTABLE")
        printf '  %-26s %-7s %-19s %s\n' "$u" "$scopetag" "$cls" "$ep"
        ;;
    esac
  done
done

HL "== env FILES referencing an ep3 endpoint (systemd Environment= scan MISSES these) =="
# A unit that reads its endpoint from an EnvironmentFile/.env shows nothing in `systemctl show
# -p Environment`. apply-loop is exactly this shape, which is why the unit scan alone is not
# sufficient and this section exists.
found=0
for f in /home/*/apply-machine/.env /home/*/treasurer/.env /home/*/*/.env; do
  [ -r "$f" ] || continue
  hits=$(grep -hoE '(EP3[A-Z_]*|[A-Z_]*BASE)=https?://[0-9.]+:[0-9]+[^ ]*' "$f" 2>/dev/null | sort -u)
  [ -z "$hits" ] && continue
  match "$hits" || continue
  found=1
  printf '  %s\n%s\n' "$f" "$(sed 's/^/    /' <<<"$hits")"
done
[ "$found" -eq 0 ] && echo "  (none found in the scanned paths — see LIMITS, this is not proof of none)"

HL "== live TCP consumers on the serving ports (catches what config grep cannot) =="
for h in 10.0.0.8 10.0.0.197; do
  match "$h" || continue
  out=$(ss -tnp 2>/dev/null | grep ":8000" | grep "$h" | awk '{print $5, $6}' | sort -u)
  [ -n "$out" ] && printf '  %s:\n%s\n' "$h" "$(sed 's/^/    /' <<<"$out")" \
                || printf '  %s: (no ESTABLISHED connections at this instant)\n' "$h"
done
echo "  NOTE: a point-in-time check MISSES intermittent consumers. Never treat 'none now' as 'none'."

HL "== SOURCE LITERALS — endpoints hardcoded or defaulted in code, invisible to any config scan =="
# A unit/env scan finds consumers that RESOLVE their endpoint from configuration. It cannot find one
# that carries the address as a literal in source, or as a default in an os.environ.get(...) fallback
# that nobody ever sets. Those consumers are real, they hit the node, and they will not appear above.
# Proven 2026-07-27: apply-loop/apply-scorer had been repointed to the other Thor SEVEN HOURS earlier
# via a drop-in, yet Thor1 was still taking sustained traffic — from two sites neither governed by
# APPLYMACHINE_EP3_BASE:
#     treasurer/scripts/loop/taey_comment_draft.py:26  THOR1 = "http://10.0.0.8:8000/..."  (literal)
#     apply-machine/taey_compose_driver.py:739         os.environ.get(..., "http://10.0.0.8:8000/v1")
# The compose path is SPLIT as a result: the composer leg follows EP3_BASE while the overseer leg
# defaults to the other node independently.
for d in /home/*/treasurer /home/*/apply-machine /home/*/the-conductor /home/*/taeys-hands; do
  [ -d "$d" ] || continue
  grep -rnoE '"https?://10\.0\.0\.(8|197):[0-9]+[^"]*"' "$d" --include=*.py 2>/dev/null |
    grep -v '/\.git/' | while IFS= read -r hit; do
      match "$hit" || continue
      printf '  %s\n' "$hit"
    done
done
echo "  (grep covers .py under the scanned roots only — a literal in another language or path is"
echo "   still invisible. This narrows the hole; it does not close it.)"

HL "== seats that WATCH the served-weights root (notify even though they send no inference) =="
cat <<'EOF'
  These gate their own work on which checkpoint is served and full-stop if it changes
  without notice. They will not appear in a port or env scan.
    - the `linkedin` seat   : takes a served-weights receipt at cycle open
    - tutor / tutor-codex   : compare runs across checkpoints; a silent swap confounds their results
    - treasurer             : sequences serving changes for the revenue lanes
  Confirm this list against the fleet roster before relying on it; it is the class most easily
  forgotten precisely because it is invisible to a technical scan.
EOF

HL "== WHAT THIS RUN COULD AND COULD NOT RULE OUT =="
# The three blind spots below are all one thing: the observation surface cannot see the consumer,
# and reports that as the consumer not existing. A false negative of that kind is INDISTINGUISHABLE
# FROM A TRUE NEGATIVE — it reads as evidence, so it gets asserted to peers and then hardens when
# someone cites it back. Prose caveats do not stop that; a per-run verdict does. So this section
# states, for THIS run, which shapes were actually excluded and which remain open.

# WRONG SCOPE — this one IS ruled out, because both scopes are scanned above.
echo "  [RULED OUT ] wrong-scope: system AND user scope both enumerated this run."

# DOWN — enumerable from config: a unit can exist, reference the endpoint, and not be running.
downlist=""
for scope in "--user" ""; do
  # shellcheck disable=SC2086
  while read -r u; do
    [ -n "$u" ] || continue
    st=$(systemctl $scope is-active "$u" 2>/dev/null)
    [ "$st" = "active" ] && continue
    env=$(systemctl $scope show "$u" -p Environment --value 2>/dev/null)
    case "$env" in *EP3*|*:8000*) downlist="${downlist} ${u}[${st:-unknown}]" ;; esac
  done < <(systemctl $scope list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}')
done
if [ -n "$downlist" ]; then
  echo "  [OPEN      ] down-consumer: these units reference an ep3 endpoint and are NOT running -"
  printf '                %s\n' $downlist
  echo "                They hold no socket, so nothing above can see them. Treat as consumers."
else
  echo "  [RULED OUT ] down-consumer: no non-running unit references an ep3 endpoint."
fi

# INTERMITTENT — never excludable from a point sample. Sample a few times and say so honestly.
n=0
for _ in 1 2 3; do
  c=$(ss -tn 2>/dev/null | grep -c ':8000' || true); n=$((n + c)); sleep 2
done
echo "  [OPEN      ] intermittent-consumer: sockets sampled 3x over ~6s, ${n} observation(s)."
echo "                A burst driver that connects outside that window is invisible by construction."
echo "                This shape CANNOT be ruled out by observation — only by reading configs + asking."

HL "== LIMITS OF THIS SCAN — read before trusting it =="
cat <<'EOF'
  THREE SHAPES THIS SCAN STRUCTURALLY CANNOT REPORT. Not edge cases — each has bitten:

  1. **A CONSUMER THAT IS DOWN.** A halted or failed unit may not appear, and a stopped process
     holds no socket, so a clean scan during an outage under-reports the true consumer set.
     Proven 2026-07-27 — apply-loop.service is hard-pinned to one node with no failover and was
     INVISIBLE to a scan taken while it was halted; repointing without it would have produced a
     404 on lane restart that reads exactly like the halt being recovered from.

  2. **A CONSUMER THAT IS INTERMITTENT.** Same invisibility, opposite cause: it is healthy but
     simply not connected at the instant you looked, and it connects during your window. The
     LinkedIn step5 driver is this shape — it drives an endpoint directly, in bursts. A stopped
     consumer and an intermittent one are both absent from a point-in-time view, and both bite
     at exactly the wrong moment.

  3. **A CONSUMER IN THE OTHER SYSTEMD SCOPE.** `systemctl` defaults to system scope, so a
     user-scope unit answers `inactive` — or reports no FragmentPath — while it is in fact
     RUNNING and holding the endpoint. Proven 2026-07-27: apply-loop and apply-scorer both
     returned `inactive` to a system-scope query while both were active user units with drop-ins
     resolving the endpoint. This script scans BOTH scopes; an ad-hoc check by hand usually does
     not. Always pass `--user` too, and read the EFFECTIVE merged value
     (`systemctl --user show <u> -p Environment --value`) rather than reasoning about drop-in
     precedence.

  If any consumer is currently down, enumerate from CONFIG (unit files, env files) and from
  memory of what normally runs — never from what answers right now.

  This narrows the guessing; it does NOT prove completeness. Other known blind spots:
    - processes launched ad-hoc (a seat running a worker by hand) appear only if connected RIGHT NOW
    - consumers whose endpoint is a literal in source rather than config
    - env files outside the scanned paths
    - anything on a host this script is not run from
  So: run it, then still ASK the fleet before a bounce. The failure mode this replaces is
  "four people recalled the same incomplete list" — not "we can now skip asking".
EOF

HL "== before you bounce =="
cat <<'EOF'
  1. STOP every PINNED-NO-FAILOVER unit for the window (it cannot redirect; it will just error).
  2. Repoint or pause every REDIRECTABLE consumer at the other Thor (same weights = no behaviour change).
  3. NOTIFY every WEIGHTS-WATCHER seat, and notify them BEFORE the bounce, not after.
  4. Confirm quiesced for the WHOLE window, not idle at one instant.
  5. After: verify with a REAL production request, not a self-probe. Served means production.
EOF
