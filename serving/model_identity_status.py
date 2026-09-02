#!/usr/bin/env python3
"""Read and verify the serving-owned Taey model identity publication."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
import json
import os
import re
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit

CONTRACT = "taey-serving-model-identity-receipt/v1"
PUBLICATION_CONTRACT = "taey-serving-model-identity-publication/v1"
AUTHORITY_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")
SYSTEMD_UNIT = "taey-ep3.service"
ED25519_SPKI_DER_PREFIX = bytes.fromhex("302a300506032b6570032100")
ENVIRONMENT_KEYS = {
    "TAEY_MODEL_IDENTITY_AUTHORITY_ID",
    "TAEY_MODEL_IDENTITY_REDIS_HOST",
    "TAEY_MODEL_IDENTITY_REDIS_PORT",
    "TAEY_MODEL_IDENTITY_UPSTREAM_COMPLETION_ENDPOINT",
}


class VerificationError(RuntimeError):
    pass


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def effective_unit_sha256(unit: str) -> str:
    result = subprocess.run(
        ["systemctl", "cat", unit],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise VerificationError(f"effective systemd configuration is unavailable: {unit}")
    return "sha256:" + hashlib.sha256(result.stdout).hexdigest()


def read_redis_value(response: Any) -> Any:
    prefix = response.read(1)
    line = response.readline(4096)
    if prefix == b"+":
        return line.removesuffix(b"\r\n").decode("utf-8")
    if prefix == b"-":
        raise VerificationError(line.decode(errors="replace").strip())
    if prefix == b":":
        return int(line)
    if prefix == b"$":
        length = int(line)
        if length == -1:
            return None
        value = response.read(length)
        if response.read(2) != b"\r\n":
            raise VerificationError("Redis returned a malformed bulk value")
        return value.decode("utf-8")
    if prefix == b"*":
        return [read_redis_value(response) for _ in range(int(line))]
    raise VerificationError("Redis returned an unknown response type")


def redis_command(host: str, port: int, parts: list[str]) -> Any:
    wire = bytearray(f"*{len(parts)}\r\n".encode("ascii"))
    for part in parts:
        encoded = part.encode("utf-8")
        wire.extend(f"${len(encoded)}\r\n".encode("ascii"))
        wire.extend(encoded)
        wire.extend(b"\r\n")
    try:
        with socket.create_connection((host, port), timeout=5) as connection:
            connection.sendall(wire)
            with connection.makefile("rb") as response:
                return read_redis_value(response)
    except OSError as error:
        raise VerificationError(f"Redis is unavailable: {error}") from error


def redis_configuration() -> dict[str, Any]:
    host = os.environ.get("TAEY_MODEL_IDENTITY_REDIS_HOST", "")
    authority_id = os.environ.get("TAEY_MODEL_IDENTITY_AUTHORITY_ID", "")
    if not host or AUTHORITY_RE.fullmatch(authority_id) is None:
        raise VerificationError("Redis host and a valid authority ID are required")
    port = int(os.environ.get("TAEY_MODEL_IDENTITY_REDIS_PORT", "6379"))
    if not 1 <= port <= 65535:
        raise VerificationError("Redis port is out of range")
    return {
        "authority_id": authority_id,
        "host": host,
        "key": f"taey:serving:model_identity:{authority_id}",
        "port": port,
    }


def load_environment_file(path: Path) -> None:
    if not path.is_file():
        raise VerificationError("model identity environment file is missing")
    parsed: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise VerificationError("model identity environment file has an invalid line")
        key, value = stripped.split("=", 1)
        if key not in ENVIRONMENT_KEYS or key in parsed or not value:
            raise VerificationError("model identity environment file has an invalid assignment")
        parsed[key] = value
    required = {
        "TAEY_MODEL_IDENTITY_AUTHORITY_ID",
        "TAEY_MODEL_IDENTITY_REDIS_HOST",
    }
    if not required.issubset(parsed):
        raise VerificationError("model identity environment file is incomplete")
    for key, value in parsed.items():
        existing = os.environ.get(key)
        if existing is not None and existing != value:
            raise VerificationError("model identity environment conflicts with the process")
        os.environ[key] = value


def verify_signature(publication: dict[str, Any], authority_id: str) -> None:
    public_key = Path(__file__).with_name("trust") / f"{authority_id}.pub.pem"
    if not public_key.is_file():
        raise VerificationError("model identity public trust root is missing")
    derived = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", str(public_key), "-outform", "DER"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if derived.returncode != 0:
        raise VerificationError("model identity public trust root is invalid")
    if (
        len(derived.stdout) != len(ED25519_SPKI_DER_PREFIX) + 32
        or not derived.stdout.startswith(ED25519_SPKI_DER_PREFIX)
    ):
        raise VerificationError("model identity public trust root must be Ed25519")
    fingerprint = "sha256:" + hashlib.sha256(derived.stdout).hexdigest()
    if publication["signing_key_sha256"] != fingerprint:
        raise VerificationError("model identity signing key is not the trusted key")
    try:
        signature = base64.b64decode(publication["signature_base64"], validate=True)
    except (TypeError, ValueError) as error:
        raise VerificationError("model identity publication signature is invalid") from error
    signed = {key: value for key, value in publication.items() if key != "signature_base64"}
    with tempfile.NamedTemporaryFile() as signature_file:
        signature_file.write(signature)
        signature_file.flush()
        verified = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-rawin",
                "-pubin",
                "-inkey",
                str(public_key),
                "-sigfile",
                signature_file.name,
            ],
            input=json.dumps(
                signed,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8"),
            capture_output=True,
            timeout=10,
            check=False,
        )
    if verified.returncode != 0:
        raise VerificationError("model identity publication signature did not verify")


def verify_implementation(implementation: Any) -> None:
    expected_fields = {
        "attestor_sha256",
        "attestor_service_unit_sha256",
        "effective_attestor_service_sha256",
        "effective_serving_service_sha256",
        "public_trust_root_sha256",
        "serve_launcher_sha256",
        "serving_service_unit_sha256",
    }
    if not isinstance(implementation, dict) or set(implementation) != expected_fields:
        raise VerificationError("model identity implementation is invalid")
    if any(not is_sha256(value) for value in implementation.values()):
        raise VerificationError("model identity implementation digest is invalid")


def verify_host_implementation(implementation: dict[str, str], authority_id: str) -> None:
    root = Path(__file__).resolve(strict=True).parent
    expected = {
        "attestor_sha256": file_sha256(root / "model_identity_attestor.py"),
        "attestor_service_unit_sha256": file_sha256(
            Path("/etc/systemd/system/taey-model-identity-attestor.service")
        ),
        "effective_attestor_service_sha256": effective_unit_sha256(
            "taey-model-identity-attestor.service"
        ),
        "effective_serving_service_sha256": effective_unit_sha256(SYSTEMD_UNIT),
        "public_trust_root_sha256": file_sha256(
            root / "trust" / f"{authority_id}.pub.pem"
        ),
        "serve_launcher_sha256": file_sha256(root / "vllm_serve.sh"),
        "serving_service_unit_sha256": file_sha256(
            Path("/etc/systemd/system/taey-ep3.service")
        ),
    }
    if implementation != expected:
        raise VerificationError("model identity implementation differs from this host")


def verify_artifact(model: Any) -> None:
    if not isinstance(model, dict) or set(model) != {
        "artifact_file_count",
        "artifact_fence_sha256",
        "artifact_seal_sha256",
        "host_tree_read_only",
        "model_content_sha256",
        "model_manifest_file",
        "model_manifest_sha256",
        "seal_contract",
        "symlink_count",
    }:
        raise VerificationError("model identity artifact is invalid")
    for field in (
        "artifact_seal_sha256",
        "artifact_fence_sha256",
        "model_content_sha256",
        "model_manifest_sha256",
    ):
        if not is_sha256(model[field]):
            raise VerificationError("model identity artifact digest is invalid")
    manifest = model["model_manifest_file"]
    if (
        not isinstance(manifest, str)
        or not manifest
        or Path(manifest).is_absolute()
        or ".." in Path(manifest).parts
        or any(character in manifest for character in ("\\", "\n", "\r"))
    ):
        raise VerificationError("model identity manifest path is invalid")
    if (
        model["host_tree_read_only"] is not True
        or model["symlink_count"] != 0
        or model["seal_contract"] != "taey-complete-artifact-sha256sum/v1"
        or not isinstance(model["artifact_file_count"], int)
        or isinstance(model["artifact_file_count"], bool)
        or model["artifact_file_count"] <= 1
    ):
        raise VerificationError("model identity artifact invariant is invalid")


def verify_endpoint(endpoint: Any, path: str) -> None:
    if not isinstance(endpoint, str):
        raise VerificationError("model identity endpoint is invalid")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as error:
        raise VerificationError("model identity endpoint is invalid") from error
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or port is None
        or parsed.path != path
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise VerificationError("model identity endpoint is invalid")


def verify_serving(serving: Any, expected_aliases: list[str]) -> None:
    if not isinstance(serving, dict) or set(serving) != {
        "aliases",
        "completion_endpoint",
        "container_command",
        "container_id",
        "container_name",
        "container_started_at",
        "container_started_at_unix_ns",
        "image_digest",
        "image_reference",
        "model_container_root",
        "model_mount_read_only",
        "model_mount_root",
        "models_endpoint",
        "provenance_labels",
        "service",
        "vllm_environment",
    }:
        raise VerificationError("model identity serving record is invalid")
    aliases = serving["aliases"]
    if (
        not isinstance(aliases, list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"id", "max_model_len", "owned_by", "root"}
            or not isinstance(item["id"], str)
            or not item["id"]
            or not isinstance(item["max_model_len"], int)
            or isinstance(item["max_model_len"], bool)
            or item["max_model_len"] <= 0
            or not isinstance(item["owned_by"], str)
            or not item["owned_by"]
            or not isinstance(item["root"], str)
            for item in aliases
        )
        or aliases != sorted(aliases, key=lambda item: item["id"])
        or len({item["id"] for item in aliases}) != len(aliases)
        or [item["id"] for item in aliases] != expected_aliases
    ):
        raise VerificationError("model identity aliases are not canonical")
    model_root = serving["model_container_root"]
    if (
        not isinstance(model_root, str)
        or not model_root.startswith("/models/")
        or any(item["root"] != model_root for item in aliases)
        or serving["model_mount_root"] != "/models"
        or serving["model_mount_read_only"] is not True
    ):
        raise VerificationError("model identity serving root is invalid")
    verify_endpoint(serving["completion_endpoint"], "/v1/chat/completions")
    verify_endpoint(serving["models_endpoint"], "/v1/models")
    if urlsplit(serving["completion_endpoint"]).netloc != urlsplit(
        serving["models_endpoint"]
    ).netloc:
        raise VerificationError("model identity endpoints name different authorities")
    command = serving["container_command"]
    if not isinstance(command, list) or not command or any(
        not isinstance(item, str) for item in command
    ) or model_root not in command:
        raise VerificationError("model identity container command is invalid")
    started_at = serving["container_started_at"]
    try:
        derived_started_at_unix_ns = (
            int(
                datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
                * 1_000_000_000
            )
            if isinstance(started_at, str)
            else 0
        )
    except ValueError:
        derived_started_at_unix_ns = 0
    if (
        not isinstance(serving["container_id"], str)
        or CONTAINER_ID_RE.fullmatch(serving["container_id"]) is None
        or serving["container_name"] != "taey-vllm"
        or not isinstance(started_at, str)
        or not started_at
        or not isinstance(serving["container_started_at_unix_ns"], int)
        or isinstance(serving["container_started_at_unix_ns"], bool)
        or serving["container_started_at_unix_ns"] <= 0
        or serving["container_started_at_unix_ns"] != derived_started_at_unix_ns
        or not is_sha256(serving["image_digest"])
        or not isinstance(serving["image_reference"], str)
        or re.fullmatch(r".+@sha256:[0-9a-f]{64}", serving["image_reference"]) is None
        or serving["vllm_environment"] != {"VLLM_ALLOW_RUNTIME_LORA_UPDATING": "0"}
    ):
        raise VerificationError("model identity live container is invalid")
    service = serving["service"]
    if (
        not isinstance(service, dict)
        or set(service) != {"invocation_id", "main_pid", "unit"}
        or not isinstance(service["invocation_id"], str)
        or re.fullmatch(r"[0-9a-f]{32}", service["invocation_id"]) is None
        or not isinstance(service["main_pid"], int)
        or isinstance(service["main_pid"], bool)
        or service["main_pid"] <= 0
        or service["unit"] != SYSTEMD_UNIT
    ):
        raise VerificationError("model identity systemd process is invalid")
    labels = serving["provenance_labels"]
    if (
        not isinstance(labels, dict)
        or set(labels) != {
            "palios.taey.serve-launcher-sha256",
            "palios.taey.systemd-invocation-id",
        }
        or labels["palios.taey.systemd-invocation-id"] != service["invocation_id"]
        or not isinstance(labels["palios.taey.serve-launcher-sha256"], str)
        or re.fullmatch(
            r"[0-9a-f]{64}", labels["palios.taey.serve-launcher-sha256"]
        ) is None
    ):
        raise VerificationError("model identity provenance labels are invalid")


def verify_host_service(serving: dict[str, Any]) -> None:
    service = serving["service"]
    live_service = subprocess.run(
        ["systemctl", "show", SYSTEMD_UNIT, "-p", "InvocationID", "-p", "MainPID"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    live_values = dict(
        line.split("=", 1)
        for line in live_service.stdout.splitlines()
        if "=" in line
    )
    if live_service.returncode != 0 or live_values != {
        "InvocationID": service["invocation_id"],
        "MainPID": str(service["main_pid"]),
    }:
        raise VerificationError("model identity receipt is not the live systemd process")


def read_publication(
    expected_aliases: list[str],
    expected_completion_endpoint: str,
    host_local: bool,
) -> tuple[dict, int]:
    config = redis_configuration()
    script = (
        "local now=redis.call('TIME');"
        "return {redis.call('GET',KEYS[1]),redis.call('PTTL',KEYS[1]),now[1],now[2]}"
    )
    result = redis_command(
        config["host"],
        config["port"],
        ["EVAL", script, "1", config["key"]],
    )
    if not isinstance(result, list) or len(result) != 4 or not isinstance(result[0], str):
        raise VerificationError("model identity publication is absent")
    try:
        publication = json.loads(result[0])
    except json.JSONDecodeError as error:
        raise VerificationError("model identity publication is not JSON") from error
    if not isinstance(publication, dict) or set(publication) != {
        "attestor_generation",
        "authority_id",
        "contract",
        "published_at_redis_unix_ns",
        "receipt",
        "receipt_sha256",
        "signature_base64",
        "signing_key_sha256",
    }:
        raise VerificationError("model identity publication has the wrong field set")
    if publication["contract"] != PUBLICATION_CONTRACT:
        raise VerificationError("model identity publication contract is unsupported")
    if publication["authority_id"] != config["authority_id"]:
        raise VerificationError("model identity publication authority is mismatched")
    if (
        not isinstance(publication["attestor_generation"], str)
        or re.fullmatch(r"[0-9a-f]{32}", publication["attestor_generation"])
        is None
    ):
        raise VerificationError("model identity publication generation is invalid")
    if (
        not isinstance(publication["published_at_redis_unix_ns"], int)
        or isinstance(publication["published_at_redis_unix_ns"], bool)
    ):
        raise VerificationError("model identity publication timestamp is invalid")
    verify_signature(publication, config["authority_id"])
    receipt = publication["receipt"]
    if not isinstance(receipt, dict) or receipt.get("contract") != CONTRACT:
        raise VerificationError("model identity receipt contract is unsupported")
    if set(receipt) != {
        "contract",
        "implementation",
        "model",
        "publisher",
        "receipt_sha256",
        "serving",
    }:
        raise VerificationError("model identity receipt has the wrong field set")
    publisher = receipt["publisher"]
    if not isinstance(publisher, dict) or publisher != {
        "authority_id": config["authority_id"],
        "audience": "taey-native-dcm/v2",
        "component": "taey-model-identity-attestor",
        "host_identity_sha256": publisher.get("host_identity_sha256"),
        "redis_key": config["key"],
    }:
        raise VerificationError("model identity receipt publisher is invalid")
    if not is_sha256(publisher["host_identity_sha256"]):
        raise VerificationError("model identity host identity is invalid")
    stated_digest = receipt.get("receipt_sha256")
    unhashed = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if stated_digest != canonical_sha256(unhashed):
        raise VerificationError("model identity receipt digest is invalid")
    if publication["receipt_sha256"] != stated_digest:
        raise VerificationError("publication and receipt digests differ")
    verify_implementation(receipt["implementation"])
    verify_artifact(receipt["model"])
    verify_serving(receipt["serving"], expected_aliases)
    verify_endpoint(expected_completion_endpoint, "/v1/chat/completions")
    if receipt["serving"]["completion_endpoint"] != expected_completion_endpoint:
        raise VerificationError("model identity completion endpoint is mismatched")
    if receipt["serving"]["provenance_labels"][
        "palios.taey.serve-launcher-sha256"
    ] != receipt["implementation"]["serve_launcher_sha256"].removeprefix("sha256:"):
        raise VerificationError("model identity launcher provenance is inconsistent")
    if host_local:
        verify_host_implementation(receipt["implementation"], config["authority_id"])
        verify_host_service(receipt["serving"])
    ttl_ms = result[1]
    if (
        not isinstance(ttl_ms, int)
        or isinstance(ttl_ms, bool)
        or not 6000 <= ttl_ms <= 15000
    ):
        raise VerificationError("model identity publication has insufficient remaining TTL")
    if not all(isinstance(item, str) and item.isdigit() for item in result[2:]):
        raise VerificationError("Redis server time is invalid")
    redis_now_ns = int(result[2]) * 1_000_000_000 + int(result[3]) * 1_000
    publication_age_ns = redis_now_ns - publication["published_at_redis_unix_ns"]
    if not -1_000_000_000 <= publication_age_ns <= 7_000_000_000:
        raise VerificationError("model identity publication is stale or future-dated")
    return publication, ttl_ms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-file", type=Path)
    parser.add_argument("--expected-completion-endpoint")
    parser.add_argument("--host-local", action="store_true")
    parser.add_argument("--served-name", required=True)
    args = parser.parse_args()
    if args.environment_file is not None:
        load_environment_file(args.environment_file)
    expected_aliases = sorted(args.served_name.split())
    if not expected_aliases or len(expected_aliases) != len(set(expected_aliases)):
        raise VerificationError("expected served aliases are invalid")
    expected_completion_endpoint = args.expected_completion_endpoint or os.environ.get(
        "TAEY_MODEL_IDENTITY_UPSTREAM_COMPLETION_ENDPOINT", ""
    )
    if not expected_completion_endpoint:
        raise VerificationError("expected completion endpoint is required")
    verify_endpoint(expected_completion_endpoint, "/v1/chat/completions")
    publication, ttl_ms = read_publication(
        expected_aliases,
        expected_completion_endpoint,
        args.host_local,
    )
    receipt = publication["receipt"]
    print(
        json.dumps(
            {
                "authority_id": publication["authority_id"],
                "model_identity_receipt_sha256": publication["receipt_sha256"],
                "ok": True,
                "pttl_ms": ttl_ms,
                "served_aliases": receipt["serving"]["aliases"],
                "serving_process": receipt["serving"]["service"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (VerificationError, OSError, TypeError, ValueError) as error:
        print(json.dumps({"error": str(error), "ok": False}, sort_keys=True))
        sys.exit(1)
