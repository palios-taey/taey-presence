#!/usr/bin/env bash
# promote_model.sh — put ONE model on BOTH serving nodes under the same alias, and prove it.
#
# WHY THIS EXISTS. On 2026-07-30 the two nodes were found serving DIFFERENT WEIGHTS under the
# SAME alias: node1 `ep3` -> cpt_v7_eps1fix_servable, node2 `ep3` -> module5_merged. Because
# the executive proxy routes to node1 and the delegate proxy to node2, Main Taey and its own
# delegate had been answering
# from different models for days. Nothing detected it: both nodes returned 200, both advertised
# `ep3`, every health check passed. The divergence was invisible because every check compared
# the ALIAS, which was identical, instead of the ROOT, which was not.
#
# The root cause was not the mismatch — it was that promotion did not exist. deploy_thor.sh
# installs the serving stack and substitutes @TAEY_MODEL_PATH@, but nothing ever copied weights
# or pointed both nodes at a new bake. Promotion was a human remembering to move ~52GB and
# hand-edit a systemd unit on each node, with no trigger, no owner, and no check. So a bake
# reached one node and stopped there, and the fleet kept passing its own health checks.
#
# Operator intent this implements, verbatim: "Model bakes. Promoted to both nodes... Plug-n-play.
# No changing anything except swapping out the model. Same name, same everything."
#
# SAFETY. Nodes are promoted ONE AT A TIME, which bounds the blast radius rather than preserving
# availability — each proxy is pinned to a node, so that node's route is down while it reloads. Each node is verified with
# a REAL COMPLETION, not a /v1/models listing — a served root proves which files were opened, and
# a completion proves the weights actually load and generate. If a node fails to come back, the
# script stops before touching the second node, leaving the fleet half-promoted but SERVING, which
# is recoverable; promoting blindly onto both is not.
#
# Usage:
#   promote_model.sh <node1|node2> <model-dir-name>       # promote a bake living on <source-node>
#   promote_model.sh --check           # served-root agreement only; fast, mutates nothing
#   promote_model.sh --check-content   # also compares per-file sha256 manifests; slow, reads every byte
#
# Example:
#   TAEY_NODE1_SSH=user@host1 TAEY_NODE1_MODELS=/srv/models \
#     TAEY_NODE2_SSH=user@host2 TAEY_NODE2_MODELS=/srv/models promote_model.sh node1 my_checkpoint
set -Eeuo pipefail

# Load site config if present. This script and list_ep3_consumers.sh are the ONLY consumers of
# fleet.env — the proxy services load their own environment through systemd and do not read it.
# A fresh install copies fleet.env.example to fleet.env and edits the four required values, and
# these two tools pick them up. An explicit env var still wins, so CI and one-off overrides need
# no file edit.
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _cfg in "${TAEY_FLEET_ENV:-}" "$_here/fleet.env" "$_here/../fleet.env"; do
  if [ -n "$_cfg" ] && [ -f "$_cfg" ]; then
    set -a; . "$_cfg"; set +a
    printf '[promote] loaded site config: %s\n' "$_cfg"
    break
  fi
done

