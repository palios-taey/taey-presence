#!/usr/bin/env bash
# deploy_thor.sh — install the canonical serving stack from THIS REPO onto a Thor, and
# verify afterwards that what runs there is what is committed here.
#
# WHY THIS EXISTS. taey-ep3.service has always documented that its @TAEY_ROOT@ /
# @TAEY_MODEL_PATH@ placeholders are "substituted at install time by serving/deploy_thor.sh"
# — and that script did not exist. With no deploy path, the stack was placed on the nodes by
# hand, which produced exactly the drift you would predict:
#
#   Observed 2026-07-27. Both Thors ran ExecStart=/usr/bin/bash $HOME/vllm_serve.sh — a loose
#   copy in the home directory, not a repo-managed path. Thor1's copy matched this repo
#   byte-for-byte (md5 910cd759). Thor2's did NOT (md5 3228ac31): it was 53 lines behind,
#   missing the quantization and speculative-decoding blocks. Default-path behaviour was
#   identical, because every added argument is empty when its variable is unset — so nothing
#   looked wrong. The real cost was latent: setting TAEY_QUANTIZATION or
#   TAEY_SPECULATIVE_CONFIG on Thor2 would have been SILENTLY IGNORED, and the operator would
#   have gone looking for the fault in vLLM.
#
#   And bin/gpu-cleanup.sh — which ExecStartPre calls WITHOUT a `-` prefix, so a missing file
#   hard-fails the start — existed only on the nodes and had never been committed at all. A
#   cold clone of this repo could not stand up a Thor.
#
# The fix is structural, not a reminder to be careful: deploy FROM the repo to TAEY_ROOT, point
# the unit at repo-managed paths, and make drift a command anyone can run.
#
# TWO SAFETY PROPERTIES, both deliberate:
#
#   1. A DEPLOY NEVER SILENTLY CHANGES WHICH MODEL IS SERVED. TAEY_MODEL_PATH is read from the
#      unit already installed on the node and preserved. Changing the served model is a
#      decision with consumers attached, so it requires --model-path, said out loud.
#
#   2. A DEPLOY NEVER RESTARTS PRODUCTION ON ITS OWN. Files are installed and the unit is
#      reloaded; the running service keeps serving from the copy it already exec'd. The new
#      files take effect on the NEXT restart, which you schedule after running
#      serving/list_ep3_consumers.sh and notifying the consumers. Pass --restart only when you
#      have done that.
#
# Usage:
#   ./deploy_thor.sh --check --authority-id host-authority \
#     <user@host> [taey-root]                           # verify only, mutates nothing
#   ./deploy_thor.sh --authority-id host-authority \
#     <user@host> [taey-root]                           # install, no restart
#   ./deploy_thor.sh --restart --model-path /path --served-name id \
#     --authority-id host-authority <user@host> [root]
#
# Exit: 0 = in sync / deployed. 1 = drift found (--check) or deploy failed.
set -euo pipefail

CHECK=0; RESTART=0; MODEL_PATH=""; SERVED_NAME=""; AUTHORITY_ID=""; KEEP_NAME=0; NAME_EXPLICIT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check)       CHECK=1; shift ;;
    --restart)     RESTART=1; shift ;;
    --model-path)  MODEL_PATH="${2:?--model-path needs a value}"; shift 2 ;;
    --served-name) SERVED_NAME="${2:?--served-name needs a value}"; NAME_EXPLICIT=1; shift 2 ;;
    --authority-id) AUTHORITY_ID="${2:?--authority-id needs a value}"; shift 2 ;;
    --keep-served-name) KEEP_NAME=1; shift ;;
    -h|--help)     sed -n '2,40p' "$0"; exit 0 ;;
    *)             break ;;
  esac
done

TARGET="${1:?usage: deploy_thor.sh [--check] [--restart] [--model-path P] <user@host> [taey-root]}"
ROOT="${2:-}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="${REPO}/serving/systemd/taey-ep3.service"
ATTESTOR_UNIT_SRC="${REPO}/serving/systemd/taey-model-identity-attestor.service"

[ -r "$UNIT_SRC" ] || { echo "FATAL: missing $UNIT_SRC" >&2; exit 1; }
[ -r "$ATTESTOR_UNIT_SRC" ] || { echo "FATAL: missing $ATTESTOR_UNIT_SRC" >&2; exit 1; }

