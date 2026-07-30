#!/usr/bin/env bash
# Promote an already-served model endpoint into the durable UI-facing Taey proxy.
#
# This is the second half of a model release:
#   1. deploy_thor.sh installs and starts the accepted artifact on a serving node.
#   2. promote_main_model.sh waits for Main Taey to be idle, switches its upstream,
#      proves the exact model through the proxy, runs one real inference, and writes
#      a machine-readable receipt.
#
# The previous drop-in is restored automatically if the new route fails CONTROL.
set -Eeuo pipefail

ENDPOINT=""
MODEL=""
UNIT="taey-soma-proxy-mira.service"
PROXY_URL="http://127.0.0.1:8766"
TIMEOUT_SECONDS=900
RECEIPT_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/taey-presence/model-releases"
RESTART=1

usage() {
  cat <<'EOF'
usage:
  promote_main_model.sh --endpoint URL --model ID [options]

required:
  --endpoint URL       OpenAI-compatible raw model endpoint, e.g. http://node1.example:8000
  --model ID           Exact served model id expected at that endpoint

options:
  --unit NAME          Main proxy user unit (default: taey-soma-proxy-mira.service)
  --proxy-url URL      Main proxy URL (default: http://127.0.0.1:8766)
  --timeout SECONDS    Idle/startup deadline (default: 900)
  --receipt-dir DIR    Receipt destination
  --no-restart         Stage the route and daemon-reload, but do not restart
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --endpoint) ENDPOINT="${2:?--endpoint needs a value}"; shift 2 ;;
    --model) MODEL="${2:?--model needs a value}"; shift 2 ;;
    --unit) UNIT="${2:?--unit needs a value}"; shift 2 ;;
    --proxy-url) PROXY_URL="${2:?--proxy-url needs a value}"; shift 2 ;;
    --timeout) TIMEOUT_SECONDS="${2:?--timeout needs a value}"; shift 2 ;;
    --receipt-dir) RECEIPT_DIR="${2:?--receipt-dir needs a value}"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "FATAL: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$ENDPOINT" ] || { echo "FATAL: --endpoint is required" >&2; exit 2; }
[ -n "$MODEL" ] || { echo "FATAL: --model is required" >&2; exit 2; }
case "$ENDPOINT" in
  http://*|https://*) ;;
  *) echo "FATAL: endpoint must start with http:// or https://" >&2; exit 2 ;;
esac
case "$ENDPOINT:$MODEL:$UNIT:$PROXY_URL" in
  *$'\n'*|*$'\r'*|*" "*|*$'\t'*)
    echo "FATAL: endpoint, model, unit, and proxy URL must not contain whitespace" >&2
    exit 2
    ;;
esac
case "$TIMEOUT_SECONDS" in
  ""|*[!0-9]*) echo "FATAL: --timeout must be a positive integer" >&2; exit 2 ;;
