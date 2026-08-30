#!/usr/bin/env python3
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVING_ROOT = REPO_ROOT / "serving"
LAUNCHER = SERVING_ROOT / "launch_greenhouse_ats_observe.py"
APPLICATION_DIGEST = "a" * 64
HANDS_COMMIT = "b" * 40
TRANSACTION_ID = "11111111-1111-4111-8111-111111111111"
ACTION_ID = "22222222-2222-4222-8222-222222222222"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class RequestRecorder(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    next_status = 200
    next_ok: bool | None = None
    next_success_state = "action_receipted"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.__class__.requests.append({
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body,
        })
        response_ok = (
            self.__class__.next_status == 200
            if self.__class__.next_ok is None
            else self.__class__.next_ok
        )
        payload = json.dumps(
            {
                "ok": response_ok,
                "display": ":26",
                "action": "operate",
                "greenhouse_ats_sequence": {
                    "state": (
                        self.__class__.next_success_state
                        if response_ok
                        else "refused"
                    ),
                    **(
                        {
                            "postcondition_proven": True,
                            "receipt_event_hash": "c" * 64,
                            "hands_result_sha256": "d" * 64,
                            "hands_state": "action_ready",
                            "mutation_count": 0,
                            "hands_next_mutation_authorized": True,
                            "next_mutation_authorized": False,
                            "surface_capsule": {"schema": "bounded-test-capsule"},
                        }
                        if response_ok
                        else {}
                    ),
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(self.__class__.next_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def launch_command(root: Path, endpoint: str, correlation: str) -> list[str]:
    return [
        sys.executable,
        str(LAUNCHER),
        "--private-root",
        str(root),
        "--seat-id",
        "greenhouse-canary",
        "--event-id",
        correlation,
        "--correlation-id",
        correlation,
        "--display",
        ":26",
        "--application-identity-sha256",
        APPLICATION_DIGEST,
        "--hands-commit",
        HANDS_COMMIT,
        "--transaction-id",
        TRANSACTION_ID,
        "--action-id",
        ACTION_ID,
        "--endpoint",
        endpoint,
    ]


def run_launcher(root: Path, endpoint: str, correlation: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        launch_command(root, endpoint, correlation),
        capture_output=True,
        text=True,
        timeout=20,
    )


def canonical_bytes(value: object) -> bytes:
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


def require_private_file(path: Path, mode: int) -> bytes:
    linked = os.lstat(path)
    require(stat.S_ISREG(linked.st_mode), f"{path.name} is not regular")
    require(linked.st_uid == os.geteuid(), f"{path.name} owner drifted")
    require(stat.S_IMODE(linked.st_mode) == mode, f"{path.name} mode drifted")
    return path.read_bytes()


def require_birth_matches_ctime(path: Path) -> None:
    completed = subprocess.run(
        ["stat", "-c", "%W:%Z", "--", str(path)],
        capture_output=True,
        check=True,
        text=True,
    )
    birth, changed = completed.stdout.strip().split(":", 1)
    if int(birth) > 0:
        require(birth == changed, f"{path.name} was not immutable from birth")


def validate_published_contract(root: Path, correlation: str) -> None:
    action_path = root / "actions" / "greenhouse-canary" / f"{correlation}.json"
    manifest_path = (
        root / "transactions" / "greenhouse-canary" / f"{correlation}.json"
    )
    action_raw = require_private_file(action_path, 0o400)
    manifest_raw = require_private_file(manifest_path, 0o400)
    require_birth_matches_ctime(action_path)
    require_birth_matches_ctime(manifest_path)
    action = json.loads(action_raw)
    expected_action = {
        "schema": "ats_greenhouse_frozen_action_v1",
        "provider": "greenhouse",
        "transaction_id": TRANSACTION_ID,
        "action_id": ACTION_ID,
        "application_identity_sha256": APPLICATION_DIGEST,
        "expected_prior_event_hash": None,
        "action": {"kind": "observe_form"},
    }
    require(action == expected_action, "frozen observe action schema drifted")
    require(action_raw == canonical_bytes(expected_action), "action is not canonical")
    action_digest = hashlib.sha256(action_raw).hexdigest()
    expected_manifest = {
        "schema": "taey_greenhouse_ats_private_manifest_v1",
        "seat_id": "greenhouse-canary",
        "event_id": correlation,
        "correlation_id": correlation,
        "platform": "greenhouse",
        "display": ":26",
        "hands_commit": HANDS_COMMIT,
        "frozen_action_path": str(action_path),
        "frozen_action_sha256": action_digest,
    }
    require(json.loads(manifest_raw) == expected_manifest, "manifest schema drifted")
    require(
        manifest_raw == canonical_bytes(expected_manifest),
        "manifest is not canonical",
    )
    output = root / "outputs" / "greenhouse-canary" / correlation
    require(
        stat.S_IMODE(os.lstat(output).st_mode) == 0o700,
        "private output directory mode drifted",
    )
    headers = require_private_file(output / "headers.txt", 0o600)
    response = require_private_file(output / "response.json", 0o600)
    require(headers.startswith(b"HTTP/1.0 200 OK\r\n"), "response headers were not stored")
    require(json.loads(response)["ok"] is True, "response body was not stored")


def validate_exact_request(request: dict[str, object], correlation: str) -> None:
    require(request["method"] == "POST", "launcher did not use POST")
    require(
        request["path"] == "/v1/greenhouse-ats/one-action",
        "launcher called the wrong endpoint",
    )
    require(request["body"] == b'{"display":":26"}', "request body drifted")
    headers = request["headers"]
    require(isinstance(headers, dict), "request headers were not captured")
    expected = {
        "Content-Type": "application/json",
        "X-Taey-Seat-Id": "greenhouse-canary",
        "X-Taey-Event-Id": correlation,
        "X-Taey-Correlation-Id": correlation,
        "X-Taey-Tool-Profile": "greenhouse-ats-ui",
    }
    for name, value in expected.items():
        require(headers.get(name) == value, f"{name} drifted")
    require("Authorization" not in headers, "launcher transmitted an authorization secret")


def validate_refusals(endpoint: str, baseline_requests: int) -> None:
    with tempfile.TemporaryDirectory(prefix="greenhouse-observe-refusal-") as temporary:
        root = Path(temporary) / "private"
        root.mkdir(mode=0o700)
        root.chmod(0o700)
        invalid = launch_command(root, endpoint, "invalid-digest")
        digest_index = invalid.index("--application-identity-sha256") + 1
        invalid[digest_index] = "not-a-digest"
        rejected = subprocess.run(invalid, capture_output=True, text=True, timeout=20)
        require(rejected.returncode != 0, "invalid digest was accepted")
        require(
            not (root / "outputs").exists(),
            "invalid identity created private output",
        )
        require(
            len(RequestRecorder.requests) == baseline_requests,
            "invalid identity reached the endpoint",
        )

    with tempfile.TemporaryDirectory(prefix="greenhouse-observe-mode-") as temporary:
        root = Path(temporary) / "private"
        root.mkdir(mode=0o755)
        root.chmod(0o755)
        rejected = run_launcher(root, endpoint, "bad-root-mode")
        require(rejected.returncode != 0, "unsafe private root was accepted")
        require(
            len(RequestRecorder.requests) == baseline_requests,
            "unsafe private root reached the endpoint",
        )

    with tempfile.TemporaryDirectory(prefix="greenhouse-observe-link-") as temporary:
        parent = Path(temporary)
        root = parent / "private"
        outside = parent / "outside"
        root.mkdir(mode=0o700)
        outside.mkdir(mode=0o700)
        root.chmod(0o700)
        outside.chmod(0o700)
        (root / "outputs").symlink_to(outside, target_is_directory=True)
        rejected = run_launcher(root, endpoint, "symlink-output")
        require(rejected.returncode != 0, "symlinked output root was accepted")
        require(
            len(RequestRecorder.requests) == baseline_requests,
            "symlinked output root reached the endpoint",
        )


def main() -> int:
    source = LAUNCHER.read_text(encoding="utf-8")
    require(
        source.count('connection.request("POST", _ENDPOINT_PATH') == 1,
        "launcher does not contain one exact POST call",
    )
    require("Authorization" not in source, "launcher source contains authorization")
    RequestRecorder.requests = []
    RequestRecorder.next_status = 200
    RequestRecorder.next_ok = None
    RequestRecorder.next_success_state = "action_receipted"
    server = ThreadingHTTPServer(("127.0.0.1", 0), RequestRecorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = (
        f"http://127.0.0.1:{server.server_address[1]}"
        "/v1/greenhouse-ats/one-action"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="greenhouse-observe-valid-") as temporary:
            root = Path(temporary) / "private"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            correlation = "greenhouse-observe-valid"
            completed = run_launcher(root, endpoint, correlation)
            require(
                completed.returncode == 0,
                f"valid launch failed: {completed.stderr}",
            )
            require(
                completed.stdout == "greenhouse observe one-action request completed\n",
                "launcher exposed unexpected output",
            )
            require(len(RequestRecorder.requests) == 1, "launcher did not call once")
            validate_exact_request(RequestRecorder.requests[0], correlation)
            validate_published_contract(root, correlation)
            repeated = run_launcher(root, endpoint, correlation)
            require(repeated.returncode != 0, "duplicate identity was accepted")
            require(
                len(RequestRecorder.requests) == 1,
                "duplicate identity retried the endpoint",
            )

        validate_refusals(endpoint, 1)

        with tempfile.TemporaryDirectory(prefix="greenhouse-observe-http-") as temporary:
            root = Path(temporary) / "private"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            RequestRecorder.next_status = 503
            failed = run_launcher(root, endpoint, "http-failure")
            require(failed.returncode != 0, "non-success endpoint was accepted")
            require(
                len(RequestRecorder.requests) == 2,
                "non-success endpoint was retried",
            )
            output = root / "outputs" / "greenhouse-canary" / "http-failure"
            require_private_file(output / "headers.txt", 0o600)
            require_private_file(output / "response.json", 0o600)

        with tempfile.TemporaryDirectory(prefix="greenhouse-observe-refused-") as temporary:
            root = Path(temporary) / "private"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            RequestRecorder.next_status = 200
            RequestRecorder.next_ok = False
            refused = run_launcher(root, endpoint, "endpoint-refusal")
            require(refused.returncode != 0, "200 endpoint refusal was accepted")
            require(
                len(RequestRecorder.requests) == 3,
                "200 endpoint refusal was retried",
            )
            output = root / "outputs" / "greenhouse-canary" / "endpoint-refusal"
            require_private_file(output / "headers.txt", 0o600)
            require_private_file(output / "response.json", 0o600)

        with tempfile.TemporaryDirectory(prefix="greenhouse-observe-contract-") as temporary:
            root = Path(temporary) / "private"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            RequestRecorder.next_status = 200
            RequestRecorder.next_ok = True
            RequestRecorder.next_success_state = "unexpected_success"
            mismatched = run_launcher(root, endpoint, "contract-mismatch")
            require(mismatched.returncode != 0, "200 contract mismatch was accepted")
            require(
                len(RequestRecorder.requests) == 4,
                "200 contract mismatch was retried",
            )
            output = root / "outputs" / "greenhouse-canary" / "contract-mismatch"
            require_private_file(output / "headers.txt", 0o600)
            require_private_file(output / "response.json", 0o600)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("Greenhouse observe publisher and one-action launcher: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