# NODE CONFIG — every operator-specific value is an env var with a documented default, because a
# downloaded Taey plus the public repos must be a working system: a fresh Jetson Thor owner changes
# these and nothing else. Hardcoding an operator's hostnames or home directories here would make the
# public artifact unusable to anyone but its author.
#
#   TAEY_NODE1_SSH / TAEY_NODE2_SSH       user@host for each serving node
#   TAEY_NODE1_HOST / TAEY_NODE2_HOST     host:port used for HTTP probes
#   TAEY_NODE1_MODELS / TAEY_NODE2_MODELS host dir mounted at /models in the serving container
#   TAEY_SERVED_NAME                      space-separated stable aliases promoted together
#   TAEY_PRIMARY_SERVED_NAME              alias used for the generation gate
#   TAEY_SERVE_UNIT                       systemd unit that serves the model
NODE1_SSH="${TAEY_NODE1_SSH:?set TAEY_NODE1_SSH, e.g. taey@node1.local}"
NODE2_SSH="${TAEY_NODE2_SSH:?set TAEY_NODE2_SSH, e.g. taey@node2.local}"
NODE1_HOST="${TAEY_NODE1_HOST:-${NODE1_SSH#*@}}"
NODE2_HOST="${TAEY_NODE2_HOST:-${NODE2_SSH#*@}}"
NODE1_MODELS="${TAEY_NODE1_MODELS:?set TAEY_NODE1_MODELS, the host dir mounted at /models}"
NODE2_MODELS="${TAEY_NODE2_MODELS:?set TAEY_NODE2_MODELS, the host dir mounted at /models}"
SERVE_PORT="${TAEY_SERVE_PORT:-8000}"
# CONSUMERS PINNED TO EACH NODE. Restarting a serving node takes its pinned consumers' backend away
# mid-flight, and the proxies have NO admission gate — there is no way to tell one "stop accepting
# turns". An instantaneous active-turns==0 poll proves nothing, because a new turn can be admitted
# in the microsecond after the poll. So the only honest quiescence is to STOP the consumers for the
# window and restart them after. These must be set explicitly, to "none" if genuinely none, so that
# promoting without having thought about who is pinned is impossible rather than merely unwise.
# NOT required for --check, which mutates nothing: demanding a declaration to run a read-only gate
# would train people to set it carelessly just to get the check to run, which defeats the point of
# making it explicit. Bound below, on the promotion path only.
NODE1_CONSUMERS="${TAEY_NODE1_CONSUMERS:-}"
NODE2_CONSUMERS="${TAEY_NODE2_CONSUMERS:-}"
UNIT="${TAEY_SERVE_UNIT:-taey-ep3.service}"
SERVED_NAME_LIST="${TAEY_SERVED_NAME:?set TAEY_SERVED_NAME to the complete space-separated stable alias set}"
read -r -a SERVED_ALIASES <<< "$SERVED_NAME_LIST"
PRIMARY_ALIAS="${TAEY_PRIMARY_SERVED_NAME:-${SERVED_ALIASES[0]:-}}"
PROMOTION_DROPIN="zzzz-active-model.conf"
LEGACY_PROMOTION_DROPIN="zzz-taey-promoted-model.conf"
# SSH may be one-directional between nodes; push from whichever side has the key. Node-to-node only: relaying through the
# operator workstation drops transfer from ~112MB/s to ~7MB/s because OpenSSH 9+ routes
# remote-to-remote scp through the local host.
LOAD_TIMEOUT="${TAEY_LOAD_TIMEOUT:-900}"

log() { printf '[promote] %s\n' "$*"; }
die() { printf '[promote] FAIL: %s\n' "$*" >&2; exit 1; }

served_root() {  # <host> <alias> -> the model directory the node opened FOR THIS ALIAS
  # Select by IDENTITY, never by position. A node may advertise several models — this fleet's
  # second node serves the alias alongside a LoRA adapter — and /v1/models ordering is not part of
  # any contract, so data[0] can silently be a different model than the one being promoted. This
  # function IS the integrity gate; an index standing in for an identity is exactly the
  # name-versus-artifact confusion the gate exists to catch. Exactly one match is required: zero
  # means the alias is not served, more than one means the payload is ambiguous, and both are
  # failures rather than something to pick a winner from.
  local host="$1" alias="$2"
  curl -s --max-time 10 "http://$host:$SERVE_PORT/v1/models" 2>/dev/null \
    | ALIAS="$alias" python3 -c '
import sys, json, os
alias = os.environ["ALIAS"]
try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(1)
data = payload.get("data")
if not isinstance(data, list):
    sys.exit(1)
hits = [m for m in data if isinstance(m, dict) and m.get("id") == alias]
if len(hits) != 1:
    sys.exit(1)
root = hits[0].get("root")
if not isinstance(root, str) or not root:
    sys.exit(1)
print(root)
' 2>/dev/null || true
}

generates() {  # <host> -> proof the weights load AND produce tokens
  curl -s --max-time 120 "http://$1:$SERVE_PORT/v1/chat/completions" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$PRIMARY_ALIAS\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly one word: ready\"}],\"max_tokens\":10,\"chat_template_kwargs\":{\"enable_thinking\":false}}" 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["choices"][0]["message"]["content"].strip())' 2>/dev/null || true
}

