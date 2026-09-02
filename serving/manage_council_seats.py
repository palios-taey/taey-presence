#!/usr/bin/env python3
"""Validate, render, launch, and inspect the seven local Taey council seats."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import redis
from dotenv import load_dotenv

import council_prompt_receipt as prompt_producer

SERVING_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SERVING_ROOT.parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT))
from dashboard.native_council import CouncilTransportFailure, RoundLedger  # noqa: E402

DEFAULT_MANIFEST = SERVING_ROOT / "council_seats.json"
RUN_ROOT = SERVING_ROOT / "run"
SYSTEMD_UNIT_PREFIX = "taey-council-seat@"
LEGACY_TERMINAL_ROUND = "dcm-20260817T014529Z-9b0dbd7863d5"
LEGACY_TERMINAL_LEDGER_SHA256 = "24b6899841603b8173e7a329e135273520f9f77aaac9171020b82c1ad9bfb367"
LEGACY_TERMINAL_SOURCE_SHA256 = "3894dafb71b74ed8b65c842ea91cd6d90896f6651d2500f3f24807f142aaa7c8"
LEGACY_TERMINAL_REQUEST_COUNT = 13
_READ_REGISTRATION_LUA = """
local value = redis.call('GET', KEYS[1])
if not value then
    return {}
