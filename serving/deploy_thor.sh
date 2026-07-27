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
#   ./deploy_thor.sh --check  <user@host> [taey-root]   # verify only, mutates nothing
#   ./deploy_thor.sh          <user@host> [taey-root]   # install, no restart
#   ./deploy_thor.sh --restart --model-path /path <user@host> [root]
#
# Exit: 0 = in sync / deployed. 1 = drift found (--check) or deploy failed.
set -euo pipefail

CHECK=0; RESTART=0; MODEL_PATH=""; SERVED_NAME=""; KEEP_NAME=0; NAME_EXPLICIT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check)       CHECK=1; shift ;;
    --restart)     RESTART=1; shift ;;
    --model-path)  MODEL_PATH="${2:?--model-path needs a value}"; shift 2 ;;
    --served-name) SERVED_NAME="${2:?--served-name needs a value}"; NAME_EXPLICIT=1; shift 2 ;;
    --keep-served-name) KEEP_NAME=1; shift ;;
    -h|--help)     sed -n '2,40p' "$0"; exit 0 ;;
    *)             break ;;
  esac
done

TARGET="${1:?usage: deploy_thor.sh [--check] [--restart] [--model-path P] <user@host> [taey-root]}"
ROOT="${2:-}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="${REPO}/serving/systemd/taey-ep3.service"
FILES=(serving/vllm_serve.sh bin/gpu-cleanup.sh)

[ -r "$UNIT_SRC" ] || { echo "FATAL: missing $UNIT_SRC" >&2; exit 1; }
for f in "${FILES[@]}"; do
  [ -r "${REPO}/${f}" ] || { echo "FATAL: missing ${REPO}/${f} — the repo cannot deploy what it does not carry" >&2; exit 1; }
done

# Derive TAEY_ROOT from the node's own installed unit when not given, so we adopt the layout
# already in use rather than imposing a new one and leaving two copies behind.
if [ -z "$ROOT" ]; then
  ROOT=$(ssh "$TARGET" "systemctl cat taey-ep3 2>/dev/null | sed -n 's|^ExecStartPre=\(/.*\)/bin/gpu-cleanup.sh.*|\1|p' | head -1" || true)
  ROOT="${ROOT:-\$HOME/palios-taey}"
fi
echo "[deploy] target=${TARGET} root=${ROOT} repo=$(cd "$REPO" && git rev-parse --short HEAD 2>/dev/null || echo unknown)"

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
# Word this precisely. This compares the INSTALLED unit and the files on disk against the repo.
# It does NOT prove the RUNNING process was started from them — a process that exec'd an older
# copy keeps running it until restart. Claiming "the node runs this" would be true of the config
# and false of the live process, which is the exact gap this whole script exists to close.
[ "$drift" -eq 0 ] && echo "  IN SYNC — installed unit + files match this repo (the running process picks them up at next start)"

if [ "$CHECK" -eq 1 ]; then
  exit "$drift"
fi

# ---------- install ----------
# Preserve the served model unless explicitly overridden. A deploy that quietly repointed the
# model would change production behaviour while reporting only that files were copied.
if [ -z "$MODEL_PATH" ]; then
  # --value is load-bearing. Without it systemctl prints `Environment=TAEY_MODEL_PATH=... VAR=...`
  # as ONE line, so the first whitespace-split token is `Environment=TAEY_MODEL_PATH=/path` and an
  # anchored ^TAEY_MODEL_PATH= match silently finds nothing. --value emits the bare assignments.
  MODEL_PATH=$(ssh "$TARGET" "systemctl show taey-ep3 -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^TAEY_MODEL_PATH=//p' | head -1" || true)
  [ -n "$MODEL_PATH" ] && echo "[deploy] preserving served model: ${MODEL_PATH}"
fi

# PRESERVE THE SERVED ID TOO. The unit template hardcodes Environment=TAEY_SERVED_NAME=ep3, so
# without this a plain deploy — one intended only to push a launcher change — SILENTLY REVERTS the
# served id back to the template value. Observed 2026-07-27: Thor1 had been deliberately renamed to
# a candidate id so stale ep3 callers would 404; a later deploy carrying an unrelated change put it
# back to 'ep3' while the weights stayed the candidate's, recreating the exact alias trap the
# --served-name gate below exists to prevent. The gate guarded the artifact/name relationship and
# the deploy path went around it. Preserve the node's value; require a flag to change it.
if [ -z "$SERVED_NAME" ]; then
  SERVED_NAME=$(ssh "$TARGET" "systemctl show taey-ep3 -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^TAEY_SERVED_NAME=//p' | head -1" || true)
  [ -n "$SERVED_NAME" ] && echo "[deploy] preserving served id: ${SERVED_NAME}"
