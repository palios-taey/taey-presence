#!/usr/bin/env python3
"""Publish one serving-owned, short-lived identity receipt for the live Taey model."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
import uuid


CONTRACT = "taey-serving-model-identity-receipt/v1"
PUBLICATION_CONTRACT = "taey-serving-model-identity-publication/v1"
CONTAINER_NAME = "taey-vllm"
SYSTEMD_UNIT = "taey-ep3.service"
SEAL_NAME = "ARTIFACT_SHA256SUMS"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
KEY_RE = re.compile(r"[A-Za-z0-9:_-]{1,200}")
AUTHORITY_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
TTL_SECONDS = 15
HEARTBEAT_SECONDS = 3
ED25519_SPKI_DER_PREFIX = bytes.fromhex("302a300506032b6570032100")


class AttestationError(RuntimeError):
    pass


class AttestorStopped(Exception):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


HASH_CHUNK_BYTES = 8 * 1024 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_fence(root: Path) -> tuple[list[Path], list[dict[str, Any]]]:
    paths: list[Path] = []
    fence: list[dict[str, Any]] = []
    for path in [root, *root.rglob("*")]:
        metadata = path.lstat()
        mode = metadata.st_mode
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(mode):
            raise AttestationError(f"artifact contains a symlink: {relative}")
        if mode & 0o222:
            raise AttestationError(f"artifact entry is writable: {relative}")
        entry_type = "directory" if stat.S_ISDIR(mode) else "file"
        if entry_type == "file" and not stat.S_ISREG(mode):
            raise AttestationError(
                f"artifact contains a non-regular entry: {relative}"
            )
        if any(character in relative for character in ("\\", "\n", "\r")):
            raise AttestationError(f"artifact path cannot be hashed canonically: {relative!r}")
        fence.append(
            {
                "ctime_ns": metadata.st_ctime_ns,
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "mode": stat.S_IMODE(mode),
                "mtime_ns": metadata.st_mtime_ns,
                "path": relative,
                "size_bytes": metadata.st_size,
                "type": entry_type,
            }
        )
        if entry_type == "file":
            paths.append(path)
    if not paths:
        raise AttestationError("artifact contains no regular files")
    return (
        sorted(paths, key=lambda item: item.relative_to(root).as_posix()),
        sorted(fence, key=lambda item: item["path"]),
    )


def immutable_regular_files(root: Path) -> list[Path]:
    return artifact_fence(root)[0]


def canonical_seal_expected(
    root: Path, paths: list[Path]
) -> dict[str, str]:
    seal = root / SEAL_NAME
    if seal not in paths:
        raise AttestationError(f"artifact is missing {SEAL_NAME}")
    expected: dict[str, str] = {}
    for line in seal.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise AttestationError(f"{SEAL_NAME} contains a non-canonical line")
        digest, name = match.group(1), match.group(2)
        if name.startswith("./"):
            name = name[2:]
        candidate = Path(name)
        if candidate.is_absolute() or ".." in candidate.parts or name in expected:
            raise AttestationError(f"{SEAL_NAME} contains an unsafe or duplicate path")
        expected[name] = digest
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in paths
        if path.name != SEAL_NAME or path.parent != root
    }
    if set(expected) != actual_paths:
        raise AttestationError(f"{SEAL_NAME} does not cover the exact artifact file set")
    return expected


def sealed_file_digests(
    root: Path, paths: list[Path], expected: dict[str, str]
) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        digest = file_sha256(path)
        expected_digest = expected.get(relative)
        if expected_digest is not None and digest != expected_digest:
            raise AttestationError(f"artifact seal verification failed: {relative}")
        digests[relative] = digest
    return digests


def parse_seal(root: Path, paths: list[Path]) -> None:
    sealed_file_digests(root, paths, canonical_seal_expected(root, paths))


def artifact_identity(
    root: Path, manifest_name: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    paths, fence_before = artifact_fence(root)
    expected = canonical_seal_expected(root, paths)
    manifest_path = root / manifest_name
    if manifest_path not in paths:
        raise AttestationError(f"artifact manifest is missing: {manifest_name}")
    digests = sealed_file_digests(root, paths, expected)
    aggregate = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        aggregate.update(
            f"{digests[relative]}  ./{relative}\n".encode("utf-8")
        )
    paths_after, fence_after = artifact_fence(root)
    if paths_after != paths or fence_after != fence_before:
        raise AttestationError("artifact identity changed while it was being hashed")
    seal_relative = SEAL_NAME
    manifest_relative = manifest_path.relative_to(root).as_posix()
    return {
        "artifact_file_count": len(paths),
        "artifact_fence_sha256": canonical_sha256(fence_before),
        "artifact_seal_sha256": "sha256:" + digests[seal_relative],
        "host_tree_read_only": True,
        "model_manifest_file": manifest_name,
        "model_manifest_sha256": "sha256:" + digests[manifest_relative],
        "model_content_sha256": "sha256:" + aggregate.hexdigest(),
        "seal_contract": "taey-complete-artifact-sha256sum/v1",
        "symlink_count": 0,
    }, fence_before


def command_json(command: list[str], *, timeout: int = 30) -> Any:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AttestationError(f"command failed ({' '.join(command)}): {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AttestationError(f"command did not return JSON: {' '.join(command)}") from error


def live_models(models_endpoint: str) -> list[dict[str, Any]]:
    try:
        with urlopen(models_endpoint, timeout=10) as response:
            payload = json.load(response)
    except Exception as error:
        raise AttestationError(f"model catalogue is unavailable: {error}") from error
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise AttestationError("model catalogue has no data array")
    models: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise AttestationError("model catalogue contains an invalid model record")
        root = item.get("root")
        max_model_len = item.get("max_model_len")
        owned_by = item.get("owned_by")
        if not isinstance(root, str) or not root.startswith("/models/"):
            raise AttestationError("model catalogue does not expose an exact /models root")
        if not isinstance(max_model_len, int) or max_model_len <= 0:
            raise AttestationError("model catalogue has no exact positive max_model_len")
        if not isinstance(owned_by, str) or not owned_by:
            raise AttestationError("model catalogue has no exact owner")
        models.append(
            {
                "id": item["id"],
                "max_model_len": max_model_len,
                "owned_by": owned_by,
                "root": root,
            }
        )
    return sorted(models, key=lambda item: item["id"])


def systemd_identity() -> dict[str, Any]:
    result = subprocess.run(
        ["systemctl", "show", SYSTEMD_UNIT, "-p", "InvocationID", "-p", "MainPID"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    values = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )
    generation = values.get("InvocationID", "")
    try:
        main_pid = int(values.get("MainPID", "0"))
    except ValueError:
        main_pid = 0
    if (
        result.returncode != 0
        or re.fullmatch(r"[0-9a-f]{32}", generation) is None
        or main_pid <= 0
    ):
        raise AttestationError("taey-ep3 has no exact live systemd InvocationID")
    return {"invocation_id": generation, "main_pid": main_pid, "unit": SYSTEMD_UNIT}


def effective_unit_sha256(unit: str) -> str:
    result = subprocess.run(
        ["systemctl", "cat", unit],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise AttestationError(f"effective systemd configuration is unavailable: {unit}")
    return "sha256:" + hashlib.sha256(result.stdout).hexdigest()


def runtime_identity(
    root: Path,
    aliases: list[str],
    local_models_endpoint: str,
    upstream_models_endpoint: str,
    upstream_completion_endpoint: str,
) -> dict[str, Any]:
    inspected = command_json(["docker", "inspect", CONTAINER_NAME])
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise AttestationError("docker inspect did not return exactly one serving container")
    container = inspected[0]
    if container.get("State", {}).get("Running") is not True:
        raise AttestationError("serving container is not running")
    if container.get("State", {}).get("Health", {}).get("Status") != "healthy":
        raise AttestationError("serving container is not healthy")
    container_id = container.get("Id")
    image_digest = container.get("Image")
    image_reference = container.get("Config", {}).get("Image")
    started_at = container.get("State", {}).get("StartedAt")
    if not isinstance(container_id, str) or SHA256_RE.fullmatch(container_id) is None:
        raise AttestationError("serving container has no exact container ID")
    if not isinstance(image_digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None:
        raise AttestationError("serving container has no exact image digest")
    if not isinstance(image_reference, str) or re.fullmatch(
        r".+@sha256:[0-9a-f]{64}", image_reference
    ) is None:
        raise AttestationError("serving container image reference is not digest-pinned")
    if not isinstance(started_at, str) or not started_at:
        raise AttestationError("serving container has no start identity")
    try:
        started_at_unix_ns = int(
            datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
            * 1_000_000_000
        )
    except ValueError as error:
        raise AttestationError("serving container start identity is invalid") from error

    mounts = [
        mount
        for mount in container.get("Mounts", [])
        if mount.get("Destination") == "/models"
    ]
    if len(mounts) != 1 or mounts[0].get("RW") is not False:
        raise AttestationError("the serving /models bind is not uniquely read-only")
    mount_source = Path(str(mounts[0].get("Source", ""))).resolve()
    if mount_source != root.parent:
        raise AttestationError("the serving /models bind does not contain the attested artifact")

    expected_root = "/models/" + root.name
    models = live_models(local_models_endpoint)
    if live_models(upstream_models_endpoint) != models:
        raise AttestationError("advertised upstream and loopback model catalogues differ")
    if [model["id"] for model in models] != sorted(aliases):
        raise AttestationError("live served aliases differ from the configured exact alias set")
    if any(model["root"] != expected_root for model in models):
        raise AttestationError("a live served alias resolves to a different model root")

    command = [str(container.get("Path", "")), *map(str, container.get("Args", []))]
    if expected_root not in command:
        raise AttestationError("the live container command does not name the attested model root")
    container_environment = {
        value.split("=", 1)[0]: value.split("=", 1)[1]
        for value in container.get("Config", {}).get("Env", [])
        if isinstance(value, str) and "=" in value
    }
    if container_environment.get("VLLM_ALLOW_RUNTIME_LORA_UPDATING") != "0":
        raise AttestationError("runtime LoRA mutation is not disabled")
    invocation = systemd_identity()
    labels = container.get("Config", {}).get("Labels") or {}
    launcher = Path(__file__).with_name("vllm_serve.sh")
    expected_labels = {
        "palios.taey.serve-launcher-sha256": file_sha256(launcher),
        "palios.taey.systemd-invocation-id": invocation["invocation_id"],
    }
    if not isinstance(labels, dict) or any(
        labels.get(key) != value for key, value in expected_labels.items()
    ):
        raise AttestationError("serving container is not bound to this launcher and invocation")
    return {
        "aliases": models,
        "completion_endpoint": upstream_completion_endpoint,
        "container_command": command,
        "container_id": container_id,
        "container_name": CONTAINER_NAME,
        "container_started_at": started_at,
        "container_started_at_unix_ns": started_at_unix_ns,
        "image_digest": image_digest,
        "image_reference": image_reference,
        "model_container_root": expected_root,
        "model_mount_root": "/models",
        "model_mount_read_only": True,
        "models_endpoint": upstream_models_endpoint,
        "provenance_labels": expected_labels,
        "service": invocation,
        "vllm_environment": {"VLLM_ALLOW_RUNTIME_LORA_UPDATING": "0"},
    }


def host_identity_sha256() -> str:
    machine_id = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    if re.fullmatch(r"[0-9a-f]{32}", machine_id) is None:
        raise AttestationError("host has no exact machine identity")
    return "sha256:" + hashlib.sha256(machine_id.encode("ascii")).hexdigest()


def implementation_identity(authority_id: str) -> dict[str, str]:
    attestor = Path(__file__).resolve(strict=True)
    launcher = attestor.with_name("vllm_serve.sh").resolve(strict=True)
    attestor_unit = Path("/etc/systemd/system/taey-model-identity-attestor.service")
    serving_unit = Path("/etc/systemd/system/taey-ep3.service")
    trust_root = attestor.with_name("trust") / f"{authority_id}.pub.pem"
    if not attestor_unit.is_file() or not serving_unit.is_file():
        raise AttestationError("installed model identity systemd units are missing")
    if not trust_root.is_file():
        raise AttestationError("public model identity trust root is missing")
    return {
        "attestor_sha256": "sha256:" + file_sha256(attestor),
        "attestor_service_unit_sha256": "sha256:" + file_sha256(attestor_unit),
        "effective_attestor_service_sha256": effective_unit_sha256(
            "taey-model-identity-attestor.service"
        ),
        "effective_serving_service_sha256": effective_unit_sha256(SYSTEMD_UNIT),
        "public_trust_root_sha256": "sha256:" + file_sha256(trust_root),
        "serve_launcher_sha256": "sha256:" + file_sha256(launcher),
        "serving_service_unit_sha256": "sha256:" + file_sha256(serving_unit),
    }


def signing_key(config: dict[str, Any]) -> tuple[Path, str]:
    credentials = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not credentials:
        raise AttestationError("systemd model identity signing credential is unavailable")
    private_key = Path(credentials) / "model-identity-signing-key"
    if not private_key.is_file():
        raise AttestationError("systemd model identity signing credential is missing")
    trusted_public_key = Path(__file__).with_name("trust") / (
        config["authority_id"] + ".pub.pem"
    )
    if not trusted_public_key.is_file():
        raise AttestationError("public model identity trust root is missing")
    private_public = subprocess.run(
        ["openssl", "pkey", "-in", str(private_key), "-pubout", "-outform", "DER"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    trusted_public = subprocess.run(
        [
            "openssl",
            "pkey",
            "-pubin",
            "-in",
            str(trusted_public_key),
            "-outform",
            "DER",
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if (
        private_public.returncode != 0
        or trusted_public.returncode != 0
        or private_public.stdout != trusted_public.stdout
    ):
        raise AttestationError("private signing key does not match the public trust root")
    if (
        len(trusted_public.stdout) != len(ED25519_SPKI_DER_PREFIX) + 32
        or not trusted_public.stdout.startswith(ED25519_SPKI_DER_PREFIX)
    ):
        raise AttestationError("model identity signing key must be Ed25519")
    fingerprint = "sha256:" + hashlib.sha256(trusted_public.stdout).hexdigest()
    return private_key, fingerprint


def sign_publication(payload: dict[str, Any], private_key: Path) -> str:
    with tempfile.NamedTemporaryFile() as payload_file:
        payload_file.write(canonical_json_bytes(payload))
        payload_file.flush()
        result = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(private_key),
                "-in",
                payload_file.name,
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    if result.returncode != 0:
        raise AttestationError("could not sign the model identity publication")
    return base64.b64encode(result.stdout).decode("ascii")


def read_redis_value(response: Any) -> Any:
    prefix = response.read(1)
    line = response.readline(4096)
    if prefix == b"+":
        return line.removesuffix(b"\r\n").decode("utf-8")
    if prefix == b"-":
        raise AttestationError(
            f"Redis rejected the command: {line.decode(errors='replace').strip()}"
        )
    if prefix == b":":
        return int(line)
    if prefix == b"$":
        length = int(line)
        if length == -1:
            return None
        value = response.read(length)
        if response.read(2) != b"\r\n":
            raise AttestationError("Redis returned a malformed bulk value")
        return value.decode("utf-8")
    if prefix == b"*":
        count = int(line)
        return [read_redis_value(response) for _ in range(count)]
    raise AttestationError("Redis returned an unknown response type")


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
        raise AttestationError(f"Redis is unavailable: {error}") from error


def redis_time_ns(host: str, port: int) -> int:
    value = redis_command(host, port, ["TIME"])
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, str) and item.isdigit() for item in value)
    ):
        raise AttestationError("Redis returned an invalid server time")
    return int(value[0]) * 1_000_000_000 + int(value[1]) * 1_000


def publish(
    host: str,
    port: int,
    key: str,
    value: str,
    ttl_seconds: int,
    attestor_generation: str,
) -> None:
    script = (
        "local current=redis.call('GET',KEYS[1]);"
        "if not current then redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2]);return 1;end;"
        "local ok,value=pcall(cjson.decode,current);"
        "if ok and value['attestor_generation']==ARGV[3] then "
        "redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2]);return 1;end;return 0"
    )
    if redis_command(
        host,
        port,
        ["EVAL", script, "1", key, value, str(ttl_seconds), attestor_generation],
    ) != 1:
        raise AttestationError("a different live receipt already owns the Redis identity key")


def revoke(host: str, port: int, key: str, attestor_generation: str) -> None:
    script = (
        "local current=redis.call('GET',KEYS[1]);if not current then return 0;end;"
        "local ok,value=pcall(cjson.decode,current);"
        "if ok and value['attestor_generation']==ARGV[1] then "
        "return redis.call('DEL',KEYS[1]);end;return 0"
    )
    redis_command(host, port, ["EVAL", script, "1", key, attestor_generation])


def redis_configuration() -> dict[str, Any]:
    redis_host = os.environ.get("TAEY_MODEL_IDENTITY_REDIS_HOST", "")
    authority_id = os.environ.get("TAEY_MODEL_IDENTITY_AUTHORITY_ID", "")
    if not redis_host or AUTHORITY_RE.fullmatch(authority_id) is None:
        raise AttestationError(
            "TAEY_MODEL_IDENTITY_REDIS_HOST and a valid "
            "TAEY_MODEL_IDENTITY_AUTHORITY_ID are required"
        )
    redis_port = int(os.environ.get("TAEY_MODEL_IDENTITY_REDIS_PORT", "6379"))
    if not 1 <= redis_port <= 65535:
        raise AttestationError("Redis port is out of range")
    key = f"taey:serving:model_identity:{authority_id}"
    if KEY_RE.fullmatch(key) is None:
        raise AttestationError("derived model identity Redis key is invalid")
    return {
        "authority_id": authority_id,
        "key": key,
        "redis_host": redis_host,
        "redis_port": redis_port,
    }


def required_environment() -> dict[str, Any]:
    root_raw = os.environ.get("TAEY_MODEL_PATH", "")
    if not root_raw:
        raise AttestationError("TAEY_MODEL_PATH is required")
    redis = redis_configuration()
    root = Path(root_raw).resolve(strict=True)
    if not root.is_dir():
        raise AttestationError("TAEY_MODEL_PATH is not a directory")
    configured_aliases = os.environ.get("TAEY_SERVED_NAME", "").split()
    aliases = sorted(configured_aliases)
    if not aliases or len(aliases) != len(set(aliases)):
        raise AttestationError("TAEY_SERVED_NAME must contain the exact served aliases")
    port = int(os.environ.get("VLLM_PORT", "8000"))
    if not 1 <= port <= 65535:
        raise AttestationError("endpoint port is out of range")
    upstream_completion_endpoint = os.environ.get(
        "TAEY_MODEL_IDENTITY_UPSTREAM_COMPLETION_ENDPOINT", ""
    )
    parsed_endpoint = urlsplit(upstream_completion_endpoint)
    if (
        parsed_endpoint.scheme != "http"
        or not parsed_endpoint.hostname
        or parsed_endpoint.port != port
        or parsed_endpoint.path != "/v1/chat/completions"
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise AttestationError(
            "TAEY_MODEL_IDENTITY_UPSTREAM_COMPLETION_ENDPOINT must be an exact HTTP "
            "vLLM completion endpoint on VLLM_PORT"
        )
    upstream_models_endpoint = urlunsplit(
        (
            parsed_endpoint.scheme,
            parsed_endpoint.netloc,
            "/v1/models",
            "",
            "",
        )
    )
    manifest_name = os.environ.get(
        "TAEY_MODEL_IDENTITY_MANIFEST", "model.safetensors.index.json"
    )
    manifest_path = Path(manifest_name)
    if manifest_path.is_absolute() or ".." in manifest_path.parts or manifest_name in {"", "."}:
        raise AttestationError("TAEY_MODEL_IDENTITY_MANIFEST must be a safe relative path")
    return {
        "aliases": aliases,
        **redis,
        "completion_endpoint": upstream_completion_endpoint,
        "heartbeat": HEARTBEAT_SECONDS,
        "manifest_name": manifest_name,
        "local_models_endpoint": f"http://127.0.0.1:{port}/v1/models",
        "models_endpoint": upstream_models_endpoint,
        "root": root,
        "ttl": TTL_SECONDS,
    }


def build_receipt(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime = runtime_identity(
        config["root"],
        config["aliases"],
        config["local_models_endpoint"],
        config["models_endpoint"],
        config["completion_endpoint"],
    )
    artifact, fence = artifact_identity(config["root"], config["manifest_name"])
    if max(item["ctime_ns"] for item in fence) >= runtime["container_started_at_unix_ns"]:
        raise AttestationError("artifact identity does not predate the serving process")
    if runtime_identity(
        config["root"],
        config["aliases"],
        config["local_models_endpoint"],
        config["models_endpoint"],
        config["completion_endpoint"],
    ) != runtime:
        raise AttestationError("live serving identity changed while hashing the artifact")
    receipt = {
        "contract": CONTRACT,
        "model": artifact,
        "publisher": {
            "authority_id": config["authority_id"],
            "audience": "taey-native-dcm/v2",
            "component": "taey-model-identity-attestor",
            "host_identity_sha256": host_identity_sha256(),
            "redis_key": config["key"],
        },
        "implementation": implementation_identity(config["authority_id"]),
        "serving": runtime,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt, {"artifact_fence": fence, "runtime": runtime}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    config = required_environment()
    receipt, frozen = build_receipt(config)
    if args.verify_only:
        print(canonical_json_bytes(receipt).decode("utf-8"))
        return 0
    private_key, signing_key_sha256 = signing_key(config)
    attestor_generation = uuid.uuid4().hex
    def stop(_signum: int, _frame: Any) -> None:
        raise AttestorStopped

    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            current_runtime = runtime_identity(
                config["root"],
                config["aliases"],
                config["local_models_endpoint"],
                config["models_endpoint"],
                config["completion_endpoint"],
            )
            if current_runtime != frozen["runtime"]:
                raise AttestationError("live serving identity changed after receipt construction")
            if artifact_fence(config["root"])[1] != frozen["artifact_fence"]:
                raise AttestationError("artifact filesystem identity changed after attestation")
            publication = {
                "attestor_generation": attestor_generation,
                "authority_id": config["authority_id"],
                "contract": PUBLICATION_CONTRACT,
                "published_at_redis_unix_ns": redis_time_ns(
                    config["redis_host"], config["redis_port"]
                ),
                "receipt": receipt,
                "receipt_sha256": receipt["receipt_sha256"],
                "signing_key_sha256": signing_key_sha256,
            }
            publication["signature_base64"] = sign_publication(publication, private_key)
            encoded = canonical_json_bytes(publication).decode("utf-8")
            publish(
                config["redis_host"],
                config["redis_port"],
                config["key"],
                encoded,
                config["ttl"],
                attestor_generation,
            )
            time.sleep(config["heartbeat"])
    except AttestorStopped:
        revoke(
            config["redis_host"],
            config["redis_port"],
            config["key"],
            attestor_generation,
        )
        return 0
    except BaseException:
        try:
            revoke(
                config["redis_host"],
                config["redis_port"],
                config["key"],
                attestor_generation,
            )
        except Exception as revoke_error:
            print(f"FATAL: receipt revocation failed: {revoke_error}", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AttestationError, OSError, ValueError) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        sys.exit(1)
