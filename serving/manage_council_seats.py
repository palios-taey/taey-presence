#!/usr/bin/env python3
"""Validate, render, launch, and inspect the seven local Taey council seats."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import redis


SERVING_ROOT = Path(__file__).resolve().parent
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
            "http://127.0.0.1:8766/v1/chat/completions",
        ),
        "TAEY_MODEL": os.environ.get("TAEY_MODEL", "ep3"),
        "TAEY_SEAT_MAX_TURNS": os.environ.get("TAEY_SEAT_MAX_TURNS", "60"),
        "TAEY_SEAT_TIMEOUT": os.environ.get("TAEY_SEAT_TIMEOUT", "1800"),
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


def _wait_for_at_rest(
    client: redis.Redis,
    systemctl_binary: str,
    seat: SeatConfig,
    previous_registration: str | None,
    timeout_seconds: float = 5.0,
) -> None:
    prefix = f"{os.environ.get('NOTIFY_KEY_PREFIX', 'taey')}:{seat.seat_id}"
    unit = _systemd_unit(seat)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            [systemctl_binary, "--user", "is-active", "--quiet", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise CouncilConfigError(
                f"{seat.seat_id} unit exited before publishing at-rest liveness"
            )
        raw_registration = client.get(f"{prefix}:seat_registration")
        if raw_registration and raw_registration != previous_registration:
            try:
                registration = json.loads(raw_registration)
            except json.JSONDecodeError as exc:
                raise CouncilConfigError(
                    f"{seat.seat_id} published invalid seat_registration"
                ) from exc
            if (
                not isinstance(registration, dict)
                or registration.get("seat_id") != seat.seat_id
                or registration.get("role_id") != seat.role_id
                or not registration.get("process_generation")
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
    client = _redis_client()
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    active = {
        seat.seat_id: int(
            client.zcard(f"{prefix}:{seat.seat_id}:active_turns")
        )
        for seat in seats
    }
    active = {seat_id: count for seat_id, count in active.items() if count}
    if active:
        raise CouncilConfigError(
            f"refusing launch with authoritative active turns present: {active}"
        )
    previous_registrations = {
        seat.seat_id: client.get(
            f"{prefix}:{seat.seat_id}:seat_registration"
        )
        for seat in seats
    }
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
            subprocess.run(command, check=True)
            started.append(seat.seat_id)
            _wait_for_at_rest(
                client,
                systemctl_binary,
                seat,
                previous_registrations[seat.seat_id],
            )
        except (subprocess.CalledProcessError, CouncilConfigError) as exc:
            raise CouncilConfigError(
                f"launch failed for {seat.seat_id} using {_systemd_unit(seat)} "
                f"with environment {env_file}; already-started units remain "
                f"inspectable: {started}"
            ) from exc
    print(json.dumps({"started": started}, separators=(",", ":")))


def _unit_state(systemctl_binary: str, seat: SeatConfig) -> dict[str, str]:
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
    )
    if result.returncode != 0:
        return {
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
        "unit": _systemd_unit(seat),
        "active": parsed.get("ActiveState", "unknown"),
        "sub": parsed.get("SubState", "unknown"),
        "main_pid": parsed.get("MainPID", "0"),
        "exec_main_status": parsed.get("ExecMainStatus", "unknown"),
    }


def status(seats: list[SeatConfig]) -> list[dict[str, Any]]:
    systemctl_binary = _systemctl_binary()
    client = _redis_client()
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    sessions_root = _sessions_root()
    rows: list[dict[str, Any]] = []
    for seat in seats:
        seat_prefix = f"{prefix}:{seat.seat_id}"
        raw_registration = client.get(f"{seat_prefix}:seat_registration")
        try:
            registration = json.loads(raw_registration) if raw_registration else None
        except json.JSONDecodeError:
            registration = {"invalid": raw_registration}
        rows.append(
            {
                "seat_id": seat.seat_id,
                "role_id": seat.role_id,
                "systemd": _unit_state(systemctl_binary, seat),
                "idle": client.get(f"{seat_prefix}:idle") == "1",
                "turns_open": int(client.get(f"{seat_prefix}:turns_open") or 0),
                "inbox_depth": int(client.llen(f"{seat_prefix}:inbox")),
                "processing_depth": sum(
                    int(client.llen(f"{seat_prefix}:processing:{source}"))
                    for source in ("inbox", "notifications", "orch")
                ),
                "registration": registration,
                "event_log": str(sessions_root / f"{seat.seat_id}.jsonl"),
                "environment_file": str(_seat_env_file(seat)),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("validate", "render", "launch", "status"),
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
