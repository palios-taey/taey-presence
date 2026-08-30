#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

import greenhouse_ats_observe_publisher as publisher


_ENDPOINT_PATH = "/v1/greenhouse-ats/one-action"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GreenhouseObserveLaunchError(RuntimeError):
    pass


def _validated_endpoint(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != _ENDPOINT_PATH
    ):
        raise GreenhouseObserveLaunchError(
            "endpoint must be the exact local Greenhouse one-action route"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise GreenhouseObserveLaunchError("endpoint port is invalid") from exc
    if port is None or not 1 <= port <= 65535:
        raise GreenhouseObserveLaunchError("endpoint port is required")
    return parsed.hostname, port


def _open_private_output(parent_fd: int, name: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    previous_umask = os.umask(0o177)
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    except FileExistsError as exc:
        raise GreenhouseObserveLaunchError(
            "private response output already exists"
        ) from exc
    except OSError as exc:
        raise GreenhouseObserveLaunchError(
            "private response output creation failed"
        ) from exc
    finally:
        os.umask(previous_umask)
    metadata = os.fstat(descriptor)
    linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size != 0
        or (linked.st_dev, linked.st_ino) != (metadata.st_dev, metadata.st_ino)
    ):
        os.close(descriptor)
        raise GreenhouseObserveLaunchError(
            "private response output was not born owner-controlled mode 0600"
        )
    return descriptor


def _prepare_private_outputs(
    private_root: Path,
    seat_id: str,
    correlation_id: str,
) -> tuple[int, int, int]:
    _, root_fd = publisher._open_private_root(private_root)
    outputs_fd = seat_fd = run_fd = None
    try:
        outputs_fd = publisher._open_private_directory(root_fd, "outputs")
        seat_fd = publisher._open_private_directory(outputs_fd, seat_id)
        run_fd = publisher._create_private_directory_once(seat_fd, correlation_id)
        headers_fd = _open_private_output(run_fd, "headers.txt")
        try:
            response_fd = _open_private_output(run_fd, "response.json")
        except Exception:
            os.close(headers_fd)
            raise
        return run_fd, headers_fd, response_fd
    except Exception:
        if run_fd is not None:
            os.close(run_fd)
        raise
    finally:
        if outputs_fd is not None:
            os.close(outputs_fd)
        if seat_fd is not None:
            os.close(seat_fd)
        os.close(root_fd)


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise GreenhouseObserveLaunchError(
                "private response output write did not advance"
            )
        view = view[written:]
    os.fsync(descriptor)


def _encoded_headers(response: http.client.HTTPResponse) -> bytes:
    version = {10: "1.0", 11: "1.1"}.get(response.version, str(response.version))
    lines = [f"HTTP/{version} {response.status} {response.reason}\r\n"]
    lines.extend(f"{name}: {value}\r\n" for name, value in response.getheaders())
    lines.append("\r\n")
    return "".join(lines).encode("iso-8859-1", errors="strict")


def launch_one_observe(arguments: argparse.Namespace) -> int:
    host, port = _validated_endpoint(arguments.endpoint)
    publisher.validate_observe_inputs(
        seat_id=arguments.seat_id,
        event_id=arguments.event_id,
        correlation_id=arguments.correlation_id,
        display=arguments.display,
        application_identity_sha256=arguments.application_identity_sha256,
        hands_commit=arguments.hands_commit,
        transaction_id=arguments.transaction_id,
        action_id=arguments.action_id,
    )
    run_fd = headers_fd = response_fd = None
    connection: http.client.HTTPConnection | None = None
    try:
        run_fd, headers_fd, response_fd = _prepare_private_outputs(
            arguments.private_root,
            arguments.seat_id,
            arguments.correlation_id,
        )
        publisher.publish_observe_artifacts(
            private_root=arguments.private_root,
            seat_id=arguments.seat_id,
            event_id=arguments.event_id,
            correlation_id=arguments.correlation_id,
            display=arguments.display,
            application_identity_sha256=arguments.application_identity_sha256,
            hands_commit=arguments.hands_commit,
            transaction_id=arguments.transaction_id,
            action_id=arguments.action_id,
        )
        body = json.dumps(
            {"display": arguments.display},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Taey-Seat-Id": arguments.seat_id,
            "X-Taey-Event-Id": arguments.event_id,
            "X-Taey-Correlation-Id": arguments.correlation_id,
            "X-Taey-Tool-Profile": "greenhouse-ats-ui",
        }
        connection = http.client.HTTPConnection(host, port, timeout=600)
        connection.request("POST", _ENDPOINT_PATH, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read(_MAX_RESPONSE_BYTES + 1)
        response_headers = _encoded_headers(response)
        if len(response_body) > _MAX_RESPONSE_BYTES:
            raise GreenhouseObserveLaunchError("one-action response exceeded its bound")
        _write_all(headers_fd, response_headers)
        _write_all(response_fd, response_body)
        os.fsync(run_fd)
        if not 200 <= response.status < 300:
            raise GreenhouseObserveLaunchError(
                "one-action endpoint returned a non-success status"
            )
        try:
            result = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GreenhouseObserveLaunchError(
                "one-action endpoint returned non-JSON"
            ) from exc
        if not isinstance(result, dict):
            raise GreenhouseObserveLaunchError(
                "one-action endpoint returned a non-object"
            )
        sequence = result.get("greenhouse_ats_sequence")
        if (
            set(result) != {
                "ok",
                "display",
                "action",
                "greenhouse_ats_sequence",
            }
            or result.get("ok") is not True
            or result.get("display") != arguments.display
            or result.get("action") != "operate"
            or not isinstance(sequence, dict)
            or set(sequence)
            != {
                "state",
                "postcondition_proven",
                "receipt_event_hash",
                "hands_result_sha256",
                "hands_state",
                "mutation_count",
                "hands_next_mutation_authorized",
                "next_mutation_authorized",
                "surface_capsule",
            }
            or sequence.get("state") != "action_receipted"
            or sequence.get("postcondition_proven") is not True
            or _SHA256.fullmatch(str(sequence.get("receipt_event_hash") or ""))
            is None
            or _SHA256.fullmatch(str(sequence.get("hands_result_sha256") or ""))
            is None
            or sequence.get("hands_state") != "action_ready"
            or sequence.get("mutation_count") != 0
            or sequence.get("hands_next_mutation_authorized") is not True
            or sequence.get("next_mutation_authorized") is not False
            or not isinstance(sequence.get("surface_capsule"), dict)
        ):
            raise GreenhouseObserveLaunchError(
                "one-action endpoint returned a refusal or contract mismatch"
            )
        return 0
    finally:
        if connection is not None:
            connection.close()
        for descriptor in (headers_fd, response_fd, run_fd):
            if descriptor is not None:
                os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish and execute exactly one Greenhouse observe action."
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
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:8765/v1/greenhouse-ats/one-action",
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        status = launch_one_observe(arguments)
    except (
        GreenhouseObserveLaunchError,
        publisher.GreenhouseObservePublisherError,
        OSError,
    ) as exc:
        raise SystemExit(f"greenhouse observe launch refused: {exc}") from exc
    print("greenhouse observe one-action request completed")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
