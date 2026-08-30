#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


ACTION_SCHEMA = "ats_greenhouse_frozen_action_v1"
MANIFEST_SCHEMA = "taey_greenhouse_ats_private_manifest_v1"
_SEAT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TRACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_DISPLAY = re.compile(r"^:[1-9][0-9]{0,2}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_AT_EMPTY_PATH = 0x1000
_STATX_CTIME = 0x0080
_STATX_BTIME = 0x0800


class GreenhouseObservePublisherError(RuntimeError):
    pass


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint32),
        ("blksize", ctypes.c_uint32),
        ("attributes", ctypes.c_uint64),
        ("nlink", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("mode", ctypes.c_uint16),
        ("spare0", ctypes.c_uint16),
        ("ino", ctypes.c_uint64),
        ("size", ctypes.c_uint64),
        ("blocks", ctypes.c_uint64),
        ("attributes_mask", ctypes.c_uint64),
        ("atime", _StatxTimestamp),
        ("btime", _StatxTimestamp),
        ("ctime", _StatxTimestamp),
        ("mtime", _StatxTimestamp),
        ("rdev_major", ctypes.c_uint32),
        ("rdev_minor", ctypes.c_uint32),
        ("dev_major", ctypes.c_uint32),
        ("dev_minor", ctypes.c_uint32),
        ("mnt_id", ctypes.c_uint64),
        ("dio_mem_align", ctypes.c_uint32),
        ("dio_offset_align", ctypes.c_uint32),
        ("spare3", ctypes.c_uint64 * 12),
    ]