esac
[ "$TIMEOUT_SECONDS" -gt 0 ] || { echo "FATAL: --timeout must be positive" >&2; exit 2; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_SHA="$(git -C "$REPO" rev-parse HEAD)"
CONFIG_ROOT="${XDG_CONFIG_HOME:-$HOME/.config}"
DROPIN_DIR="${CONFIG_ROOT}/systemd/user/${UNIT}.d"
DROPIN="${DROPIN_DIR}/zz-vllm-primary.conf"
LOCK="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/taey-main-model-promotion.lock"
TMP_ROOT="$(mktemp -d)"
BACKUP="${TMP_ROOT}/previous-dropin.conf"
RAW_RECEIPT="${TMP_ROOT}/raw.json"
PROXY_RECEIPT="${TMP_ROOT}/proxy.json"
CHAT_RECEIPT="${TMP_ROOT}/chat.json"
HAD_DROPIN=0
ROLLBACK_ARMED=0

cleanup() {
  rm -rf "$TMP_ROOT"
}

rollback() {
  local rc=$?
  trap - ERR
  if [ "$ROLLBACK_ARMED" -eq 1 ]; then
    echo "CONTROL FAILED: restoring previous Main Taey route" >&2
    if [ "$HAD_DROPIN" -eq 1 ]; then
      install -m 600 "$BACKUP" "$DROPIN"
    else
      rm -f "$DROPIN"
    fi
    systemctl --user daemon-reload
    systemctl --user restart "$UNIT"
    echo "ROLLBACK COMPLETE: ${UNIT} restarted with its prior route" >&2
  fi
  cleanup
  exit "$rc"
}
trap cleanup EXIT
trap rollback ERR

exec 9>"$LOCK"
flock -n 9 || {
  echo "FATAL: another Main Taey model promotion holds $LOCK" >&2
  exit 1
}

systemctl --user is-active --quiet "$UNIT" || {
  echo "FATAL: Main Taey proxy unit is not active: $UNIT" >&2
  exit 1
}

python3 - "$ENDPOINT" "$MODEL" "$RAW_RECEIPT" <<'PY'
import json
import sys
import urllib.request

endpoint, expected, output = sys.argv[1:]
with urllib.request.urlopen(endpoint.rstrip("/") + "/health", timeout=15) as response:
    if response.status != 200:
        raise SystemExit(f"raw health returned HTTP {response.status}")
with urllib.request.urlopen(endpoint.rstrip("/") + "/v1/models", timeout=15) as response:
    payload = json.load(response)
models = payload.get("data", [])
matches = [model for model in models if model.get("id") == expected]
if len(matches) != 1:
    raise SystemExit(
        f"raw endpoint does not advertise exact model {expected!r}: "
        f"{[model.get('id') for model in models]}"
    )
with open(output, "w") as handle:
    json.dump(matches[0], handle, sort_keys=True)
print(f"RAW PREFLIGHT PASS: model={expected} root={matches[0].get('root')}")
PY

current_endpoint="$(
  systemctl --user show "$UNIT" -p Environment --value |
    tr ' ' '\n' | sed -n 's/^VLLM_BASE_URL=//p' | head -1
)"
[ -n "$current_endpoint" ] || {
  echo "FATAL: $UNIT has no effective VLLM_BASE_URL" >&2
  exit 1
}

deadline=$((SECONDS + TIMEOUT_SECONDS))
while :; do
  active="$(
    python3 - "$PROXY_URL" <<'PY'
import json
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1].rstrip("/") + "/health", timeout=10) as response:
    payload = json.load(response)
print(payload.get("liveness", {}).get("active_turns", "UNKNOWN"))
PY
  )"
  [ "$active" = "0" ] && break
  [ "$active" != "UNKNOWN" ] || {
    echo "FATAL: Main Taey health does not expose attributable active_turns" >&2
    exit 1
  }
  [ "$SECONDS" -lt "$deadline" ] || {
    echo "FATAL: Main Taey did not become idle within ${TIMEOUT_SECONDS}s (active_turns=$active)" >&2
    exit 1
  }
  sleep 2
done
echo "IDLE GATE PASS: active_turns=0"

mkdir -p "$DROPIN_DIR"
if [ -f "$DROPIN" ]; then
  cp "$DROPIN" "$BACKUP"
  HAD_DROPIN=1
fi
{
  printf '%s\n' '[Service]'
  printf 'Environment=VLLM_BASE_URL=%s\n' "$ENDPOINT"
} > "${TMP_ROOT}/new-dropin.conf"
install -m 600 "${TMP_ROOT}/new-dropin.conf" "$DROPIN"
systemctl --user daemon-reload
echo "ROUTE STAGED: ${current_endpoint} -> ${ENDPOINT}"

if [ "$RESTART" -eq 0 ]; then
  echo "NOT RESTARTED: route will apply at the next ${UNIT} start"
  exit 0
fi

ROLLBACK_ARMED=1
systemctl --user restart "$UNIT"

while :; do
  if systemctl --user is-active --quiet "$UNIT" &&
      python3 - "$PROXY_URL" "$MODEL" "$PROXY_RECEIPT" <<'PY'
import json
import sys
import urllib.request

proxy, expected, output = sys.argv[1:]
try:
    with urllib.request.urlopen(proxy.rstrip("/") + "/health", timeout=10) as response:
        health = json.load(response)
except Exception:
    raise SystemExit(1)
