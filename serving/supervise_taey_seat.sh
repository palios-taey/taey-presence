#!/usr/bin/env bash
set -euo pipefail

readonly ROOT="$(realpath -e -- "${TAEY_ROOT:-$PWD}")"
readonly SESSION="${TAEY_SESSION_NAME:-taey}"
readonly CONVERSATION="${TAEY_CONVERSATION_ID:-main}"
readonly SESSIONS_DIR="${TAEY_SESSIONS_DIR:-$HOME/taey_sessions}"
readonly PROXY="${TAEY_SEAT_PROXY:-http://127.0.0.1:8766/v1/chat/completions}"
readonly PYTHON="${TAEY_SEAT_PYTHON:-/usr/bin/python3}"
readonly TMUX_BIN="${TAEY_TMUX_BIN:-/usr/bin/tmux}"
readonly REDIS_CLI="${TAEY_REDIS_CLI:-/usr/bin/redis-cli}"
readonly REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
readonly REDIS_PORT="${REDIS_PORT:-6379}"
readonly KEY_PREFIX="${NOTIFY_KEY_PREFIX:-taey}"
readonly POLL_SECONDS="${TAEY_SEAT_SUPERVISOR_POLL_SECONDS:-2}"
readonly STOP_TIMEOUT="${TAEY_SEAT_STOP_TIMEOUT:-300}"

fail() {
    printf '[taey-seat-supervisor] FATAL: %s\n' "$*" >&2
    return 1
}

process_environment_value() {
    local pid="$1"
    local key="$2"
    local entry

    while IFS= read -r -d '' entry; do
        if [[ "$entry" == "$key="* ]]; then
            printf '%s' "${entry#*=}"
            return 0
        fi
    done < "/proc/$pid/environ"
    return 1
}