@dataclass(frozen=True)
class ObservePublication:
    action_path: Path
    action_sha256: str
    manifest_path: Path
    manifest_sha256: str


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _validate_component(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise GreenhouseObservePublisherError(f"{label} is invalid")
    return value


def _validate_uuid(value: str, label: str) -> str:
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise GreenhouseObservePublisherError(f"{label} is invalid") from exc
    if value != canonical:
        raise GreenhouseObservePublisherError(f"{label} is not canonical")
    return value


def _open_private_root(path: Path) -> tuple[Path, int]:
    if not path.is_absolute():
        raise GreenhouseObservePublisherError("private root must be absolute")
    try:
        resolved = path.resolve(strict=True)
        linked = os.lstat(path)
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as exc:
        raise GreenhouseObservePublisherError("private root is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            path != resolved
            or stat.S_ISLNK(linked.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (linked.st_dev, linked.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise GreenhouseObservePublisherError(
                "private root must be one canonical owner-controlled 0700 directory"
            )
        return resolved, descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_private_directory(parent_fd: int, component: str) -> int:
    created = False
    previous_umask = os.umask(0o077)
    try:
        try:
            os.mkdir(component, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
    finally:
        os.umask(previous_umask)
    if created:
        os.fsync(parent_fd)
    try:
        descriptor = os.open(
            component,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise GreenhouseObservePublisherError(
            "private directory is unavailable"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise GreenhouseObservePublisherError(
                "private directory must be owner-controlled mode 0700"
            )
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _create_private_directory_once(parent_fd: int, component: str) -> int:
    previous_umask = os.umask(0o077)
    try:
        try:
            os.mkdir(component, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise GreenhouseObservePublisherError(
                "fresh private output identity already exists"
            ) from exc
        except OSError as exc:
            raise GreenhouseObservePublisherError(
                "fresh private output identity creation failed"
            ) from exc
    finally:
        os.umask(previous_umask)
    os.fsync(parent_fd)
    return _open_private_directory(parent_fd, component)


def _validate_birth_matches_ctime(descriptor: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    statx = getattr(libc, "statx", None)
    if statx is None:
        return
    statx.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(_Statx),
    ]
    statx.restype = ctypes.c_int
    result = _Statx()
    if statx(
        descriptor,
        b"",
        _AT_EMPTY_PATH,
        _STATX_BTIME | _STATX_CTIME,
        ctypes.byref(result),
    ) != 0:
        error = ctypes.get_errno()
        if error in {22, 38, 95}:
            return
        raise GreenhouseObservePublisherError("immutable file statx failed")
    if result.mask & _STATX_BTIME and result.btime.tv_sec != result.ctime.tv_sec:
        raise GreenhouseObservePublisherError(
            "immutable file birth and change times do not match"
        )


def _write_immutable_json(parent_fd: int, name: str, value: object) -> tuple[bytes, str]:
    body = canonical_json_bytes(value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    previous_umask = os.umask(0o377)
    try:
        descriptor = os.open(name, flags, 0o400, dir_fd=parent_fd)
    except FileExistsError as exc:
        raise GreenhouseObservePublisherError(
            "immutable transaction artifact already exists"
        ) from exc
    except OSError as exc:
        raise GreenhouseObservePublisherError(
            "immutable transaction artifact creation failed"
        ) from exc
    finally:
        os.umask(previous_umask)
    try:
        created = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(created.st_mode)
            or created.st_uid != os.geteuid()
            or stat.S_IMODE(created.st_mode) != 0o400
            or (linked.st_dev, linked.st_ino) != (created.st_dev, created.st_ino)
        ):
            raise GreenhouseObservePublisherError(
                "immutable transaction artifact was not born owner-controlled mode 0400"
            )
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise GreenhouseObservePublisherError(
                    "immutable transaction artifact write did not advance"
                )
            view = view[written:]
        os.fsync(descriptor)
        final = os.fstat(descriptor)
        if (
            final.st_size != len(body)
            or final.st_uid != os.geteuid()
            or stat.S_IMODE(final.st_mode) != 0o400
        ):
            raise GreenhouseObservePublisherError(
                "immutable transaction artifact changed during publication"
            )
        _validate_birth_matches_ctime(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(parent_fd)
    return body, hashlib.sha256(body).hexdigest()


def validate_observe_inputs(
    *,
    seat_id: str,
    event_id: str,
    correlation_id: str,
    display: str,
    application_identity_sha256: str,
    hands_commit: str,
    transaction_id: str,
    action_id: str,
) -> dict[str, str]:
    return {
        "seat_id": _validate_component(seat_id, _SEAT_ID, "seat ID"),
        "event_id": _validate_component(event_id, _TRACE_ID, "event ID"),
        "correlation_id": _validate_component(
            correlation_id, _TRACE_ID, "correlation ID"
        ),
        "display": _validate_component(display, _DISPLAY, "display"),
        "application_identity_sha256": _validate_component(
            application_identity_sha256,
            _SHA256,
            "application identity digest",
        ),
        "hands_commit": _validate_component(
            hands_commit, _COMMIT, "Hands commit"
        ),
        "transaction_id": _validate_uuid(transaction_id, "transaction ID"),
        "action_id": _validate_uuid(action_id, "action ID"),
    }


def publish_observe_artifacts(
    *,
    private_root: Path,
    seat_id: str,
    event_id: str,
    correlation_id: str,
    display: str,
    application_identity_sha256: str,
    hands_commit: str,
    transaction_id: str,
    action_id: str,
) -> ObservePublication:
    validated = validate_observe_inputs(
        seat_id=seat_id,
        event_id=event_id,
        correlation_id=correlation_id,
        display=display,
        application_identity_sha256=application_identity_sha256,
        hands_commit=hands_commit,
        transaction_id=transaction_id,
        action_id=action_id,
    )
    seat_id = validated["seat_id"]
    event_id = validated["event_id"]
    correlation_id = validated["correlation_id"]
    display = validated["display"]
    application_identity_sha256 = validated["application_identity_sha256"]
    hands_commit = validated["hands_commit"]
    transaction_id = validated["transaction_id"]
    action_id = validated["action_id"]
    resolved_root, root_fd = _open_private_root(private_root)
    action_filename = f"{correlation_id}.json"
    manifest_filename = f"{correlation_id}.json"
    action_dir_fd = transaction_dir_fd = None
    try:
        actions_fd = _open_private_directory(root_fd, "actions")
        try:
            action_dir_fd = _open_private_directory(actions_fd, seat_id)
        finally:
            os.close(actions_fd)
        transactions_fd = _open_private_directory(root_fd, "transactions")
        try:
            transaction_dir_fd = _open_private_directory(transactions_fd, seat_id)
        finally:
            os.close(transactions_fd)
        action = {
            "schema": ACTION_SCHEMA,
            "provider": "greenhouse",
            "transaction_id": transaction_id,
            "action_id": action_id,
            "application_identity_sha256": application_identity_sha256,
            "expected_prior_event_hash": None,
            "action": {"kind": "observe_form"},
        }
        _, action_sha256 = _write_immutable_json(
            action_dir_fd, action_filename, action
        )
        action_path = resolved_root / "actions" / seat_id / action_filename
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "seat_id": seat_id,
            "event_id": event_id,
            "correlation_id": correlation_id,
            "platform": "greenhouse",
            "display": display,
            "hands_commit": hands_commit,
            "frozen_action_path": str(action_path),
            "frozen_action_sha256": action_sha256,
        }
        _, manifest_sha256 = _write_immutable_json(
            transaction_dir_fd, manifest_filename, manifest
        )
        return ObservePublication(
            action_path=action_path,
            action_sha256=action_sha256,
            manifest_path=(
                resolved_root / "transactions" / seat_id / manifest_filename
            ),
            manifest_sha256=manifest_sha256,
        )
    finally:
        if action_dir_fd is not None:
            os.close(action_dir_fd)
        if transaction_dir_fd is not None:
            os.close(transaction_dir_fd)
        os.close(root_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish one fresh immutable Greenhouse observe transaction."
    )
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--seat-id", required=True)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument("--display", required=True)
    parser.add_argument("--application-identity-sha256", required=True)
    parser.add_argument("--hands-commit", required=True)
    parser.add_argument("--transaction-id", required=True)
    parser.add_argument("--action-id", required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        publish_observe_artifacts(**vars(arguments))
    except GreenhouseObservePublisherError as exc:
        raise SystemExit(f"greenhouse observe publication refused: {exc}") from exc
    print("greenhouse observe artifacts published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