fi
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
  cur_path=$(ssh "$TARGET" "systemctl show taey-ep3 -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^TAEY_MODEL_PATH=//p' | head -1" || true)
  cur_name=$(ssh "$TARGET" "systemctl show taey-ep3 -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^TAEY_SERVED_NAME=//p' | head -1" || true)
  # Test NAME_EXPLICIT, not emptiness. The preservation step above fills SERVED_NAME from the
  # node, so an emptiness test here would ALWAYS be false and this gate would never fire again —
  # the preservation fix silently disabled the guard it sits next to. Caught by a pre-window
  # precondition check, not by the edit that caused it.
  if [ -n "$cur_path" ] && [ "$cur_path" != "$MODEL_PATH" ] && [ "$NAME_EXPLICIT" -eq 0 ] && [ "$KEEP_NAME" -eq 0 ]; then
    cat >&2 <<EOF
FATAL: this deploy changes the served ARTIFACT but not the served NAME.
         artifact: ${cur_path}
               ->  ${MODEL_PATH}
         served id stays: '${cur_name:-ep3}'
  A client addressing '${cur_name:-ep3}' would then receive DIFFERENT WEIGHTS with HTTP 200 and no
  indication anything changed. Decide the name explicitly:
    --served-name <new-id>   stale callers get a clean 404 — use this while a node serves a
                             CANDIDATE that differs from its peers
    --keep-served-name       you assert every caller of '${cur_name:-ep3}' should move to the new
                             weights — use this for a fleet-wide promotion
EOF
    exit 1
  fi
fi
[ -n "$SERVED_NAME" ] && echo "[deploy] served id: ${SERVED_NAME}"

ssh "$TARGET" "mkdir -p '${ROOT}/serving' '${ROOT}/bin'"
for f in "${FILES[@]}"; do
  scp -q "${REPO}/${f}" "${TARGET}:${ROOT}/${f}"
  ssh "$TARGET" "chmod +x '${ROOT}/${f}'"
  echo "[deploy] installed ${f}"
done

tmp=$(mktemp)
sed -e "s|@TAEY_ROOT@|${ROOT}|g" -e "s|@TAEY_MODEL_PATH@|${MODEL_PATH}|g" "$UNIT_SRC" > "$tmp"
# Apply an explicit served-id override into the unit's env block.
if [ -n "$SERVED_NAME" ]; then
  sed -i -e "s|^Environment=TAEY_SERVED_NAME=.*|Environment=TAEY_SERVED_NAME=${SERVED_NAME}|" "$tmp"
  grep -q "TAEY_SERVED_NAME=${SERVED_NAME}" "$tmp" || { echo "FATAL: served-name substitution did not land" >&2; rm -f "$tmp"; exit 1; }
fi
grep -q '@TAEY_' "$tmp" && { echo "FATAL: unsubstituted placeholder remains in the unit" >&2; rm -f "$tmp"; exit 1; }
scp -q "$tmp" "${TARGET}:/tmp/taey-ep3.service"; rm -f "$tmp"
ssh "$TARGET" "sudo install -m644 /tmp/taey-ep3.service /etc/systemd/system/taey-ep3.service && rm -f /tmp/taey-ep3.service && sudo systemctl daemon-reload"
echo "[deploy] unit installed + daemon-reload"

if [ "$RESTART" -eq 1 ]; then
  echo "[deploy] restarting — you asserted consumers are quiesced"
  ssh "$TARGET" "sudo systemctl restart taey-ep3"
  ssh "$TARGET" "systemctl is-active taey-ep3" | sed 's/^/[deploy] service: /'
else
  echo "[deploy] NOT restarting. The running service still serves from the copy it exec'd."
  echo "[deploy] New files take effect on the next restart. Before that restart, run:"
  echo "[deploy]     serving/list_ep3_consumers.sh <host>"
  echo "[deploy] and notify every consumer it reports."
fi