end
return {value, redis.call('TTL', KEYS[1])}
"""
_DELETE_EXACT_REGISTRATION_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


CouncilConfigError = prompt_producer.CouncilManifestError
SeatConfig = prompt_producer.SeatConfig


@dataclass(frozen=True)
class LegacyTerminalPlan:
    round_id: str
    archive_path: Path
    requests: tuple[dict[str, Any], ...]


def load_manifest(manifest_path: Path) -> list[SeatConfig]:
    return list(prompt_producer.load_manifest(manifest_path).seats)


def _sessions_root() -> Path:
    default_root = Path(
        os.environ.get("TAEY_SESSIONS_DIR", str(Path.home() / "taey_sessions"))
    ).expanduser()
    return Path(
        os.environ.get("TAEY_COUNCIL_SESSIONS_DIR", str(default_root / "council"))
    ).expanduser().resolve()


def _seat_environment(seat: SeatConfig, sessions_root: Path) -> dict[str, str]:
    event_log = sessions_root / f"{seat.seat_id}.jsonl"
    environment = {
        "TAEY_SESSION_NAME": seat.seat_id,
        "TAEY_COUNCIL_ROLE_ID": seat.role_id,
        "TAEY_CONVERSATION_ID": seat.conversation_id,
        "TAEY_EXECUTIVE_EVENT_LOG": str(event_log),
        "TAEY_COUNCIL_MANIFEST_PATH": str(seat.manifest_path),
        "TAEY_COUNCIL_SHARED_PROMPT_PATH": str(seat.shared_prompt),
        "TAEY_COUNCIL_ROLE_PROMPT_PATH": str(seat.role_prompt),
        "TAEY_SEAT_PROXY": os.environ.get(
            "TAEY_SEAT_PROXY",
            # Seats default to the WORKER proxy (Thor2): seven concurrent seat
            # generations must not share main Taey's serving hardware — measured
            # 2026-08-11: a 7-seat wave on the main proxy finished 11-21 min
            # against a 180 s window because every seat starved on shared tokens.
            "http://127.0.0.1:8767/v1/chat/completions",
        ),
        "TAEY_MODEL": os.environ.get("TAEY_MODEL", "ep3"),
        "TAEY_SEAT_MAX_TURNS": os.environ.get("TAEY_SEAT_MAX_TURNS", "60"),
        "TAEY_COUNCIL_LIVENESS_TTL_SECONDS": os.environ.get(
            "TAEY_COUNCIL_LIVENESS_TTL_SECONDS",
            "5",
        ),
        "TAEY_COUNCIL_LIVENESS_REFRESH_SECONDS": os.environ.get(
            "TAEY_COUNCIL_LIVENESS_REFRESH_SECONDS",
            "1",
        ),
        "NOTIFY_KEY_PREFIX": os.environ.get("NOTIFY_KEY_PREFIX", "taey"),
        "REDIS_HOST": os.environ.get("REDIS_HOST", "127.0.0.1"),
        "REDIS_PORT": os.environ.get("REDIS_PORT", "6379"),
        "PYTHONUNBUFFERED": "1",
    }
    for name in (
        "DCM_NEO4J_URI",
        "DCM_NEO4J_DATABASE",
        "DCM_NEO4J_USER",
        "DCM_NEO4J_PASSWORD",
        "DCM_ALLOW_INSECURE",
        "TAEY_MODEL_IDENTITY_AUTHORITY_ID",
        "TAEY_MODEL_IDENTITY_REDIS_HOST",
        "TAEY_MODEL_IDENTITY_REDIS_PORT",
        "TAEY_MODEL_IDENTITY_UPSTREAM_COMPLETION_ENDPOINT",
        "TAEY_MODEL_IDENTITY_EXPECTED_ALIASES",
    ):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def render(seats: list[SeatConfig]) -> list[dict[str, Any]]:
    sessions_root = _sessions_root()
    return [
        {
            "seat_id": seat.seat_id,
            "role_id": seat.role_id,
            "conversation_id": seat.conversation_id,
            "event_log": str(
                sessions_root / f"{seat.seat_id}.jsonl"
            ),
            "inbox": (
                f"{os.environ.get('NOTIFY_KEY_PREFIX', 'taey')}:"
                f"{seat.seat_id}:inbox"
            ),
            "proxy": _seat_environment(seat, sessions_root)["TAEY_SEAT_PROXY"],
            "model_selector": _seat_environment(seat, sessions_root)["TAEY_MODEL"],
            "model_authority": "shared_proxy_route",
            "shared_prompt": str(seat.shared_prompt),
            "role_prompt": str(seat.role_prompt),
        }
        for seat in seats
    ]


def _seat_instance(seat: SeatConfig) -> str:
    return seat.seat_id.removeprefix("taey-council-")


def _systemd_unit(seat: SeatConfig) -> str:
    return f"{SYSTEMD_UNIT_PREFIX}{_seat_instance(seat)}.service"


def _seat_env_file(seat: SeatConfig) -> Path:
    return RUN_ROOT / f"council-seat-{_seat_instance(seat)}.env"


def _systemctl_binary() -> str:
    systemctl_binary = shutil.which("systemctl")
    if not systemctl_binary:
        raise CouncilConfigError("systemctl is not installed or not on PATH")
    return systemctl_binary


def _systemctl_show_properties(
    systemctl_binary: str,
    unit: str,
    properties: tuple[str, ...],
) -> dict[str, str]:
    try:
        result = subprocess.run(
            [
                systemctl_binary,
                "--user",
                "show",
                unit,
                *(f"--property={name}" for name in properties),
                "--no-page",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise CouncilConfigError(f"timed out inspecting {unit}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise CouncilConfigError(f"could not inspect {unit}: {detail}")
    return dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )


def _preflight_systemd_unit(systemctl_binary: str, seat: SeatConfig) -> None:
    unit = _systemd_unit(seat)
    properties = _systemctl_show_properties(
        systemctl_binary,
        unit,
        ("LoadState", "WorkingDirectory", "EnvironmentFiles"),
    )
    load_state = properties.get("LoadState", "unknown")
    if load_state != "loaded":
        raise CouncilConfigError(
            f"{unit} is not installed (LoadState={load_state}); install "
            f"serving/systemd/taey-council-seat@.service with @TAEY_ROOT@={REPO_ROOT}"
        )
    working_directory = properties.get("WorkingDirectory", "")
    if not working_directory:
        raise CouncilConfigError(
            f"{unit} does not report WorkingDirectory; reinstall the unit for "
            f"this checkout ({REPO_ROOT})"
        )
    if Path(working_directory).expanduser().resolve() != REPO_ROOT:
        raise CouncilConfigError(
            f"{unit} is installed for WorkingDirectory={working_directory}, "
            f"but this launcher is running from {REPO_ROOT}; launch from the "
            "installed checkout or reinstall the unit for this checkout"
        )
    expected_env = str(_seat_env_file(seat).resolve())
    environment_files = properties.get("EnvironmentFiles", "")
    if expected_env not in environment_files:
        raise CouncilConfigError(
            f"{unit} EnvironmentFiles={environment_files or '<unset>'} does not "
            f"consume generated environment file {expected_env}; reinstall the "
            "unit after substituting @TAEY_ROOT@ for this checkout"
        )


def _tmux_session_exists(tmux_binary: str | None, seat_id: str) -> bool:
    if not tmux_binary:
        return False
    result = subprocess.run(
        [tmux_binary, "has-session", "-t", f"={seat_id}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _systemd_env_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise CouncilConfigError("systemd EnvironmentFile values must be single-line")
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    return f'"{escaped}"'


def _write_seat_environment_file(seat: SeatConfig, sessions_root: Path) -> Path:
    RUN_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = _seat_environment(seat, sessions_root)
    env_file = _seat_env_file(seat)
    contents = "".join(
        f"{name}={_systemd_env_value(value)}\n"
        for name, value in sorted(environment.items())
    )
    env_file.write_text(contents, encoding="utf-8")
    env_file.chmod(0o600)
    return env_file


def _prepare_sessions_root(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise CouncilConfigError(
            f"council sessions directory must not be group/world accessible: "
            f"{path} mode={mode:o}"
        )


def _redis_client() -> redis.Redis:
    client = redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=8,
    )
    client.ping()
    return client


def _lifecycle_fence_token() -> str:
    pid = os.getpid()
    return json.dumps(
        {
            "schema_version": 1,
            "kind": "taey-council-lifecycle",
            "pid": pid,
            "process_ctime_ns": os.stat(f"/proc/{pid}").st_ctime_ns,
            "started_at": time.time(),
        },
        separators=(",", ":"),
    )


def _acquire_lifecycle_fence(
    client: redis.Redis,
    fence_key: str,
) -> str:
    token = _lifecycle_fence_token()
    if client.set(fence_key, token, nx=True):
        return token
    existing = client.get(fence_key)
    if existing is None:
        if client.set(fence_key, token, nx=True):
            return token
        raise CouncilConfigError(
            "council lifecycle ownership changed during acquisition"
        )
    try:
        owner = json.loads(existing or "")
        owner_pid = int(owner["pid"])
        owner_ctime_ns = int(owner["process_ctime_ns"])
        if (
            not isinstance(owner, dict)
            or owner.get("schema_version") != 1
            or owner.get("kind") != "taey-council-lifecycle"
        ):
            raise ValueError("invalid lifecycle fence owner")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CouncilConfigError(
            "another council seat lifecycle is in progress and its owner "
            "cannot be proven dead"
        ) from exc
    try:
        owner_live = os.stat(f"/proc/{owner_pid}").st_ctime_ns == owner_ctime_ns
    except FileNotFoundError:
        owner_live = False
    except OSError as exc:
        raise CouncilConfigError(
            "cannot verify the existing council lifecycle owner"
        ) from exc
    if owner_live:
        raise CouncilConfigError("another council seat lifecycle is in progress")
    client.eval(
        _DELETE_EXACT_REGISTRATION_LUA,
        1,
        fence_key,
        existing,
    )
    if not client.set(fence_key, token, nx=True):
        raise CouncilConfigError(
            "council lifecycle ownership changed during dead-owner recovery"
        )
    return token


def _release_lifecycle_fence(
    client: redis.Redis,
    fence_key: str,
    token: str,
) -> bool:
    return bool(
        int(
            client.eval(
                _DELETE_EXACT_REGISTRATION_LUA,
                1,
                fence_key,
                token,
            )
        )
    )


def _registration_snapshot(
    client: redis.Redis,
    registration_key: str,
) -> tuple[str | None, int]:
    snapshot = client.eval(
        _READ_REGISTRATION_LUA,
        1,
        registration_key,
    )
    if snapshot == []:
        return None, -2
    if not isinstance(snapshot, list) or len(snapshot) != 2:
        raise CouncilConfigError(
            f"invalid registration snapshot for {registration_key}"
        )
    return str(snapshot[0]), int(snapshot[1])


def _wait_for_at_rest(
    client: redis.Redis,
    systemctl_binary: str,
    seat: SeatConfig,
    previous_registration: str | None,
    timeout_seconds: float = 5.0,
) -> None:
    prefix = f"{os.environ.get('NOTIFY_KEY_PREFIX', 'taey')}:{seat.seat_id}"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        unit_state = _unit_state(systemctl_binary, seat)
        if unit_state["active"] != "active":
            raise CouncilConfigError(
                f"{seat.seat_id} unit exited before publishing at-rest liveness"
            )
        raw_registration, registration_ttl = _registration_snapshot(
            client,
            f"{prefix}:seat_registration",
        )
        if raw_registration and raw_registration != previous_registration:
            try:
                registration = json.loads(raw_registration)
            except json.JSONDecodeError as exc:
                raise CouncilConfigError(
                    f"{seat.seat_id} published invalid seat_registration"
                ) from exc
            if (
                not isinstance(registration, dict)
                or registration.get("schema_version") != 1
                or registration.get("seat_id") != seat.seat_id
                or registration.get("seat_kind") != "council"
                or registration.get("role_id") != seat.role_id
                or not registration.get("process_generation")
                or registration.get("response_contract")
                != "taey-council-contribution/v1"
                or registration.get("readiness") not in {"recovering", "ready"}
                or type(registration.get("pid")) is not int
                or str(registration["pid"]) != unit_state["main_pid"]
                or type(registration.get("liveness_ttl_seconds")) is not int
                or registration["liveness_ttl_seconds"] < 2
                or registration_ttl <= 0
            ):
                raise CouncilConfigError(
                    f"{seat.seat_id} published mismatched seat_registration: "
                    f"{registration}"
                )
            if registration["readiness"] == "recovering":
                registration = None
        else:
            registration = None
        if (
            registration is not None
            and client.get(f"{prefix}:idle") == "1"
            and int(client.get(f"{prefix}:turns_open") or 0) == 0
        ):
            return
        time.sleep(0.1)
    raise CouncilConfigError(
        f"{seat.seat_id} did not publish a new identity-matched registration "
        f"with idle=1 and turns_open=0 within "
        f"{timeout_seconds:.1f}s"
    )


def launch(seats: list[SeatConfig], legacy_terminal_round: str | None = None) -> None:
    systemctl_binary = _systemctl_binary()
    tmux_binary = shutil.which("tmux")
    existing = [
        seat.seat_id
        for seat in seats
        if _tmux_session_exists(tmux_binary, seat.seat_id)
    ]
    if existing:
        raise CouncilConfigError(
            "refusing a partial or configuration-blind launch because canonical "
            f"sessions already exist: {', '.join(existing)}"
        )
    for seat in seats:
        _preflight_systemd_unit(systemctl_binary, seat)
    client = _redis_client()
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    replacement_key = f"{prefix}:dcm:native:seat_replacement"
    replacement_token = _acquire_lifecycle_fence(client, replacement_key)
    unit_states = {
        seat.seat_id: _unit_state(systemctl_binary, seat) for seat in seats
    }
    unsafe_units = {
        seat_id: state
        for seat_id, state in unit_states.items()
        if (
            not state["answered"]
            or state["active"] not in {"inactive", "failed"}
            or state["main_pid"] != "0"
        )
    }
    if unsafe_units:
        _release_lifecycle_fence(
            client,
            replacement_key,
            replacement_token,
        )
        raise CouncilConfigError(
            "launch requires seven stopped, inspectable units; use replace for "
            f"live or mixed generations: {unsafe_units}"
        )
    sessions_root = _sessions_root()
    _prepare_sessions_root(sessions_root)
    legacy_plan = _prepare_legacy_or_release(
        client, seats, prefix, sessions_root, legacy_terminal_round,
        replacement_key, replacement_token)
    blockers = _seat_work_blockers(client, seats, prefix)
    active_rounds = _active_rounds(client, prefix)
    if (blockers or active_rounds) and legacy_plan is None:
        _release_lifecycle_fence(
            client,
            replacement_key,
            replacement_token,
        )
        raise CouncilConfigError(
            "refusing launch until every council delivery identity and round "
            f"is terminal: blockers={blockers} active_rounds={active_rounds}"
        )
    previous_registrations = {
        seat.seat_id: client.get(
            f"{prefix}:{seat.seat_id}:seat_registration"
        )
        for seat in seats
    }
    for seat in seats:
        previous = previous_registrations[seat.seat_id]
        if previous is None:
            continue
        current_state = _unit_state(systemctl_binary, seat)
        if (
            not current_state["answered"]
            or current_state["active"] not in {"inactive", "failed"}
            or current_state["main_pid"] != "0"
        ):
            raise CouncilConfigError(
                f"{seat.seat_id} changed state before stale registration cleanup: "
                f"{current_state}"
            )
        deleted = int(
            client.eval(
                _DELETE_EXACT_REGISTRATION_LUA,
                1,
                f"{prefix}:{seat.seat_id}:seat_registration",
                previous,
            )
        )
        if deleted != 1:
            raise CouncilConfigError(
                f"{seat.seat_id} registration changed before launch"
            )
        previous_registrations[seat.seat_id] = None
    started: list[str] = []
    for seat in seats:
        env_file = _write_seat_environment_file(seat, sessions_root)
        command = [
            systemctl_binary,
            "--user",
            "start",
            _systemd_unit(seat),
        ]
        try:
            subprocess.run(command, check=True, timeout=120)
            started.append(seat.seat_id)
            _wait_for_at_rest(
                client,
                systemctl_binary,
                seat,
                previous_registrations[seat.seat_id],
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            CouncilConfigError,
        ) as exc:
            raise CouncilConfigError(
                f"launch failed for {seat.seat_id} using {_systemd_unit(seat)} "
                f"with environment {env_file}; already-started units remain "
                f"inspectable: {started}"
            ) from exc
    final_blockers = _seat_work_blockers(client, seats, prefix)
    final_rounds = _active_rounds(client, prefix)
    if final_blockers or final_rounds:
        raise CouncilConfigError(
            "council state changed before launch fence release; the fence "
            "remains active: "
            f"blockers={final_blockers} active_rounds={final_rounds}"
        )
    if legacy_plan is not None:
        _validate_legacy_terminal_state(
            client, seats, prefix, sessions_root, legacy_plan, final=True)
    if not _release_lifecycle_fence(
        client,
        replacement_key,
        replacement_token,
    ):
        raise CouncilConfigError(
            "council seat launch completed but its dispatch fence was lost"
        )
    result: dict[str, Any] = {"started": started}
    if legacy_plan is not None:
        result["legacy_terminal_reconciled"] = _legacy_receipt(legacy_plan)
    print(json.dumps(result, separators=(",", ":")))


def _unit_state(systemctl_binary: str, seat: SeatConfig) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                systemctl_binary,
                "--user",
                "show",
                _systemd_unit(seat),
                "--property=ActiveState",
                "--property=SubState",
                "--property=MainPID",
                "--property=ExecMainStatus",
                "--no-page",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        result = None
    if result is None or result.returncode != 0:
        return {
            "transport": "systemd",
            "answered": False,
            "unit": _systemd_unit(seat),
            "active": "unknown",
            "sub": "unknown",
            "main_pid": "0",
            "exec_main_status": "unknown",
        }
    parsed = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )
    return {
        "transport": "systemd",
        "answered": True,
        "unit": _systemd_unit(seat),
        "active": parsed.get("ActiveState", "unknown"),
        "sub": parsed.get("SubState", "unknown"),
        "main_pid": parsed.get("MainPID", "0"),
        "exec_main_status": parsed.get("ExecMainStatus", "unknown"),
    }


def _tmux_state(tmux_binary: str | None, seat: SeatConfig) -> dict[str, Any]:
    return {
        "transport": "tmux",
        "answered": bool(tmux_binary),
        "session": seat.seat_id,
        "exists": _tmux_session_exists(tmux_binary, seat.seat_id),
    }


def _processing_depth(client: redis.Redis, seat_prefix: str) -> int:
    processing_keys: set[str] = set()
    for source in ("inbox", "notifications", "orch"):
        base_key = f"{seat_prefix}:processing:{source}"
        processing_keys.add(base_key)
        processing_keys.update(client.scan_iter(match=f"{base_key}:*"))
    return sum(int(client.llen(key)) for key in processing_keys)


def _seat_work_blockers(
    client: redis.Redis,
    seats: list[SeatConfig],
    prefix: str,
) -> dict[str, dict[str, int]]:
    blockers: dict[str, dict[str, int]] = {}
    for seat in seats:
        seat_prefix = f"{prefix}:{seat.seat_id}"
        state = {
            "active_turns": int(client.zcard(f"{seat_prefix}:active_turns")),
            "inbox": int(client.llen(f"{seat_prefix}:inbox")),
            "notifications": int(
                client.llen(f"{seat_prefix}:notifications")
            ),
            "orch": int(client.llen(f"{prefix}:notify:{seat.seat_id}:orch")),
            "processing": _processing_depth(client, seat_prefix),
        }
        if any(state.values()):
            blockers[seat.seat_id] = state
    return blockers


def _active_rounds(client: redis.Redis, prefix: str) -> dict[str, str]:
    rounds: dict[str, str] = {}
    pattern = f"{prefix}:dcm:native:conversation:*:active_round"
    for key in client.scan_iter(match=pattern):
        round_id = client.get(key)
        if round_id:
            rounds[str(key)] = str(round_id)
    return rounds


def _read_private_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.exists() and (path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise CouncilConfigError(f"council event log is not private: {path}")
    raw = path.read_bytes() if path.exists() else b""
    if raw and not raw.endswith(b"\n"):
        raise CouncilConfigError(f"partial council event log: {path}")
    events = [json.loads(line) for line in raw.splitlines()]
    if any(not isinstance(event, dict) for event in events):
        raise CouncilConfigError(f"non-object council event log record: {path}")
    return events


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_legacy_requests(document: dict[str, Any], seats: list[SeatConfig],
                              events: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    seat_roles = {seat.seat_id: seat.role_id for seat in seats}
    started_events = [event for event in events if event.get("event_type") == "seat_started"]
    started = {(event.get("seat_id"), event.get("prompt_revision"), event.get("phase")): event
               for event in started_events}
    requests = document.get("requests")
    if (len(started) != len(started_events) or not isinstance(requests, list)
            or len(requests) != LEGACY_TERMINAL_REQUEST_COUNT):
        raise CouncilConfigError("legacy archive count/terminal starts are invalid")
    identities: set[tuple[str, str]] = set()
    for item in requests:
        if not isinstance(item, dict) or set(item) != {"seat_id", "raw", "raw_sha256"}:
            raise CouncilConfigError("legacy archive request shape is invalid")
        seat_id, raw = item["seat_id"], item["raw"]
        if (seat_id not in seat_roles or not isinstance(raw, str)
                or hashlib.sha256(raw.encode()).hexdigest() != item["raw_sha256"]):
            raise CouncilConfigError("legacy archive request seat/raw is invalid")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise CouncilConfigError("legacy council request is not an object")
        revision, phase = payload.get("prompt_revision"), payload.get("round_phase")
        if type(revision) is not int or phase not in {"independent", "critique"}:
            raise CouncilConfigError("legacy council request revision/phase is invalid")
        request_id = f"{document['round_id']}:{revision}:{phase}:{seat_id}"
        message_id = hashlib.sha256(
            f"{document['round_id']}\0{revision}\0{phase}\0{seat_id}".encode()
        ).hexdigest()[:24]
        expected = {"from": "taey", "type": "council_request", "priority": "high",
                    "msg_id": message_id, "event_id": request_id, "request_id": request_id,
                    "correlation_id": document["round_id"],
                    "council_run_id": document["round_id"], "round_id": document["round_id"]}
        ledger_start = started.get((seat_id, revision, phase))
        if ("expected_process_generation" in payload
                or any(payload.get(key) != value for key, value in expected.items())
                or not isinstance(payload.get("body"), str) or not payload["body"].strip()
                or not isinstance(ledger_start, dict)
                or ledger_start.get("role_id") != seat_roles[seat_id]
                or ledger_start.get("request_id") != request_id
                or ledger_start.get("message_id") != message_id
                or (seat_id, message_id) in identities):
            raise CouncilConfigError("legacy request does not match its terminal ledger")
        identities.add((seat_id, message_id))
    return tuple(requests)


def _legacy_request_locations(client: redis.Redis, seats: list[SeatConfig],
                              prefix: str) -> list[tuple[str, str, str]]:
    locations: list[tuple[str, str, str]] = []
    for seat in seats:
        inbox = f"{prefix}:{seat.seat_id}:inbox"
        processing = f"{prefix}:{seat.seat_id}:processing:inbox"
        keys = [inbox, processing,
                *sorted(client.scan_iter(match=f"{processing}:*"))]
        locations.extend((seat.seat_id, key, raw)
                         for key in keys for raw in client.lrange(key, 0, -1))
    return locations


def _validate_legacy_terminal_state(client: redis.Redis, seats: list[SeatConfig],
                                    prefix: str, sessions_root: Path,
                                    plan: LegacyTerminalPlan, *,
                                    final: bool = False) -> None:
    archived = {(item["seat_id"], item["raw"]) for item in plan.requests}
    locations = _legacy_request_locations(client, seats, prefix)
    current = [(seat_id, raw) for seat_id, _, raw in locations]
    expected_blockers: dict[str, dict[str, int]] = {}
    for seat in seats:
        inbox = f"{prefix}:{seat.seat_id}:inbox"
        inbox_count = sum(1 for seat_id, key, _ in locations
                          if seat_id == seat.seat_id and key == inbox)
        processing_count = sum(1 for seat_id, key, _ in locations
                               if seat_id == seat.seat_id and key != inbox)
        if inbox_count or processing_count:
            expected_blockers[seat.seat_id] = {
                "active_turns": 0, "inbox": inbox_count, "notifications": 0,
                "orch": 0, "processing": processing_count}
    if (
        len(current) != len(set(current))
        or not set(current) <= archived
        or _seat_work_blockers(client, seats, prefix) != expected_blockers
        or _active_rounds(client, prefix)
        or (final and current)
    ):
        raise CouncilConfigError("council state exceeds the terminal legacy exception")
    current_ids = {json.loads(raw)["msg_id"] for _, raw in current}
    processing_ids = {
        json.loads(raw)["msg_id"] for seat_id, key, raw in locations
        if key != f"{prefix}:{seat_id}:inbox"}
    for item in plan.requests:
        payload = json.loads(item["raw"])
        message_id = payload["msg_id"]
        events = _read_private_jsonl(sessions_root / f"{item['seat_id']}.jsonl")
        attempts = [event for event in events if event.get("event_type") == "turn_attempt"
                    and message_id in (event.get("message_ids") or [])]
        outcomes = [event for event in events if event.get("event_type") == "turn_outcome"
                    and message_id in (event.get("message_ids") or [])]
        outcome = outcomes[0] if len(outcomes) == 1 else {}
        expected = {"kind": "dead_generation_terminal", "message_ids": [message_id],
                    "request_id": payload["request_id"], "round_id": plan.round_id,
                    "process_generation": "legacy-unbound", "skipped_inference": True,
                    "inference_state": "not_started"}
        valid_outcome = all(outcome.get(key) == value for key, value in expected.items())
        if (attempts or (message_id in current_ids and outcomes
                         and (message_id not in processing_ids or not valid_outcome))
                or (message_id not in current_ids and not valid_outcome)):
            raise CouncilConfigError(
                f"legacy request lacks dead_generation_terminal/no-attempt proof: {message_id}"
            )


def _prepare_legacy_terminal_plan(client: redis.Redis, seats: list[SeatConfig],
                                  prefix: str, sessions_root: Path,
                                  round_id: str) -> LegacyTerminalPlan:
    if round_id != LEGACY_TERMINAL_ROUND:
        raise CouncilConfigError("legacy exception is bound to one exact terminal round")
    dashboard_root = Path(os.environ.get(
        "TAEY_SESSIONS_DIR", str(Path.home() / "taey_sessions")
    )).expanduser().resolve()
    ledger = RoundLedger(dashboard_root, "main", round_id)
    before, events, after = ledger.path.read_bytes(), ledger.events(), ledger.path.read_bytes()
    terminal = [event for event in events if event.get("event_type") in
                {"round_completed", "round_failed"}]
    if before != after or len(terminal) != 1 or not events or (
        events[-1].get("event_type") != "terminal_projected"
    ):
        raise CouncilConfigError(f"round {round_id} is not stably terminal and projected")
    ledger_sha256 = hashlib.sha256(after).hexdigest()
    if ledger_sha256 != LEGACY_TERMINAL_LEDGER_SHA256:
        raise CouncilConfigError("legacy terminal ledger does not match the pinned incident")
    archive_dir = sessions_root / "legacy-terminal-reconciliation"
    archive_dir_created = not archive_dir.exists()
    archive_dir.mkdir(mode=0o700, exist_ok=True)
    if archive_dir.is_symlink() or stat.S_IMODE(archive_dir.stat().st_mode) & 0o077:
        raise CouncilConfigError(f"legacy archive directory is not private: {archive_dir}")
    if archive_dir_created:
        _fsync_directory(sessions_root)
    archive_path = archive_dir / f"{round_id}.json"
    create_archive = not archive_path.exists()
    if not create_archive:
        if archive_path.is_symlink() or stat.S_IMODE(archive_path.stat().st_mode) != 0o400:
            raise CouncilConfigError(f"legacy archive is not immutable: {archive_path}")
        document = json.loads(archive_path.read_text(encoding="utf-8"))
    else:
        source_state = [{"key": f"{prefix}:{seat.seat_id}:inbox", "values": client.lrange(
            f"{prefix}:{seat.seat_id}:inbox", 0, -1)}
                        for seat in seats]
        source_sha256 = _canonical_sha256(source_state)
        if source_sha256 != LEGACY_TERMINAL_SOURCE_SHA256:
            raise CouncilConfigError("legacy source state does not match the pinned incident")
        requests = [{"seat_id": seat.seat_id, "raw": raw, "raw_sha256": hashlib.sha256(
            raw.encode()).hexdigest()}
                    for seat, source in zip(seats, source_state, strict=True)
                    for raw in source["values"]]
        document = {"schema_version": 1, "contract": "taey-council-terminal-legacy-archive/v1",
                    "conversation_id": "main", "round_id": round_id,
                    "ledger_sha256": ledger_sha256, "ledger_events": events,
                    "source_sha256": source_sha256, "requests": requests}
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("contract") != "taey-council-terminal-legacy-archive/v1"
        or document.get("conversation_id") != "main"
        or document.get("round_id") != round_id
        or document.get("ledger_sha256") != ledger_sha256
        or document.get("ledger_events") != events
        or document.get("source_sha256") != LEGACY_TERMINAL_SOURCE_SHA256
    ):
        raise CouncilConfigError("legacy archive does not match the canonical ledger")
    requests = _validate_legacy_requests(document, seats, events)
    archived_state = [{"key": f"{prefix}:{seat.seat_id}:inbox", "values": [
        item["raw"] for item in requests if item["seat_id"] == seat.seat_id]}
                      for seat in seats]
    if _canonical_sha256(archived_state) != document["source_sha256"]:
        raise CouncilConfigError("legacy archive source fingerprint is invalid")
    if create_archive:
        encoded = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{round_id}.", suffix=".tmp", dir=archive_dir)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fchmod(handle.fileno(), 0o400)
                os.fsync(handle.fileno())
            os.link(temporary_path, archive_path, follow_symlinks=False)
        finally:
            temporary_path.unlink(missing_ok=True)
        _fsync_directory(archive_dir)
    plan = LegacyTerminalPlan(round_id, archive_path, requests)
    _validate_legacy_terminal_state(client, seats, prefix, sessions_root, plan)
    return plan


def _prepare_legacy_or_release(client: redis.Redis, seats: list[SeatConfig],
                               prefix: str, sessions_root: Path, round_id: str | None,
                               fence_key: str, fence_token: str) -> LegacyTerminalPlan | None:
    if not round_id:
        return None
    try:
        return _prepare_legacy_terminal_plan(client, seats, prefix, sessions_root, round_id)
    except (CouncilConfigError, CouncilTransportFailure, OSError, ValueError):
        _release_lifecycle_fence(client, fence_key, fence_token)
        raise


def _legacy_receipt(plan: LegacyTerminalPlan) -> dict[str, Any]:
    return {"round_id": plan.round_id, "request_count": len(plan.requests),
            "archive": str(plan.archive_path), "proof": "dead_generation_terminal/no_attempt"}


def replace(seats: list[SeatConfig], legacy_terminal_round: str | None = None) -> None:
    systemctl_binary = _systemctl_binary()
    tmux_binary = shutil.which("tmux")
    existing_tmux = [
        seat.seat_id
        for seat in seats
        if _tmux_session_exists(tmux_binary, seat.seat_id)
    ]
    if existing_tmux:
        raise CouncilConfigError(
            "refusing generation replacement while canonical tmux sessions "
            f"exist: {', '.join(existing_tmux)}"
        )
    for seat in seats:
        _preflight_systemd_unit(systemctl_binary, seat)
    if legacy_terminal_round:
        unit_states = {
            seat.seat_id: _unit_state(systemctl_binary, seat) for seat in seats
        }
        unsafe_units = {
            seat_id: state for seat_id, state in unit_states.items()
            if not state["answered"] or state["active"] != "inactive"
            or state["main_pid"] != "0"
        }
        if unsafe_units:
            raise CouncilConfigError(
                "legacy reconciliation requires all seven units already inactive: "
                f"{unsafe_units}"
            )
    client = _redis_client()
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    replacement_key = f"{prefix}:dcm:native:seat_replacement"
    replacement_token = _acquire_lifecycle_fence(client, replacement_key)
    sessions_root = _sessions_root()
    _prepare_sessions_root(sessions_root)
    legacy_plan = _prepare_legacy_or_release(
        client, seats, prefix, sessions_root, legacy_terminal_round,
        replacement_key, replacement_token)
    blockers = _seat_work_blockers(client, seats, prefix)
    active_rounds = _active_rounds(client, prefix)
    if (blockers or active_rounds) and legacy_plan is None:
        _release_lifecycle_fence(
            client,
            replacement_key,
            replacement_token,
        )
        raise CouncilConfigError(
            "refusing generation replacement until every council delivery "
            "identity and round is terminal: "
            f"blockers={blockers} active_rounds={active_rounds}"
        )
    previous_registrations = {
        seat.seat_id: client.get(
            f"{prefix}:{seat.seat_id}:seat_registration"
        )
        for seat in seats
    }
    for seat in seats:
        _write_seat_environment_file(seat, sessions_root)
    stopped: list[str] = []
    for seat in seats:
        try:
            subprocess.run(
                [systemctl_binary, "--user", "stop", _systemd_unit(seat)],
                check=True,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise CouncilConfigError(
                f"failed to stop {seat.seat_id}; already stopped={stopped}"
            ) from exc
        stopped.append(seat.seat_id)
    for seat in seats:
        if _unit_state(systemctl_binary, seat)["active"] != "inactive":
            raise CouncilConfigError(
                f"{seat.seat_id} did not become inactive; stopped={stopped}"
            )
    stopped_blockers = _seat_work_blockers(client, seats, prefix)
    stopped_rounds = _active_rounds(client, prefix)
    if stopped_blockers or stopped_rounds:
        if legacy_plan is not None:
            _validate_legacy_terminal_state(client, seats, prefix, sessions_root,
                                            legacy_plan)
        else:
            raise CouncilConfigError(
                "council state changed after replacement fencing; all units remain "
                "stopped and the fence remains active: "
                f"blockers={stopped_blockers} active_rounds={stopped_rounds}"
            )
    for seat in seats:
        previous = previous_registrations[seat.seat_id]
        if previous is not None:
            client.eval(
                _DELETE_EXACT_REGISTRATION_LUA,
                1,
                f"{prefix}:{seat.seat_id}:seat_registration",
                previous,
            )
    started: list[str] = []
    for seat in seats:
        try:
            subprocess.run(
                [systemctl_binary, "--user", "start", _systemd_unit(seat)],
                check=True,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise CouncilConfigError(
                f"failed to start {seat.seat_id}; already started={started}"
            ) from exc
        started.append(seat.seat_id)
        _wait_for_at_rest(
            client,
            systemctl_binary,
            seat,
            previous_registrations[seat.seat_id],
        )
    final_blockers = _seat_work_blockers(client, seats, prefix)
    final_rounds = _active_rounds(client, prefix)
    if final_blockers or final_rounds:
        raise CouncilConfigError(
            "council state changed before replacement fence release; the fence "
            "remains active: "
            f"blockers={final_blockers} active_rounds={final_rounds}"
        )
    if legacy_plan is not None:
        _validate_legacy_terminal_state(
            client, seats, prefix, sessions_root, legacy_plan, final=True)
    if not _release_lifecycle_fence(
        client,
        replacement_key,
        replacement_token,
    ):
        raise CouncilConfigError(
            "council seat replacement completed but its dispatch fence was lost"
        )
    result: dict[str, Any] = {
        "replaced": started,
        "registration_contract": "generation-bound-expiring/v1",
    }
    if legacy_plan is not None:
        result["legacy_terminal_reconciled"] = _legacy_receipt(legacy_plan)
    print(json.dumps(result, separators=(",", ":")))


def status(seats: list[SeatConfig]) -> list[dict[str, Any]]:
    systemctl_binary = _systemctl_binary()
    tmux_binary = shutil.which("tmux")
    client = _redis_client()
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    sessions_root = _sessions_root()
    rows: list[dict[str, Any]] = []
    for seat in seats:
        seat_prefix = f"{prefix}:{seat.seat_id}"
        raw_registration, registration_ttl = _registration_snapshot(
            client,
            f"{seat_prefix}:seat_registration",
        )
        try:
            registration = json.loads(raw_registration) if raw_registration else None
        except json.JSONDecodeError:
            registration = {"invalid": raw_registration}
        systemd_state = _unit_state(systemctl_binary, seat)
        registration_live = bool(
            isinstance(registration, dict)
            and registration.get("schema_version") == 1
            and registration.get("seat_id") == seat.seat_id
            and registration.get("seat_kind") == "council"
            and registration.get("role_id") == seat.role_id
            and registration.get("process_generation")
            and registration.get("response_contract")
            == "taey-council-contribution/v1"
            and registration.get("readiness") == "ready"
            and type(registration.get("pid")) is int
            and str(registration["pid"]) == systemd_state["main_pid"]
            and systemd_state["active"] == "active"
            and type(registration.get("liveness_ttl_seconds")) is int
            and registration["liveness_ttl_seconds"] >= 2
            and registration_ttl > 0
        )
        tmux_state = _tmux_state(tmux_binary, seat)
        rows.append(
            {
                "seat_id": seat.seat_id,
                "role_id": seat.role_id,
                "transports": {
                    "systemd": systemd_state,
                    "tmux": tmux_state,
                },
                "systemd": systemd_state,
                "tmux": tmux_state["exists"],
                "idle": client.get(f"{seat_prefix}:idle") == "1",
                "turns_open": int(client.get(f"{seat_prefix}:turns_open") or 0),
                "inbox_depth": int(client.llen(f"{seat_prefix}:inbox")),
                "processing_depth": _processing_depth(client, seat_prefix),
                "registration": registration,
                "registration_live": registration_live,
                "registration_ttl_seconds": registration_ttl,
                "event_log": str(sessions_root / f"{seat.seat_id}.jsonl"),
                "environment_file": str(_seat_env_file(seat)),
            }
        )
    return rows


def select_seats(
    seats: list[SeatConfig],
    requested_seat_ids: list[str] | None,
) -> list[SeatConfig]:
    if not requested_seat_ids:
        return list(seats)
    manifest_by_id = {seat.seat_id: seat for seat in seats}
    seen: set[str] = set()
    for raw in requested_seat_ids:
        candidate = raw.strip()
        if not candidate:
            raise CouncilConfigError("seat-id cannot be empty")
        if candidate in seen:
            raise CouncilConfigError(f"duplicate seat-id requested: {candidate}")
        if candidate not in manifest_by_id:
            raise CouncilConfigError(
                f"unknown seat-id requested: {candidate} "
                f"(available: {', '.join(manifest_by_id)})"
            )
        seen.add(candidate)
    return [seat for seat in seats if seat.seat_id in seen]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage private supporting runtime seats for Taey's local council."
    )
    parser.add_argument(
        "command",
        choices=(
            "validate",
            "render",
            "prompt-contracts",
            "launch",
            "replace",
            "status",
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
    )
    parser.add_argument(
        "--seat-id",
        action="append",
        dest="seat_ids",
        default=None,
        help=(
            "Canonical seat ID to replace (repeatable, replace command only; "
            "e.g. --seat-id taey-council-1)."
        ),
    )
    parser.add_argument("--reconcile-terminal-round")
    args = parser.parse_args()
    try:
        seats = load_manifest(args.manifest.resolve())
        if args.seat_ids and args.command != "replace":
            raise CouncilConfigError("--seat-id is only supported for the replace command")
        if args.reconcile_terminal_round and args.command not in {"launch", "replace"}:
            raise CouncilConfigError("--reconcile-terminal-round requires launch or replace")
        if args.command == "validate":
            print(
                json.dumps(
                    {
                        "contract": "taey-local-council-seats/v1",
                        "seat_count": len(seats),
                        "roles": {
                            seat.seat_id: seat.role_id for seat in seats
                        },
                    },
                    separators=(",", ":"),
                )
            )
        elif args.command == "render":
            print(json.dumps(render(seats), indent=2))
        elif args.command == "prompt-contracts":
            manifest = prompt_producer.load_manifest(args.manifest.resolve())
            print(
                json.dumps(
                    [
                        prompt_producer.prompt_contract_receipt(manifest, seat)
                        for seat in manifest.seats
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
        elif args.command == "launch":
            launch(seats, args.reconcile_terminal_round)
        elif args.command == "replace":
            target_seats = select_seats(seats, args.seat_ids)
            replace(target_seats, args.reconcile_terminal_round)
        else:
            print(json.dumps(status(seats), indent=2))
    except (CouncilConfigError, OSError, redis.RedisError, ValueError) as exc:
        print(
            f"[taey-council] FATAL: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
