#!/usr/bin/env bash
set -euo pipefail
umask 077

die() {
  printf 'linkedin-unit1-prepare launch refused: %s\n' "$1" >&2
  exit 2
}

require_trace_id() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$ ]] ||
    die "$label is not one public identity"
}

artifact_root="${TAEY_LINKEDIN_UNIT1_ARTIFACT_ROOT:?TAEY_LINKEDIN_UNIT1_ARTIFACT_ROOT is required}"
seat_id="${TAEY_LINKEDIN_UNIT1_SEAT_ID:?TAEY_LINKEDIN_UNIT1_SEAT_ID is required}"
event_id="${TAEY_LINKEDIN_UNIT1_EVENT_ID:?TAEY_LINKEDIN_UNIT1_EVENT_ID is required}"
correlation_id="${TAEY_LINKEDIN_UNIT1_CORRELATION_ID:?TAEY_LINKEDIN_UNIT1_CORRELATION_ID is required}"
model_id="${TAEY_LINKEDIN_UNIT1_MODEL:?TAEY_LINKEDIN_UNIT1_MODEL is required}"
display="${TAEY_LINKEDIN_UNIT1_DISPLAY:?TAEY_LINKEDIN_UNIT1_DISPLAY is required}"
proxy_url="${TAEY_PROXY_URL:-http://127.0.0.1:8765/v1/chat/completions}"

[[ "$seat_id" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] ||
  die "seat ID is not one public identity"
require_trace_id "event ID" "$event_id"
require_trace_id "correlation ID" "$correlation_id"
[[ "$model_id" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$ ]] ||
  die "model ID is not exact"
[[ "$display" =~ ^:[1-9][0-9]{0,2}$ ]] || die "display is not exact"
[[ "$artifact_root" == /* ]] || die "artifact root must be absolute"
[[ -d "$artifact_root" && ! -L "$artifact_root" ]] ||
  die "artifact root must be one existing nonsymlink directory"
resolved_artifact_root="$(realpath -e -- "$artifact_root")"
[[ "$artifact_root" == "$resolved_artifact_root" ]] ||
  die "artifact root path must contain no symlink or normalization alias"

owner_uid="$(id -u)"
[[ "$(stat -c '%u:%a' -- "$artifact_root")" == "$owner_uid:700" ]] ||
  die "artifact root must be owner-controlled 0700"

run_dir="$artifact_root/$correlation_id"
[[ ! -e "$run_dir" && ! -L "$run_dir" ]] ||
  die "fresh identity artifact directory already exists"
mkdir -m 700 -- "$run_dir"
[[ ! -L "$run_dir" && "$(stat -c '%u:%a' -- "$run_dir")" == "$owner_uid:700" ]] ||
  die "identity artifact directory was not created owner-controlled 0700"

headers_path="$run_dir/headers.txt"
response_path="$run_dir/response.json"
set -o noclobber
: > "$headers_path"
: > "$response_path"
set +o noclobber

for output_path in "$headers_path" "$response_path"; do
  [[ -f "$output_path" && ! -L "$output_path" ]] ||
    die "private output is not one regular nonsymlink file"
  [[ "$(stat -c '%u:%a' -- "$output_path")" == "$owner_uid:600" ]] ||
    die "private output was not created owner-controlled 0600"
done

request_body="$(python3 - "$model_id" "$display" <<'PY'
import json
import sys

model, display = sys.argv[1:]
print(json.dumps({
    "model": model,
    "stream": False,
    "chat_template_kwargs": {"enable_thinking": False},
    "messages": [{
        "role": "user",
        "content": (
            f"Prepare the frozen LinkedIn Unit 1 transaction on display {display}. "
            "Continue only through the injected profile until "
            "final_bundle_published or the first failure."
        ),
    }],
}, separators=(",", ":")))
PY
)"

set +e
curl --fail-with-body --silent --show-error \
  --dump-header "$headers_path" \
  --output "$response_path" \
  -H 'Content-Type: application/json' \
  -H "X-Taey-Seat-Id: $seat_id" \
  -H "X-Taey-Event-Id: $event_id" \
  -H "X-Taey-Correlation-Id: $correlation_id" \
  -H 'X-Taey-Tool-Profile: linkedin-unit1-prepare' \
  --data-binary "$request_body" \
  "$proxy_url"
curl_status=$?
set -e

for output_path in "$headers_path" "$response_path"; do
  [[ -f "$output_path" && ! -L "$output_path" ]] ||
    die "private output changed type during launch"
  [[ "$(stat -c '%u:%a' -- "$output_path")" == "$owner_uid:600" ]] ||
    die "private output mode changed during launch"
done

(( curl_status == 0 )) || exit "$curl_status"
printf 'headers=%s\nresponse=%s\n' "$headers_path" "$response_path"