# Derive TAEY_ROOT from the node's own installed unit when not given, so we adopt the layout
# already in use rather than imposing a new one and leaving two copies behind.
if [ -z "$ROOT" ]; then
  ROOT=$(ssh "$TARGET" "systemctl cat taey-ep3 2>/dev/null | sed -n 's|^ExecStartPre=\(/.*\)/bin/gpu-cleanup.sh.*|\1|p' | head -1" || true)
  if [ -z "$ROOT" ]; then
    REMOTE_HOME=$(ssh "$TARGET" 'printf "%s" "$HOME"')
    [ -n "$REMOTE_HOME" ] || { echo "FATAL: target home directory is unavailable" >&2; exit 1; }
    ROOT="${REMOTE_HOME}/palios-taey"
  fi
fi
case "$ROOT" in
  /*) ;;
  *) echo "FATAL: taey-root must be an absolute target path" >&2; exit 1 ;;
esac
echo "[deploy] target=${TARGET} root=${ROOT} repo=$(cd "$REPO" && git rev-parse --short HEAD 2>/dev/null || echo unknown)"

LIVE_MODEL_PATH=$(ssh "$TARGET" "systemctl show taey-ep3 -p Environment --value 2>/dev/null | xargs -n1 | sed -n 's/^TAEY_MODEL_PATH=//p' | head -1" || true)
LIVE_SERVED_NAME=$(ssh "$TARGET" "systemctl show taey-ep3 -p Environment --value 2>/dev/null | xargs -n1 | sed -n 's/^TAEY_SERVED_NAME=//p' | head -1" || true)
LIVE_AUTHORITY_ID=$(ssh "$TARGET" "sudo sed -n 's/^TAEY_MODEL_IDENTITY_AUTHORITY_ID=//p' /etc/taey/model-identity-attestor.env 2>/dev/null | head -1" || true)
if [ -z "$MODEL_PATH" ]; then
  MODEL_PATH="$LIVE_MODEL_PATH"
  [ -n "$MODEL_PATH" ] && echo "[deploy] preserving served model: ${MODEL_PATH}"
fi
if [ -z "$SERVED_NAME" ]; then
  SERVED_NAME="$LIVE_SERVED_NAME"
  [ -n "$SERVED_NAME" ] && echo "[deploy] preserving served id: ${SERVED_NAME}"
fi
if [ -z "$AUTHORITY_ID" ]; then
  AUTHORITY_ID="$LIVE_AUTHORITY_ID"
  [ -n "$AUTHORITY_ID" ] && echo "[deploy] preserving model identity authority: ${AUTHORITY_ID}"
fi
[ -n "$SERVED_NAME" ] || { echo "FATAL: no served name exists on the node. Pass --served-name." >&2; exit 1; }
[ -n "$AUTHORITY_ID" ] || { echo "FATAL: no model identity authority exists on the node. Pass --authority-id." >&2; exit 1; }
[[ "$AUTHORITY_ID" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]] \
  || { echo "FATAL: model identity authority is invalid" >&2; exit 1; }
if [ -n "$SERVED_NAME" ]; then
  [[ "$SERVED_NAME" =~ ^[A-Za-z0-9._-]+([[:space:]][A-Za-z0-9._-]+)*$ ]] \
    || { echo "FATAL: served names contain unsupported characters" >&2; exit 1; }
  echo "[deploy] served id: ${SERVED_NAME}"
fi
TRUST_FILE="serving/trust/${AUTHORITY_ID}.pub.pem"
FILES=(serving/vllm_serve.sh serving/model_identity_attestor.py serving/model_identity_status.py serving/seal_model_artifact.py "$TRUST_FILE" bin/gpu-cleanup.sh)
for f in "${FILES[@]}"; do
  [ -r "${REPO}/${f}" ] || { echo "FATAL: missing ${REPO}/${f} — the repo cannot deploy what it does not carry" >&2; exit 1; }
done

EP3_RENDERED=$(mktemp)
ATTESTOR_RENDERED=$(mktemp)
cleanup_rendered_units() {
  rm -f "$EP3_RENDERED" "$ATTESTOR_RENDERED"
}
trap cleanup_rendered_units EXIT

render_unit() {
  local source="$1"
  local destination="$2"
  sed -e "s|@TAEY_ROOT@|${ROOT}|g" -e "s|@TAEY_MODEL_PATH@|${MODEL_PATH}|g" "$source" > "$destination"
  if [ -n "$SERVED_NAME" ]; then
    sed -i -e "s|^Environment=.*TAEY_SERVED_NAME=.*|Environment=\"TAEY_SERVED_NAME=${SERVED_NAME}\"|" "$destination"
    grep -Fq "Environment=\"TAEY_SERVED_NAME=${SERVED_NAME}\"" "$destination" \
      || { echo "FATAL: served-name substitution did not land" >&2; exit 1; }
  fi
  if grep -q '@TAEY_' "$destination"; then
    echo "FATAL: unsubstituted placeholder remains in a unit" >&2
    exit 1
  fi
}

if [ -n "$MODEL_PATH" ]; then
  render_unit "$UNIT_SRC" "$EP3_RENDERED"
  render_unit "$ATTESTOR_UNIT_SRC" "$ATTESTOR_RENDERED"
fi

# ---------- drift report: compare what the node RUNS against what the repo HOLDS ----------
# Compares the path systemd actually exec's, not the path we assume it exec's. Those differed
# on both nodes, and assuming would have reported "in sync" over a real mismatch.
drift=0
live_exec=$(ssh "$TARGET" "systemctl cat taey-ep3 2>/dev/null | sed -n 's|^ExecStart=.*bash ||p' | head -1" || true)
if [ -n "$live_exec" ]; then
  want="${ROOT}/serving/vllm_serve.sh"
  if [ "$live_exec" != "$want" ]; then
    echo "  DRIFT unit ExecStart -> ${live_exec}"
    echo "        repo-managed    -> ${want}"
    drift=1
  fi
  live_md5=$(ssh "$TARGET" "md5sum '${live_exec}' 2>/dev/null | cut -d' ' -f1" || true)
  repo_md5=$(md5sum "${REPO}/serving/vllm_serve.sh" | cut -d' ' -f1)
  if [ -n "$live_md5" ] && [ "$live_md5" != "$repo_md5" ]; then
    echo "  DRIFT executed script content differs from repo (live ${live_md5:0:8} vs repo ${repo_md5:0:8})"
    drift=1
  fi
else
  echo "  NOTE  no taey-ep3 unit installed on this node yet"
  drift=1
fi
for f in "${FILES[@]}"; do
  lm=$(ssh "$TARGET" "md5sum '${ROOT}/${f}' 2>/dev/null | cut -d' ' -f1" || true)
  rm_=$(md5sum "${REPO}/${f}" | cut -d' ' -f1)
  if [ -z "$lm" ]; then echo "  DRIFT ${f} ABSENT at ${ROOT}/${f}"; drift=1
  elif [ "$lm" != "$rm_" ]; then echo "  DRIFT ${f} differs (live ${lm:0:8} vs repo ${rm_:0:8})"; drift=1; fi
done
if [ -n "$MODEL_PATH" ]; then
  compare_unit() {
    local name="$1"
    local rendered="$2"
    live_sha=$(ssh "$TARGET" "sha256sum '/etc/systemd/system/${name}' 2>/dev/null | cut -d' ' -f1" || true)
    expected_sha=$(sha256sum "$rendered" | cut -d' ' -f1)
    if [ -z "$live_sha" ]; then
      echo "  DRIFT ${name} ABSENT"
      drift=1
    elif [ "$live_sha" != "$expected_sha" ]; then
      echo "  DRIFT ${name} differs (live ${live_sha:0:8} vs repo ${expected_sha:0:8})"
      drift=1
    fi
  }
  compare_unit taey-ep3.service "$EP3_RENDERED"
  compare_unit taey-model-identity-attestor.service "$ATTESTOR_RENDERED"
fi
# Word this precisely. This compares the INSTALLED unit and the files on disk against the repo.
# It does NOT prove the RUNNING process was started from them — a process that exec'd an older
# copy keeps running it until restart. Claiming "the node runs this" would be true of the config
# and false of the live process, which is the exact gap this whole script exists to close.
[ "$drift" -eq 0 ] && echo "  IN SYNC — installed unit + files match this repo (the running process picks them up at next start)"

if [ "$CHECK" -eq 1 ]; then
  exit "$drift"
fi

# ---------- install ----------
[ -n "$MODEL_PATH" ] || { echo "FATAL: no TAEY_MODEL_PATH on the node and none given. Pass --model-path." >&2; exit 1; }

# ---------- the alias gate: changing WEIGHTS forces a decision about the served NAME ----------
# Clients address a served id, not a path, and vLLM will happily serve new weights under an old
# id. That combination is the worst failure shape available here: a caller holding a stale URL +
# model=ep3 gets HTTP 200 and DIFFERENT WEIGHTS, silently. It does not 404, so nothing tells the
# caller or the operator. Observed 2026-07-27 — Thor1 advertised id 'ep3' with
# root /models/cpt_refresh_v3_servable while Thor2 advertised id 'ep3' with root /models/ep3-hf.
# Same name, two different models, no signal. Found by the linkedin seat, not by me.
#
# So: if this deploy changes which artifact is served, the served id must be decided in the same
# breath. Either give it a new name (stale callers then fail LOUD with a 404, which is the whole
# point) or say --keep-served-name to assert that every caller of that id SHOULD move to the new
# weights. Both are legitimate; silently inheriting the old name is not.
if [ -n "$MODEL_PATH" ]; then
  # Test NAME_EXPLICIT, not emptiness. The preservation step above fills SERVED_NAME from the
  # node, so an emptiness test here would ALWAYS be false and this gate would never fire again —
  # the preservation fix silently disabled the guard it sits next to. Caught by a pre-window
  # precondition check, not by the edit that caused it.
  if [ -n "$LIVE_MODEL_PATH" ] && [ "$LIVE_MODEL_PATH" != "$MODEL_PATH" ] && [ "$NAME_EXPLICIT" -eq 0 ] && [ "$KEEP_NAME" -eq 0 ]; then
    cat >&2 <<EOF
FATAL: this deploy changes the served ARTIFACT but not the served NAME.
         artifact: ${LIVE_MODEL_PATH}
               ->  ${MODEL_PATH}
         served id stays: '${LIVE_SERVED_NAME:-ep3}'
  A client addressing '${LIVE_SERVED_NAME:-ep3}' would then receive DIFFERENT WEIGHTS with HTTP 200 and no
  indication anything changed. Decide the name explicitly:
    --served-name <new-id>   stale callers get a clean 404 — use this while a node serves a
                             CANDIDATE that differs from its peers
    --keep-served-name       you assert every caller of '${LIVE_SERVED_NAME:-ep3}' should move to the new
                             weights — use this for a fleet-wide promotion
EOF
    exit 1
  fi
fi
ssh "$TARGET" "mkdir -p '${ROOT}/serving/trust' '${ROOT}/bin'"
for f in "${FILES[@]}"; do
  scp -q "${REPO}/${f}" "${TARGET}:${ROOT}/${f}"
  case "$f" in
    *.pem) ssh "$TARGET" "chmod 0644 '${ROOT}/${f}'" ;;
    *) ssh "$TARGET" "chmod +x '${ROOT}/${f}'" ;;
  esac
  echo "[deploy] installed ${f}"
done

scp -q "$EP3_RENDERED" "${TARGET}:/tmp/taey-ep3.service"
scp -q "$ATTESTOR_RENDERED" "${TARGET}:/tmp/taey-model-identity-attestor.service"
ssh "$TARGET" "sudo install -m644 /tmp/taey-ep3.service /etc/systemd/system/taey-ep3.service && sudo install -m644 /tmp/taey-model-identity-attestor.service /etc/systemd/system/taey-model-identity-attestor.service && rm -f /tmp/taey-ep3.service /tmp/taey-model-identity-attestor.service && sudo systemctl daemon-reload"
echo "[deploy] serve + model-identity units installed + daemon-reload"

if [ "$RESTART" -eq 1 ]; then
  ssh "$TARGET" "sudo test -s /etc/taey/model-identity-attestor.env && sudo test -s /etc/taey/model-identity-attestor.key && sudo test -s '${MODEL_PATH}/ARTIFACT_SHA256SUMS'" \
    || { echo "FATAL: restart requires the attestor environment, private key, and artifact seal" >&2; exit 1; }
  echo "[deploy] restarting — you asserted consumers are quiesced"
  ssh "$TARGET" "sudo systemctl restart taey-ep3"
  ready=0
  status_output=""
  for _attempt in $(seq 1 400); do
    if status_output=$(ssh "$TARGET" "sudo '${ROOT}/serving/model_identity_status.py' --environment-file /etc/taey/model-identity-attestor.env --host-local --served-name '${SERVED_NAME}'" 2>&1); then
      echo "$status_output"
      ready=1
      break
    fi
    sleep 5
  done
  [ "$ready" -eq 1 ] || { echo "FATAL: model identity publication did not become ready: ${status_output}" >&2; exit 1; }
else
  echo "[deploy] NOT restarting. The running service still serves from the copy it exec'd."
  echo "[deploy] New files take effect on the next restart. Before that restart, run:"
  echo "[deploy]     serving/list_ep3_consumers.sh <host>"
  echo "[deploy] and notify every consumer it reports."
fi