assert_no_identity_conflicts() {  # <ssh> <host> -> no unowned drop-in assigns model identity
  local ssh_t="$1" host="$2" conflicts
  if ! conflicts="$(ssh -o ConnectTimeout=10 "$ssh_t" "
    for f in /etc/systemd/system/$UNIT.d/*.conf; do
      [ -f \"\$f\" ] || continue
      b=\$(basename \"\$f\")
      case \"\$b\" in '$PROMOTION_DROPIN'|'$LEGACY_PROMOTION_DROPIN') continue ;; esac
      grep -Eq '^[[:space:]]*Environment[[:space:]]*=\"?TAEY_(MODEL_PATH|SERVED_NAME)=' \"\$f\" && printf '%s\\n' \"\$b\"
    done
    exit 0
  " 2>/dev/null)"; then
    die "$host: could not inspect systemd model-identity drop-ins"
  fi
  [ -z "$conflicts" ] \
    || die "$host: these unowned sibling drop-ins assign model path or served names: $conflicts"
}

assert_dropin_convention() {  # <ssh> <host> <expected-host-path> -> exact owned identity, no override
  # WHY THIS EXISTS SEPARATELY FROM THE ROOT CHECK. served_root() already proves the promoted
  # WEIGHTS are what the alias opened, so a sibling drop-in that clobbers TAEY_MODEL_PATH is
  # already caught. What nothing caught is a sibling that sets something ELSE. Observed
  # 2026-08-04: node2 carried zzzz-lora-eval.conf injecting TAEY_LORA_PATH, so every restart
  # silently re-attached an eval adapter to the promoted model — correct root, real generation,
  # green on every existing gate, and an unintended adapter live in production for two days.
  # A promotion is exactly when that should surface, because a promotion is when the operator
  # believes they know what the node is serving.
  #
  # Checked on the EFFECTIVE merged env, not only the files: an adapter injected from the unit
  # itself or an env file would satisfy a file census and still reach the serve.
  local ssh_t="$1" host="$2" expected_path="$3" effective_env identity_file expected_file lora siblings

  if ! effective_env="$(ssh -o ConnectTimeout=10 "$ssh_t" \
            "systemctl show $UNIT -p Environment --value" 2>/dev/null)"; then
    die "$host: could not inspect the effective systemd environment after promotion"
  fi
  lora="$(printf '%s\n' "$effective_env" | tr ' ' '\n' | grep '^TAEY_LORA_PATH=' || true)"
  [ -z "$lora" ] \
    || die "$host: promoted serve resolves ${lora% *} — an adapter is attached to the model just
       promoted. Remove the drop-in or env entry that sets it, then re-run. (Adapters are attached
       deliberately for an eval; carrying one THROUGH a promotion is never deliberate.)"

  if ! identity_file="$(ssh -o ConnectTimeout=10 "$ssh_t" \
      "cat /etc/systemd/system/$UNIT.d/$PROMOTION_DROPIN" 2>/dev/null)"; then
    die "$host: could not read the promotion-owned identity drop-in after restart"
  fi
  expected_file="$(printf '%s\n' \
    '# Written by promote_model.sh. This is the sole model-identity drop-in.' \
    '[Service]' \
    "Environment=TAEY_MODEL_PATH=$expected_path" \
    "Environment=\"TAEY_SERVED_NAME=$SERVED_NAME_LIST\"")"
  [ "$identity_file" = "$expected_file" ] \
    || die "$host: $PROMOTION_DROPIN does not exactly match the promoted path and stable aliases"

  assert_no_identity_conflicts "$ssh_t" "$host"

  siblings="$(ssh -o ConnectTimeout=10 "$ssh_t" \
                "ls /etc/systemd/system/$UNIT.d/ 2>/dev/null | grep -v '^${PROMOTION_DROPIN}$'" \
              2>/dev/null || true)"
  if [ -n "$siblings" ]; then
    # Not fatal on its own — the two hard properties above are already proven, and a legitimate
    # operator drop-in must not block a promotion. Loud, named, and attributable is the point.
    log "WARNING: $host carries drop-ins besides the promotion drop-in. Effective model path and"
    log "         adapter state are verified above, so these are not currently overriding either —"
    log "         but each one wins over the promoted config if it ever sets the same key:"
    printf '%s\n' "$siblings" | while read -r s; do [ -n "$s" ] && log "           $s"; done
  else
    log "$host drop-ins: exactly $PROMOTION_DROPIN, no siblings"
  fi
}

manifest() {  # <ssh> <dir> -> one digest over every file's name+content, or NOTHING on any failure
  # pipefail on the REMOTE side is load-bearing: without it a failing find, an unreadable file, or a
  # dying sha256sum is masked by the trailing `cut`, which exits 0 and prints a perfectly plausible
  # digest — of a PARTIAL file set. That would let --check-content certify incomplete content, which
  # is a worse outcome than no check at all, because it carries the authority of a verification.
  # Zero files is also a failure: an empty directory otherwise hashes to a stable, confident value.
  ssh -o ConnectTimeout=30 "$1" "
    set -o pipefail
    cd '$2' || exit 1
    n=\$(find . -type f | wc -l) || exit 1
    [ \"\$n\" -gt 0 ] || exit 1
    find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1
  " 2>/dev/null
}

drift_check() {  # $1 = "deep" to additionally compare per-file content manifests
  local deep="${1:-}" alias r1 r2 resolved_root=""
  assert_no_identity_conflicts "$NODE1_SSH" "$NODE1_HOST"
  assert_no_identity_conflicts "$NODE2_SSH" "$NODE2_HOST"
  for alias in "${SERVED_ALIASES[@]}"; do
    r1="$(served_root "$NODE1_HOST" "$alias")"; r2="$(served_root "$NODE2_HOST" "$alias")"
    printf '  node1 %s -> %s\n  node2 %s -> %s\n' "$alias" "${r1:-UNRESOLVED}" "$alias" "${r2:-UNRESOLVED}"
    [ -n "$r1" ] && [ -n "$r2" ] || { echo "  INCONCLUSIVE: alias '$alias' did not resolve to exactly one model with a root on both nodes"; return 2; }
    if [ "$r1" != "$r2" ]; then
      echo "  DRIFT: the nodes serve DIFFERENT model directories under alias '$alias'"; return 1
    fi
    if [ -n "$resolved_root" ] && [ "$r1" != "$resolved_root" ]; then
      echo "  DRIFT: stable aliases resolve to different model roots ('$resolved_root' versus '$r1')"; return 1
    fi
    resolved_root="$r1"
  done
  # Say exactly what was checked. Equal roots mean both nodes OPENED THE SAME PATH — they do not
  # mean the files under that path are identical, and a corrupted or partially-synced tree serves
  # happily from the right path. Claiming "same weights" from a string comparison would be the same
  # class of overclaim this script exists to catch, so the cheap check reports its own limit.
  if [ "$deep" != "deep" ]; then
    echo "  OK (served-root agreement): both nodes serve '$resolved_root' for aliases '${SERVED_ALIASES[*]}'."
    echo "     CONTENT NOT VERIFIED — equal roots do not prove equal files. Use --check-content to compare per-file sha256 manifests."
    return 0
  fi
  echo "  comparing per-file content manifests (reads every byte on both nodes; slow)"
  local m1 m2
  # `|| true` so a nonzero ssh cannot terminate the script under set -e before the INCONCLUSIVE
  # branch below is reached. An unreachable node must REPORT "cannot tell", not vanish mid-check.
  m1="$(manifest "$NODE1_SSH" "$NODE1_MODELS/${resolved_root##*/}" || true)"
  m2="$(manifest "$NODE2_SSH" "$NODE2_MODELS/${resolved_root##*/}" || true)"
  [ -n "$m1" ] && [ -n "$m2" ] || { echo "  INCONCLUSIVE: could not compute a manifest on one or both nodes"; return 2; }
  [ "$m1" = "$m2" ] || { echo "  CONTENT DRIFT: same root '$resolved_root' but DIFFERENT file contents (node1=$m1 node2=$m2)"; return 1; }
  echo "  OK (content verified): identical root AND identical per-file manifest $m1"
  return 0
}

