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