if health.get("status") != "healthy":
    raise SystemExit(1)
if health.get("liveness", {}).get("active_turns") != 0:
    raise SystemExit(1)
try:
    with urllib.request.urlopen(proxy.rstrip("/") + "/v1/models", timeout=10) as response:
        payload = json.load(response)
except Exception:
    raise SystemExit(1)
models = payload.get("data", [])
matches = [model for model in models if model.get("id") == expected]
if len(matches) != 1:
    raise SystemExit(1)
with open(output, "w") as handle:
    json.dump({"health": health, "model": matches[0]}, handle, sort_keys=True)
PY
  then
    break
  fi
  [ "$SECONDS" -lt "$deadline" ] || {
    echo "FATAL: Main Taey did not expose exact model $MODEL before timeout" >&2
    exit 1
  }
  sleep 2
done
echo "ROUTE CONTROL PASS: Main Taey advertises model=${MODEL}"

python3 - "$PROXY_URL" "$MODEL" "$CHAT_RECEIPT" <<'PY'
import json
import sys
import urllib.request

proxy, model, output = sys.argv[1:]
body = json.dumps({
    "model": model,
    "messages": [{
        "role": "user",
        "content": (
            "This is the production observation after a model promotion. "
            "Reply with one concise sentence confirming you are available."
        ),
    }],
    "temperature": 0,
    "max_tokens": 128,
}).encode()
request = urllib.request.Request(
    proxy.rstrip("/") + "/v1/chat/completions",
    data=body,
    headers={"Content-Type": "application/json", "X-Taey-Seat-Id": "release-control"},
)
with urllib.request.urlopen(request, timeout=300) as response:
    payload = json.load(response)
choices = payload.get("choices", [])
if response.status != 200 or len(choices) != 1:
    raise SystemExit("production inference did not return one HTTP-200 choice")
choice = choices[0]
message = choice.get("message", {})
generated = (message.get("content") or "") + (message.get("reasoning_content") or "")
completion_tokens = payload.get("usage", {}).get("completion_tokens", 0)
if payload.get("model") != model or completion_tokens <= 0 or not generated:
    raise SystemExit(
        "production inference receipt is incomplete: "
        f"model={payload.get('model')!r} completion_tokens={completion_tokens} "
        f"generated_chars={len(generated)}"
    )
receipt = {
    "id": payload.get("id"),
    "model": payload.get("model"),
    "finish_reason": choice.get("finish_reason"),
    "completion_tokens": completion_tokens,
    "generated_chars": len(generated),
}
with open(output, "w") as handle:
    json.dump(receipt, handle, sort_keys=True)
print(
    "PRODUCTION INFERENCE PASS: "
    f"id={receipt['id']} tokens={completion_tokens} "
    f"finish_reason={receipt['finish_reason']}"
)
PY

mkdir -p "$RECEIPT_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
safe_model="$(printf '%s' "$MODEL" | tr -c 'A-Za-z0-9._-' '_')"
receipt="${RECEIPT_DIR}/${stamp}-${safe_model}.json"
python3 - \
  "$receipt" "$REPO_SHA" "$UNIT" "$current_endpoint" "$ENDPOINT" "$MODEL" \
  "$RAW_RECEIPT" "$PROXY_RECEIPT" "$CHAT_RECEIPT" <<'PY'
import datetime
import json
import os
import sys

(output, repo_sha, unit, previous_endpoint, endpoint, model,
 raw_path, proxy_path, chat_path) = sys.argv[1:]
payload = {
    "schema": "taey.main_model_promotion.v1",
    "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "serving_repo_commit": repo_sha,
    "unit": unit,
    "previous_endpoint": previous_endpoint,
    "endpoint": endpoint,
    "expected_model": model,
    "raw_model": json.load(open(raw_path)),
    "proxy_control": json.load(open(proxy_path)),
    "production_inference": json.load(open(chat_path)),
}
temporary = output + ".tmp"
with open(temporary, "w") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, output)
print(f"RELEASE RECEIPT: {output}")
PY

ROLLBACK_ARMED=0
echo "PROMOTION COMPLETE: Main Taey -> ${ENDPOINT} model=${MODEL}"