consumers_for() { case "$1" in node1) printf '%s' "$NODE1_CONSUMERS" ;; node2) printf '%s' "$NODE2_CONSUMERS" ;; esac; }
declare -A QUIESCED_CONSUMERS=()

quiesce() {  # <node-label> — stop every pinned consumer and PROVE it stopped
  local list; list="$(consumers_for "$1")"
  [ "$list" = "none" ] && { log "$1: no pinned consumers declared"; return 0; }
  local u state
  for u in $list; do
    state="$(systemctl --user is-active "$u" 2>/dev/null || true)"
    case "$state" in
      active|activating|reloading) ;;
      deactivating)
        systemctl --user stop "$u" || die "$1: could not finish stopping $u"
        log "$1: $u was already deactivating; leaving it stopped after the window"
        continue
        ;;
      inactive|failed)
        log "$1: $u was $state before the window; leaving it unchanged"
        continue
        ;;
      *) die "$1: could not determine the pre-window state of declared consumer $u (got '${state:-no state}')" ;;
    esac
    systemctl --user stop "$u" || die "$1: could not stop $u — refusing to restart a node whose consumer is still admitting turns"
    [ "$(systemctl --user is-active "$u")" = "active" ] \
      && die "$1: $u still active after stop — refusing to proceed"
    QUIESCED_CONSUMERS["$1:$u"]=1
    log "$1: quiesced $u"
  done
}