validate_session() {
    local snapshot name windows panes dead pane_path pid actual expected
    local -a fields argv

    "$TMUX_BIN" has-session -t "=$SESSION" 2>/dev/null ||
        fail "tmux session $SESSION does not exist" || return
    snapshot="$("$TMUX_BIN" list-panes -t "=$SESSION" \
        -F '#{session_name}|#{session_windows}|#{window_panes}|#{pane_dead}|#{pane_current_path}|#{pane_pid}|#{pane_current_command}')"
    IFS='|' read -r -a fields <<< "$snapshot"
    [[ ${#fields[@]} -eq 7 ]] ||
        fail "session $SESSION must contain exactly one pane" || return
    name="${fields[0]}"
    windows="${fields[1]}"
    panes="${fields[2]}"
    dead="${fields[3]}"
    pane_path="${fields[4]}"
    pid="${fields[5]}"

    [[ "$name" == "$SESSION" && "$windows" == 1 && "$panes" == 1 && "$dead" == 0 ]] ||
        fail "session $SESSION is not one live single-pane seat" || return
    [[ "$(realpath -e -- "$pane_path")" == "$ROOT" ]] ||
        fail "session $SESSION runs from $pane_path, expected $ROOT" || return
    [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" && -r "/proc/$pid/environ" ]] ||
        fail "session $SESSION pane PID is not inspectable: $pid" || return
    [[ "$(readlink -e -- "/proc/$pid/cwd")" == "$ROOT" ]] ||
        fail "session $SESSION process cwd is not $ROOT" || return
    [[ "$(readlink -e -- "/proc/$pid/exe")" == "$(readlink -e -- "$PYTHON")" ]] ||
        fail "session $SESSION is not running $PYTHON" || return

    mapfile -d '' -t argv < "/proc/$pid/cmdline"
    [[ "${argv[*]}" == *"serving/taey_seat.py"* ]] ||
        fail "session $SESSION is not running serving/taey_seat.py" || return

    for expected in \
        "TAEY_SEAT_PROXY=$PROXY" \
        "TAEY_SESSION_NAME=$SESSION" \
        "TAEY_CONVERSATION_ID=$CONVERSATION" \
        "TAEY_SESSIONS_DIR=$SESSIONS_DIR"; do
        actual="$(process_environment_value "$pid" "${expected%%=*}")" ||
            fail "session $SESSION lacks ${expected%%=*}" || return
        [[ "$actual" == "${expected#*=}" ]] ||
            fail "session $SESSION has ${expected%%=*}=$actual, expected ${expected#*=}" || return
    done
}

seat_command() {
    local command
    local -a argv=(
        env
        "TAEY_SEAT_PROXY=$PROXY"
        "TAEY_SESSION_NAME=$SESSION"
        "TAEY_CONVERSATION_ID=$CONVERSATION"
        "TAEY_SESSIONS_DIR=$SESSIONS_DIR"
        "PYTHONUNBUFFERED=1"
        "$PYTHON"
        -u
        serving/taey_seat.py
    )
    printf -v command '%q ' "${argv[@]}"
    printf '%s' "exec $command"
}

harden_session() {
    "$TMUX_BIN" set-option -t "=$SESSION" remain-on-exit on ||
        fail "cannot set remain-on-exit on $SESSION" || return
}

read_pane_dead() {
    local dead
    dead="$("$TMUX_BIN" display-message -t "=$SESSION" -p '#{pane_dead}')" ||
        fail "cannot read pane_dead for $SESSION" || return
    [[ "$dead" == 0 || "$dead" == 1 ]] ||
        fail "pane_dead for $SESSION is not 0 or 1: $dead" || return
    printf '%s' "$dead"
}

preserve_dead_pane_evidence() {
    local dir stamp status
    dir="$SESSIONS_DIR/$SESSION/exit-evidence"
    mkdir -p "$dir" || fail "cannot create evidence dir $dir" || return
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    status="$("$TMUX_BIN" display-message -t "=$SESSION" -p \
        'dead=#{pane_dead} status=#{pane_dead_status} pid=#{pane_pid}')" ||
        fail "cannot read dead-pane status for $SESSION" || return
    [[ "$status" == *"dead="* && "$status" == *"status="* && "$status" == *"pid="* ]] ||
        fail "incomplete dead-pane status for $SESSION: $status" || return
    printf '%s\n' "$status" > "$dir/$stamp.status" ||
        fail "cannot write status evidence $dir/$stamp.status" || return
    [[ -s "$dir/$stamp.status" ]] ||
        fail "empty status evidence $dir/$stamp.status" || return
    "$TMUX_BIN" capture-pane -t "=$SESSION" -p -S -500 \
        > "$dir/$stamp.capture" ||
        fail "cannot capture dead pane for $SESSION" || return
    [[ -f "$dir/$stamp.capture" ]] ||
        fail "missing capture evidence $dir/$stamp.capture" || return
    printf '[taey-seat-supervisor] dead-pane evidence stamp=%s %s\n' "$stamp" "$status"
}

respawn_seat_pane() {
    "$TMUX_BIN" respawn-pane -k -t "=$SESSION" "$(seat_command)" ||
        fail "cannot respawn seat pane for $SESSION" || return
    for _ in {1..20}; do
        if validate_session 2>/dev/null; then
            return 0
        fi
        sleep 0.25
    done
    validate_session
}

recover_dead_pane() {
    local dead
    dead="$(read_pane_dead)" || return
    [[ "$dead" == 1 ]] || return 0
    preserve_dead_pane_evidence || return
    respawn_seat_pane || return
}

start_session() {
    "$TMUX_BIN" new-session -d -s "$SESSION" -c "$ROOT" -- /bin/sleep 2147483647 ||
        fail "cannot create holding session $SESSION" || return
    harden_session || return
    respawn_seat_pane || return
}

seat_is_idle() {
    local idle turns
    idle="$("$REDIS_CLI" -h "$REDIS_HOST" -p "$REDIS_PORT" --raw \
        GET "$KEY_PREFIX:$SESSION:idle" 2>/dev/null || true)"
    turns="$("$REDIS_CLI" -h "$REDIS_HOST" -p "$REDIS_PORT" --raw \
        GET "$KEY_PREFIX:$SESSION:turns_open" 2>/dev/null || true)"
    [[ "$idle" == 1 && "${turns:-0}" == 0 ]]
}

stop_session() {
    local deadline
    trap - TERM INT HUP
    if ! "$TMUX_BIN" has-session -t "=$SESSION" 2>/dev/null; then
        exit 0
    fi

    printf '[taey-seat-supervisor] stop requested; waiting for %s to become idle\n' "$SESSION"
    deadline=$((SECONDS + STOP_TIMEOUT))
    while (( SECONDS < deadline )); do
        if seat_is_idle; then
            "$TMUX_BIN" kill-session -t "=$SESSION"
            exit 0
        fi
        sleep 2
    done
    printf '[taey-seat-supervisor] stop timeout reached; terminating %s\n' "$SESSION" >&2
    "$TMUX_BIN" kill-session -t "=$SESSION"
    exit 0
}

case "${1:-}" in
    "") ;;
    --check)
        validate_session
        printf '[taey-seat-supervisor] verified session=%s root=%s proxy=%s\n' \
            "$SESSION" "$ROOT" "$PROXY"
        exit 0
        ;;
    *)
        fail "usage: ${0##*/} [--check]"
        exit 64
        ;;
esac

[[ -x "$PYTHON" ]] || { fail "Python is not executable: $PYTHON"; exit 78; }
[[ -x "$TMUX_BIN" ]] || { fail "tmux is not executable: $TMUX_BIN"; exit 78; }
[[ -x "$REDIS_CLI" ]] || { fail "redis-cli is not executable: $REDIS_CLI"; exit 78; }
[[ -f "$ROOT/serving/taey_seat.py" ]] || {
    fail "Taey seat is absent: $ROOT/serving/taey_seat.py"
    exit 78
}

trap stop_session TERM INT HUP

if "$TMUX_BIN" has-session -t "=$SESSION" 2>/dev/null; then
    harden_session
    recover_dead_pane || exit 1
    validate_session || exit 78
    printf '[taey-seat-supervisor] adopted session=%s root=%s proxy=%s\n' \
        "$SESSION" "$ROOT" "$PROXY"
else
    start_session || exit 1
    printf '[taey-seat-supervisor] started session=%s root=%s proxy=%s\n' \
        "$SESSION" "$ROOT" "$PROXY"
fi

while sleep "$POLL_SECONDS"; do
    "$TMUX_BIN" has-session -t "=$SESSION" 2>/dev/null ||
        fail "tmux session $SESSION does not exist" || exit 1
    recover_dead_pane || exit 1
    validate_session || exit 1
done
