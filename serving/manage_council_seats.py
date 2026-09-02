#!/usr/bin/env python3
"""Validate, render, launch, and inspect the seven local Taey council seats."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import redis


SERVING_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SERVING_ROOT.parent
DEFAULT_MANIFEST = SERVING_ROOT / "council_seats.json"
RUN_ROOT = SERVING_ROOT / "run"
SYSTEMD_UNIT_PREFIX = "taey-council-seat@"
CANONICAL_ROLES = {
    "taey-council-1": "context-memory",
    "taey-council-2": "evidence-reality",
    "taey-council-3": "systems-dependencies",
    "taey-council-4": "adversarial-failure",
    "taey-council-5": "scope-intent",
    "taey-council-6": "options-alternatives",
    "taey-council-7": "control-acceptance",
}
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


class CouncilConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class SeatConfig:
    seat_id: str
    role_id: str
    conversation_id: str
    shared_prompt: Path
    role_prompt: Path


def _required_string(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise CouncilConfigError(f"{field_name} must be a non-empty string")
    return normalized


def _prompt_path(manifest_path: Path, value: Any, field_name: str) -> Path:
    relative = Path(_required_string(value, field_name))
    if relative.is_absolute():
        raise CouncilConfigError(f"{field_name} must be relative to the manifest")
    resolved = (manifest_path.parent / relative).resolve()
    if not resolved.is_file():
        raise CouncilConfigError(f"{field_name} is not a file: {resolved}")
    if not resolved.read_text(encoding="utf-8").strip():
        raise CouncilConfigError(f"{field_name} is empty: {resolved}")
    return resolved


def load_manifest(manifest_path: Path) -> list[SeatConfig]:
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CouncilConfigError(f"cannot read manifest {manifest_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise CouncilConfigError("manifest root must be an object")
    if document.get("schema_version") != 1:
        raise CouncilConfigError("manifest schema_version must be 1")
    if document.get("contract") != "taey-local-council-seats/v1":
        raise CouncilConfigError(
            "manifest contract must be taey-local-council-seats/v1"
        )
    shared_prompt = _prompt_path(
        manifest_path,
        document.get("shared_prompt"),
        "shared_prompt",
    )
    raw_seats = document.get("seats")
    if not isinstance(raw_seats, list):
        raise CouncilConfigError("manifest seats must be an array")
    seats: list[SeatConfig] = []
    seen_conversations: set[str] = set()
    for index, raw_seat in enumerate(raw_seats):
        if not isinstance(raw_seat, dict):
            raise CouncilConfigError(f"seats[{index}] must be an object")
        seat_id = _required_string(raw_seat.get("seat_id"), f"seats[{index}].seat_id")
        role_id = _required_string(raw_seat.get("role_id"), f"seats[{index}].role_id")
        conversation_id = _required_string(
            raw_seat.get("conversation_id"),
            f"seats[{index}].conversation_id",
        )
        if conversation_id == "main" or conversation_id in seen_conversations:
            raise CouncilConfigError(
                f"conversation_id must be unique and private: {conversation_id}"
            )
        seen_conversations.add(conversation_id)
        seats.append(
            SeatConfig(
                seat_id=seat_id,
                role_id=role_id,
                conversation_id=conversation_id,
                shared_prompt=shared_prompt,
                role_prompt=_prompt_path(
                    manifest_path,
                    raw_seat.get("role_prompt"),
                    f"seats[{index}].role_prompt",
                ),
            )
        )
    actual_roles = {seat.seat_id: seat.role_id for seat in seats}
    if actual_roles != CANONICAL_ROLES:
        raise CouncilConfigError(
            f"manifest seat/role mapping differs from canonical mapping: {actual_roles}"
        )
    return seats


def _sessions_root() -> Path:
    default_root = Path(
        os.environ.get("TAEY_SESSIONS_DIR", str(Path.home() / "taey_sessions"))
    ).expanduser()
    return Path(
        os.environ.get("TAEY_COUNCIL_SESSIONS_DIR", str(default_root / "council"))
    ).expanduser().resolve()


def _seat_environment(seat: SeatConfig, sessions_root: Path) -> dict[str, str]:
    event_log = sessions_root / f"{seat.seat_id}.jsonl"
    return {
        "TAEY_SESSION_NAME": seat.seat_id,
        "TAEY_COUNCIL_ROLE_ID": seat.role_id,
        "TAEY_CONVERSATION_ID": seat.conversation_id,
        "TAEY_EXECUTIVE_EVENT_LOG": str(event_log),
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
                or registration.get("readiness") != "ready"
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


def launch(seats: list[SeatConfig]) -> None:
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
    blockers = _seat_work_blockers(client, seats, prefix)
    active_rounds = _active_rounds(client, prefix)
    if blockers or active_rounds:
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
    sessions_root = _sessions_root()
    _prepare_sessions_root(sessions_root)
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
    if not _release_lifecycle_fence(
        client,
        replacement_key,
        replacement_token,
    ):
        raise CouncilConfigError(
            "council seat launch completed but its dispatch fence was lost"
        )
    print(json.dumps({"started": started}, separators=(",", ":")))


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


def replace(seats: list[SeatConfig]) -> None:
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
    client = _redis_client()
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    replacement_key = f"{prefix}:dcm:native:seat_replacement"
    replacement_token = _acquire_lifecycle_fence(client, replacement_key)
    blockers = _seat_work_blockers(client, seats, prefix)
    active_rounds = _active_rounds(client, prefix)
    if blockers or active_rounds:
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
    sessions_root = _sessions_root()
    _prepare_sessions_root(sessions_root)
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
    if not _release_lifecycle_fence(
        client,
        replacement_key,
        replacement_token,
    ):
        raise CouncilConfigError(
            "council seat replacement completed but its dispatch fence was lost"
        )
    print(
        json.dumps(
            {
                "replaced": started,
                "registration_contract": "generation-bound-expiring/v1",
            },
            separators=(",", ":"),
        )
    )


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("validate", "render", "launch", "replace", "status"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
    )
    args = parser.parse_args()
    try:
        seats = load_manifest(args.manifest.resolve())
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
        elif args.command == "launch":
            launch(seats)
        elif args.command == "replace":
            replace(seats)
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