restore() {  # <node-label> — bring the pinned consumers back and PROVE they came back
  local list; list="$(consumers_for "$1")"
  [ "$list" = "none" ] && return 0
  local u key
  for u in $list; do
    key="$1:$u"
    [ "${QUIESCED_CONSUMERS[$key]:-}" = "1" ] || continue
    systemctl --user start "$u" || die "$1: $u FAILED TO RESTART after promotion — the node is serving but its consumer is down"
    [ "$(systemctl --user is-active "$u")" = "active" ] \
      || die "$1: $u did not become active after promotion"
    QUIESCED_CONSUMERS["$key"]=""
    log "$1: restored $u"
  done
}

promote_node() {  # <ssh> <models-dir> <host> <model-name> <node-label>
  local ssh_t="$1" models="$2" host="$3" model="$4" label="$5"
  log "promoting $host -> $model (maintenance window opens for ${label}'s consumers)"
  assert_no_identity_conflicts "$ssh_t" "$host"
  quiesce "$label"
  ssh -o ConnectTimeout=10 "$ssh_t" "test -d '$models/$model'" \
    || die "$host: $models/$model missing — copy the weights before promoting"
  # A DEDICATED ACTIVE-MODEL DROP-IN — not the generic override.conf.
  # Two reasons. Ownership: override.conf is the name a human reaches for, so writing it means
  # promotion silently clobbers whatever an operator put there. The previous promotion filename
  # started with only three z's and lost to the fleet's existing zzzz-active-model.conf, so every
  # run restarted the old weights and then timed out. Promotion now OWNS that active-model file,
  # writes model path and stable aliases together, and removes its obsolete losing predecessor.
  ssh -o ConnectTimeout=10 "$ssh_t" "
    set -e
    _dir=/etc/systemd/system/$UNIT.d
    _tmp=\"\$_dir/.${PROMOTION_DROPIN}.\$\$.tmp\"
    sudo mkdir -p \"\$_dir\" &&
    printf '%s\n' '# Written by promote_model.sh. This is the sole model-identity drop-in.' '[Service]' 'Environment=TAEY_MODEL_PATH=$models/$model' 'Environment=\"TAEY_SERVED_NAME=$SERVED_NAME_LIST\"' \
      | sudo tee \"\$_tmp\" >/dev/null &&
    sudo chmod 0644 \"\$_tmp\" &&
    sudo mv -f \"\$_tmp\" \"\$_dir/$PROMOTION_DROPIN\" &&
    sudo rm -f \"\$_dir/$LEGACY_PROMOTION_DROPIN\" &&
    sudo systemctl daemon-reload && sudo systemctl restart $UNIT" \
    || die "$host: restart failed"

  local waited=0 alias root ready=0 roots_summary=""
  while [ "$waited" -lt "$LOAD_TIMEOUT" ]; do
    ready=1
    roots_summary=""
    for alias in "${SERVED_ALIASES[@]}"; do
      root="$(served_root "$host" "$alias")"
      roots_summary="${roots_summary}${alias}=${root:-UNRESOLVED} "
      [ "$root" = "/models/$model" ] || ready=0
    done
    [ "$ready" -eq 1 ] && break
    sleep 15; waited=$((waited + 15))
  done
  [ "$ready" -eq 1 ] || die "$host: did not serve /models/$model under every stable alias within ${LOAD_TIMEOUT}s (got '$roots_summary')"
  local out; out="$(generates "$host")"
  [ -n "$out" ] || die "$host: serves the right root but does NOT generate — weights are loaded but broken"
  restore "$label"
  # AFTER restore, deliberately. This asserts config hygiene, not whether the promotion worked —
  # the weights are already proven serving AND generating above. A die() here before restore would
  # leave every pinned consumer stopped over a drop-in problem, which is a worse outage than the
  # one it reports.
  assert_dropin_convention "$ssh_t" "$host" "$models/$model"
  log "$host OK: root=/models/$model, aliases=${SERVED_ALIASES[*]}, completion=$(printf %q "$out"), consumers restored"
}

# INPUT VALIDATION BEFORE ANY SSH OR DESTRUCTIVE RSYNC.
# MODEL is interpolated into remote shell strings and into `rsync -a --delete` paths. A value
# containing a slash, "..", a quote, whitespace or any shell metacharacter could escape the intended
# model directory or break quoting — and with --delete that is destructive on a serving node. So it
# must be a SINGLE path component from a strict allowlist, checked before anything leaves this host.
valid_component() {  # single path component: starts alphanumeric, then alnum . _ - only
  case "$1" in
    ""|*/*|*..*) return 1 ;;
    *) printf '%s' "$1" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._-]*$' ;;
  esac
}
valid_abs_dir() { case "$1" in /*) case "$1" in *..*|*[\'\"\$\`\;\&\|\<\>]*) return 1 ;; *) return 0 ;; esac ;; *) return 1 ;; esac; }

valid_abs_dir "$NODE1_MODELS" || die "TAEY_NODE1_MODELS must be an absolute path with no '..' or shell metacharacters"
valid_abs_dir "$NODE2_MODELS" || die "TAEY_NODE2_MODELS must be an absolute path with no '..' or shell metacharacters"
valid_component "$UNIT" || die "TAEY_SERVE_UNIT must be a single safe component (got: $UNIT)"
[ "${#SERVED_ALIASES[@]}" -gt 0 ] || die "TAEY_SERVED_NAME must name at least one stable alias"
for alias in "${SERVED_ALIASES[@]}"; do
  valid_component "$alias" || die "each TAEY_SERVED_NAME alias must be a single safe component (got: $alias)"
done
SERVED_NAME_LIST="${SERVED_ALIASES[*]}"
valid_component "$PRIMARY_ALIAS" || die "TAEY_PRIMARY_SERVED_NAME must be a single safe component (got: $PRIMARY_ALIAS)"
case " ${SERVED_ALIASES[*]} " in
  *" $PRIMARY_ALIAS "*) ;;
  *) die "TAEY_PRIMARY_SERVED_NAME '$PRIMARY_ALIAS' is not present in TAEY_SERVED_NAME '$SERVED_NAME_LIST'" ;;
esac

case "${1:-}" in
  --check)         drift_check;      exit $? ;;
  --check-content) drift_check deep; exit $? ;;
esac
[ $# -eq 2 ] || die "usage: promote_model.sh <node1|node2> <model-dir-name>  |  promote_model.sh --check"
SRC="$1"; MODEL="$2"
# Promotion restarts serving nodes, so from here the consumer declaration is mandatory.
[ -n "$NODE1_CONSUMERS" ] || die "set TAEY_NODE1_CONSUMERS to the systemd --user units pinned to node1, or the literal: none"
[ -n "$NODE2_CONSUMERS" ] || die "set TAEY_NODE2_CONSUMERS to the systemd --user units pinned to node2, or the literal: none"

# A NON-EMPTY DECLARATION IS NOT A COMPLETE ONE. Checking only that the operator typed something
# lets an undeclared consumer keep admitting work while its node restarts — observed on this fleet,
# where a revenue-lane unit was pinned to node2 alongside the delegate proxy and appeared in no
# example. A crash-looping unit counts too: `activating` is not `active`, but it will admit work the
# moment it comes up, so the scan covers active, activating and reloading rather than active alone.
# example. So cross-check the declaration against what is ACTUALLY pointed at each node, and refuse
# on any consumer that is running but undeclared.
undeclared_for() {  # <node-host> <declared-list> -> units targeting that host but not declared
  local host="$1" declared="$2" u env_target
  for u in $(systemctl --user list-units --state=active,activating,reloading --no-legend --plain 2>/dev/null | awk '{print $1}' | grep '\.service$'); do
    env_target="$(systemctl --user show "$u" -p Environment --value 2>/dev/null | tr ' ' '\n' | grep -oE 'https?://[^/ ]+' | grep -F "$host" | head -1)"
    [ -n "$env_target" ] || continue
    case " $declared " in *" $u "*) ;; *) printf '%s ' "$u" ;; esac
  done
}
for pair in "node1:$NODE1_HOST:$NODE1_CONSUMERS" "node2:$NODE2_HOST:$NODE2_CONSUMERS"; do
  lbl="${pair%%:*}"; rest="${pair#*:}"; host="${rest%%:*}"; decl="${rest#*:}"
  [ "$decl" = "none" ] && decl=""
  missing="$(undeclared_for "$host" "$decl")"
  [ -z "$missing" ] || die "$lbl: these ACTIVE units target $host but are not in TAEY_NODE${lbl#node}_CONSUMERS: $missing
Add them (they must be stopped for the maintenance window) or stop them first. Refusing to restart a node while an undeclared consumer can admit work."
done
log "consumer declarations cover every active unit targeting each node"
valid_component "$MODEL" \
  || die "model must be a single directory name matching [A-Za-z0-9][A-Za-z0-9._-]* — got: $MODEL"

case "$SRC" in
  node1) src_ssh="$NODE1_SSH"; src_dir="$NODE1_MODELS"; dst_ssh="$NODE2_SSH"; dst_dir="$NODE2_MODELS" ;;
  node2) src_ssh="$NODE2_SSH"; src_dir="$NODE2_MODELS"; dst_ssh="$NODE1_SSH"; dst_dir="$NODE1_MODELS" ;;
  *) die "source must be node1 or node2" ;;
esac
ssh -o ConnectTimeout=10 "$src_ssh" "test -d '$src_dir/$MODEL'" || die "$SRC: $src_dir/$MODEL not found"

# WHICH NODE DRIVES THE TRANSFER IS EXPLICIT AND VERIFIED, never assumed. An earlier version ran
# rsync from node1 in BOTH directions while its comment claimed it drove "from whichever side can
# authenticate" — the code did not do what the sentence said. SSH between serving nodes is commonly
# one-directional, so the driver is stated via TAEY_SYNC_DRIVER and then PROVEN to reach its peer
# before any --delete sync runs. Node-to-node only: relaying through the operator workstation drops
# throughput roughly 16x, because OpenSSH 9+ routes remote-to-remote transfers through the local host.
SYNC_DRIVER="${TAEY_SYNC_DRIVER:-node1}"
case "$SYNC_DRIVER" in
  node1) drv_ssh="$NODE1_SSH" ;;
  node2) drv_ssh="$NODE2_SSH" ;;
  *) die "TAEY_SYNC_DRIVER must be node1 or node2 (got: $SYNC_DRIVER)" ;;
esac
# The driver must be one END of this transfer; a third party would relay and defeat node-to-node.
[ "$drv_ssh" = "$src_ssh" ] || [ "$drv_ssh" = "$dst_ssh" ] \
  || die "TAEY_SYNC_DRIVER=$SYNC_DRIVER is neither the source nor the destination of this promotion"
peer_ssh="$([ "$drv_ssh" = "$src_ssh" ] && printf '%s' "$dst_ssh" || printf '%s' "$src_ssh")"
log "sync driver: $SYNC_DRIVER — proving it can reach $peer_ssh before any --delete"
ssh -o ConnectTimeout=10 "$drv_ssh" "ssh -o BatchMode=yes -o ConnectTimeout=10 '$peer_ssh' true" 2>/dev/null \
  || die "$SYNC_DRIVER cannot SSH to $peer_ssh non-interactively. Set TAEY_SYNC_DRIVER to the node that CAN, or install a key. Refusing to run a --delete sync from a node that cannot reach its target."

# Sync UNCONDITIONALLY. An earlier version copied only when the destination directory was ABSENT,
# which skipped the dangerous case: a directory left behind by an interrupted transfer is PRESENT
# but PARTIAL, so no copy ran and the mismatch merely aborted, with no path back to a good state.
# rsync -a --delete makes the destination match the source exactly, which is what repairs a partial
# tree — and is also why the input validation above is not optional.
log "syncing $MODEL: $SRC -> peer, driven by $SYNC_DRIVER"
if [ "$drv_ssh" = "$src_ssh" ]; then
  ssh -o ConnectTimeout=10 "$drv_ssh" \
    "rsync -a --delete '$src_dir/$MODEL/' '$dst_ssh:$dst_dir/$MODEL/'" || die "sync $SRC -> peer failed"
else
  ssh -o ConnectTimeout=10 "$drv_ssh" \
    "rsync -a --delete '$src_ssh:$src_dir/$MODEL/' '$dst_dir/$MODEL/'" || die "sync $SRC -> peer failed"
fi

# VERIFY BY PER-FILE CHECKSUM MANIFEST, not by total size. A byte-total comparison is not a
# byte-equality check: two trees of identical total size with different contents pass it, and so
# does a tree whose files were shuffled between names. The failure that matters here is a model
# that LOADS and SERVES THE RIGHT ROOT while holding wrong or partial weights — it answers
# healthily and is indistinguishable from a correct node, which is the exact class of silent fork
# this script exists to prevent. So compare a sorted name+sha256 manifest of every file.
log "verifying per-file checksum manifests (this reads every byte on both nodes)"
# In PROMOTION mode an uncomputable manifest is fatal, not merely inconclusive: the next step
# restarts a serving node, and doing that without having verified the copy is the failure this
# whole script exists to prevent.
m1="$(manifest "$NODE1_SSH" "$NODE1_MODELS/$MODEL" || true)"
m2="$(manifest "$NODE2_SSH" "$NODE2_MODELS/$MODEL" || true)"
[ -n "$m1" ] && [ -n "$m2" ] || die "could not compute a manifest on one or both nodes — refusing to promote unverified content"
[ "$m1" = "$m2" ] || die "MANIFEST MISMATCH — node1=$m1 node2=$m2; the copies differ in content, not just size"
log "manifests match on both nodes: $m1"

# One node at a time, each inside its own maintenance window: its pinned consumers are STOPPED
# before the node restarts and STARTED again only after the node proves it serves and generates.
# This bounds the blast radius — a node that fails to come back stops the run
# before the second is touched — but it is NOT a general availability guarantee: the executive and
# delegate proxies are each PINNED to one node, so restarting that node takes its route down for
# the reload regardless of the other node being healthy. Sequencing limits damage; it does not keep
# both routes serving.
promote_node "$NODE2_SSH" "$NODE2_MODELS" "$NODE2_HOST" "$MODEL" node2
promote_node "$NODE1_SSH" "$NODE1_MODELS" "$NODE1_HOST" "$MODEL" node1

log "final drift check:"
drift_check || die "promoted both nodes but they still disagree — investigate before serving traffic"
log "PROMOTED: $MODEL is live on both nodes as '${SERVED_ALIASES[*]}'"
