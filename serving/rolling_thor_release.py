#!/usr/bin/env python3
"""Bounded two-Thor rolling release orchestrator.

This tool deliberately does not alter promote_model.sh. It is a signed,
replay-protected planner for the bounded Thor release contract. Live execution
is deliberately disabled until a separately testable executor can prove every
remote transaction, rollback, locking, and host-identity guarantee.

A release is identified everywhere by a full SHA-256 over the candidate's sorted
``path + file-SHA-256`` manifest.  Names are never release identities.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HEX_SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
REPOSITORY_COMMIT_RE = re.compile(r"^[0-9A-Fa-f]{40}(?:[0-9A-Fa-f]{24})?$")
COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RELEASE_DIR_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_ALIASES = ("taey", "ep3")
CANONICAL_SIGNATURE_NAMESPACE = "taey-release"


class Refusal(RuntimeError):
    """A contract failure.  Nothing after this point may mutate a node."""


def refuse(message: str) -> None:
    raise Refusal(message)


def full_digest(value: str, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        refuse(f"{field} must be a lowercase full 64-character SHA-256")
    return value


def safe_absolute_path(value: str, field: str) -> str:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        refuse(f"{field} must be an absolute path without '..'")
    return str(path)


def safe_unit(value: str, field: str) -> str:
    if not COMPONENT_RE.fullmatch(value):
        refuse(f"{field} must be one safe systemd-unit component")
    return value


def canonical_aliases(value: str | list[str], field: str) -> tuple[str, str]:
    items = value.split() if isinstance(value, str) else value
    if not isinstance(items, list) or tuple(items) != REQUIRED_ALIASES:
        refuse(f"{field} must be exactly the stable aliases: taey ep3")
    if any(not isinstance(alias, str) or not COMPONENT_RE.fullmatch(alias) for alias in items):
        refuse(f"{field} contains an unsafe alias")
    return REQUIRED_ALIASES


def regular_file_bytes(path: Path, field: str) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            refuse(f"{field} must be a regular non-symlink file")
        return path.read_bytes()
    except OSError as exc:
        refuse(f"cannot read {field}: {exc}")


def verify_detached_signature(
    raw: bytes,
    signature_path: Path,
    allowed_signers_path: Path,
    signer_identity: str,
    signature_namespace: str,
    trust_policy_sha256: str,
) -> None:
    """Verify raw receipt bytes using its canonical authority signer policy."""
    signature = regular_file_bytes(signature_path, "hub decision signature")
    allowed = regular_file_bytes(allowed_signers_path, "allowed-signers file")
    if hashlib.sha256(allowed).hexdigest() != full_digest(
        trust_policy_sha256,
        "receipt authority trust_policy_sha256",
    ):
        refuse("allowed-signers file does not match receipt authority trust_policy_sha256")
    # ssh-keygen accepts the signature and allowed-signers policy only by pathname.  Verify
    # copies of the already lstat-checked/pinned bytes so a later pathname replacement cannot
    # change the policy or signature between these checks and the verifier opening it.
    with tempfile.TemporaryDirectory(prefix="taey-release-signature-") as temporary:
        temporary_path = Path(temporary)
        verifier_allowed = temporary_path / "allowed-signers"
        verifier_signature = temporary_path / "receipt.sig"
        verifier_allowed.write_bytes(allowed)
        verifier_signature.write_bytes(signature)
        os.chmod(verifier_allowed, 0o600)
        os.chmod(verifier_signature, 0o600)
        process = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(verifier_allowed),
                "-I",
                signer_identity,
                "-n",
                signature_namespace,
                "-s",
                str(verifier_signature),
            ],
            input=raw,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    if process.returncode:
        refuse("hub decision receipt detached signature is invalid or untrusted")
    if not signature:
        refuse("hub decision signature is empty")


def preverify_authority(receipt: Any) -> tuple[str, str, str]:
    """Extract only the signed-envelope authority inputs needed for verification.

    These values are untrusted until `ssh-keygen -Y verify` succeeds.  They are
    constrained here solely so the verifier uses the canonical namespace and a
    safe signer principal, while the allowed-signers file hash is bound to the
    authority's trust-policy digest.
    """
    if not isinstance(receipt, dict):
        refuse("hub decision receipt root must be an object")
    authority = receipt.get("authority")
    expected = {
        "surface",
        "actor_type",
        "actor_id",
        "signer_identity",
        "signature_namespace",
        "trust_policy_sha256",
    }
    if not isinstance(authority, dict) or set(authority) != expected:
        refuse("hub decision receipt authority is not exact")
    if not isinstance(authority["signer_identity"], str) or not COMPONENT_RE.fullmatch(authority["signer_identity"]):
        refuse("hub decision receipt authority signer_identity must be safe and nonblank")
    if authority["signature_namespace"] != CANONICAL_SIGNATURE_NAMESPACE:
        refuse(f"hub decision receipt authority signature_namespace must be {CANONICAL_SIGNATURE_NAMESPACE}")
    return (
        authority["signer_identity"],
        authority["signature_namespace"],
        full_digest(authority["trust_policy_sha256"], "receipt authority trust_policy_sha256"),
    )


def validate_authenticated_authority(receipt: dict[str, Any]) -> None:
    """Apply authorization semantics only after the complete receipt verifies."""
    authority = receipt["authority"]
    expected = {
        "surface",
        "actor_type",
        "actor_id",
        "signer_identity",
        "signature_namespace",
        "trust_policy_sha256",
    }
    if not isinstance(authority, dict) or set(authority) != expected:
        refuse("hub decision receipt authority is not exact")
    if authority["surface"] != "the-hub":
        refuse("hub decision receipt was not issued through The Hub")
    if authority["actor_type"] not in {"taey", "family-chat"}:
        refuse("a user is not a release approver; receipt must attribute Taey or a Family Chat")
    if not isinstance(authority["actor_id"], str) or not COMPONENT_RE.fullmatch(authority["actor_id"]):
        refuse("hub decision receipt authority actor_id must be safe and nonblank")
    if not isinstance(authority["signer_identity"], str) or not COMPONENT_RE.fullmatch(authority["signer_identity"]):
        refuse("hub decision receipt authority signer_identity must be safe and nonblank")
    if authority["signature_namespace"] != CANONICAL_SIGNATURE_NAMESPACE:
        refuse(f"hub decision receipt authority signature_namespace must be {CANONICAL_SIGNATURE_NAMESPACE}")
    full_digest(authority["trust_policy_sha256"], "receipt authority trust_policy_sha256")


def consume_receipt(
    ledger_path: Path,
    receipt_id: str,
    receipt_sha256: str,
) -> None:
    """Atomically consume a validated receipt ID so a planner cannot replay it."""
    parent = ledger_path.parent
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        refuse(f"receipt consumption ledger parent is unavailable: {exc}")
    if not stat.S_ISDIR(parent_info.st_mode) or parent.is_symlink():
        refuse("receipt consumption ledger parent must be a real directory")
    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        refuse(f"cannot acquire receipt consumption lock: {exc}")
    try:
        with os.fdopen(descriptor, "r+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            consumed: dict[str, Any] = {}
            if ledger_path.exists():
                raw_ledger = regular_file_bytes(ledger_path, "receipt consumption ledger")
                try:
                    consumed = json.loads(raw_ledger)
                except json.JSONDecodeError:
                    refuse("receipt consumption ledger is invalid JSON")
                if not isinstance(consumed, dict):
                    refuse("receipt consumption ledger root must be an object")
            if receipt_id in consumed:
                refuse("hub decision receipt_id was already consumed")
            consumed[receipt_id] = {
                "receipt_sha256": receipt_sha256,
                "consumed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{ledger_path.name}.",
                suffix=".tmp",
                dir=parent,
            )
            try:
                with os.fdopen(temporary_fd, "w", encoding="utf-8") as temporary:
                    json.dump(consumed, temporary, sort_keys=True, separators=(",", ":"))
                    temporary.write("\n")
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.chmod(temporary_name, 0o600)
                os.replace(temporary_name, ledger_path)
                directory_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
    finally:
        # fdopen owns and closes descriptor on the normal path; close only exceptional opens.
        try:
            os.close(descriptor)
        except OSError:
            pass


def read_receipt(
    path: Path,
    signature_path: Path,
    allowed_signers_path: Path,
    ledger_path: Path,
    max_age_seconds: int,
    candidate_digest: str,
    rollback_digest: str,
) -> tuple[str, dict[str, Any]]:
    """Validate the shared training promote envelope without translating its fields."""
    try:
        raw = regular_file_bytes(path, "hub decision receipt")
        receipt = json.loads(raw)
    except json.JSONDecodeError as exc:
        refuse(f"hub decision receipt is unreadable or invalid JSON: {exc}")
    signer_identity, signature_namespace, trust_policy_sha256 = preverify_authority(receipt)
    verify_detached_signature(
        raw,
        signature_path,
        allowed_signers_path,
        signer_identity,
        signature_namespace,
        trust_policy_sha256,
    )

    required = {
        "schema_version",
        "receipt_id",
        "campaign_id",
        "campaign_spec_sha256",
        "transition",
        "decision",
        "issued_at",
        "authority",
        "authorization_plane",
        "evidence",
        "subject",
    }
    if set(receipt) != required:
        refuse("hub decision receipt root fields are not the exact canonical training envelope")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        refuse("hub decision receipt schema_version must be integer 1")
    for field in ("receipt_id", "campaign_id"):
        if not isinstance(receipt[field], str) or not receipt[field].strip():
            refuse(f"hub decision receipt lacks nonblank {field}")
    full_digest(receipt["campaign_spec_sha256"], "receipt campaign_spec_sha256")
    if not isinstance(receipt["issued_at"], str) or not receipt["issued_at"].strip():
        refuse("hub decision receipt lacks issued_at")
    try:
        issued_at = dt.datetime.fromisoformat(receipt["issued_at"].replace("Z", "+00:00"))
    except ValueError:
        refuse("hub decision receipt issued_at is not ISO-8601")
    if issued_at.tzinfo is None:
        refuse("hub decision receipt issued_at must carry a timezone")
    age_seconds = (dt.datetime.now(dt.timezone.utc) - issued_at.astimezone(dt.timezone.utc)).total_seconds()
    if age_seconds < -60 or age_seconds > max_age_seconds:
        refuse("hub decision receipt is outside the allowed issuance window")

    validate_authenticated_authority(receipt)
    if receipt["transition"] != "promote":
        refuse("hub decision receipt transition must be promote")
    if receipt["decision"] != "approved":
        refuse("hub decision receipt does not approve this rolling release")
    if receipt["authorization_plane"] != "taey-family-chats":
        refuse("hub decision receipt authorization_plane is not taey-family-chats")

    evidence = receipt["evidence"]
    if not isinstance(evidence, list) or not evidence:
        refuse("hub decision receipt evidence must be a nonempty list")
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"repository_commit", "receipt_sha256"}:
            refuse("each hub decision receipt evidence item must be exact")
        if not isinstance(item["repository_commit"], str) or not REPOSITORY_COMMIT_RE.fullmatch(item["repository_commit"]):
            refuse("hub decision receipt evidence repository_commit must be 40 or 64 hex")
        if not isinstance(item["receipt_sha256"], str) or not HEX_SHA256_RE.fullmatch(item["receipt_sha256"]):
            refuse("hub decision receipt evidence receipt_sha256 must be 64 hex")

    subject = receipt["subject"]
    if not isinstance(subject, dict) or set(subject) != {
        "artifact_sha256",
        "rollback_artifact_sha256",
        "consumer_aliases",
    }:
        refuse("hub decision receipt subject is not exact")
    if full_digest(subject["artifact_sha256"], "receipt subject artifact_sha256") != candidate_digest:
        refuse("hub decision receipt artifact digest does not match candidate")
    if full_digest(subject["rollback_artifact_sha256"], "receipt subject rollback_artifact_sha256") != rollback_digest:
        refuse("hub decision receipt rollback digest does not match rollback target")
    canonical_aliases(subject["consumer_aliases"], "receipt subject consumer_aliases")
    receipt_sha256 = hashlib.sha256(raw).hexdigest()
    consume_receipt(ledger_path, receipt["receipt_id"], receipt_sha256)
    return receipt_sha256, receipt


def parse_env_file(path: Path) -> dict[str, str]:
    """Read the simple KEY=value fleet template without executing site configuration."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        refuse(f"cannot read fleet env file: {exc}")
    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped, comments=True, posix=True)
        except ValueError as exc:
            refuse(f"invalid fleet env syntax at line {line_number}: {exc}")
        if not tokens:
            continue
        if len(tokens) != 1 or "=" not in tokens[0]:
            refuse(f"fleet env line {line_number} is not a simple KEY=value assignment")
        key, value = tokens[0].split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            refuse(f"fleet env line {line_number} has an unsafe variable name")
        values[key] = value
    return values


def environment(fleet_path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if fleet_path:
        values.update(parse_env_file(fleet_path))
    # Explicit environment always wins, matching the existing fleet tooling.
    for key, value in os.environ.items():
        if key.startswith("TAEY_"):
            values[key] = value
    return values


def required(values: dict[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        refuse(f"set {key}")
    return value


def consumer_declaration(value: str, key: str) -> tuple[str, ...]:
    if value == "none":
        return ()
    if not value:
        refuse(f"set {key} to the pinned consumer units, or literal 'none'")
    units = tuple(value.split())
    if not units or any(not COMPONENT_RE.fullmatch(unit) or not unit.endswith(".service") for unit in units):
        refuse(f"{key} must be 'none' or safe .service unit names")
    if len(set(units)) != len(units):
        refuse(f"{key} contains a duplicate consumer declaration")
    return units


@dataclass(frozen=True)
class Node:
    label: str
    ssh: str
    host: str
    models: str
    consumers: tuple[str, ...]

    @property
    def root(self) -> str:
        return f"{self.models}/.taey-release"


@dataclass(frozen=True)
class Contract:
    candidate_digest: str
    rollback_digest: str
    receipt_sha256: str
    campaign_id: str
    campaign_spec_sha256: str
    transition: str
    staging_node: Node
    other_node: Node
    serve_unit: str
    port: int
    container_models_root: str
    sync_driver: str
    candidate_source: str
    bake_command: str
    verify_command: str


def build_contract(args: argparse.Namespace) -> Contract:
    values = environment(Path(args.fleet_env) if args.fleet_env else None)
    candidate = full_digest(args.artifact_sha256, "--artifact-sha256")
    rollback = full_digest(args.rollback_artifact_sha256, "--rollback-artifact-sha256")
    if candidate == rollback:
        refuse("candidate and rollback artifact digests must differ")
    if args.receipt_max_age_seconds <= 0:
        refuse("--receipt-max-age-seconds must be positive")

    canonical_aliases(required(values, "TAEY_SERVED_NAME"), "TAEY_SERVED_NAME")
    if values.get("TAEY_PRIMARY_SERVED_NAME", "taey") != "taey":
        refuse("TAEY_PRIMARY_SERVED_NAME must remain taey")
    serve_unit = safe_unit(values.get("TAEY_SERVE_UNIT", "taey-ep3.service"), "TAEY_SERVE_UNIT")
    try:
        port = int(values.get("TAEY_SERVE_PORT", "8000"))
    except ValueError:
        refuse("TAEY_SERVE_PORT must be an integer")
    if not 1 <= port <= 65535:
        refuse("TAEY_SERVE_PORT must be a valid TCP port")
    container_models_root = safe_absolute_path(values.get("TAEY_CONTAINER_MODELS_ROOT", "/models"), "TAEY_CONTAINER_MODELS_ROOT")

    nodes: dict[str, Node] = {}
    for label in ("node1", "node2"):
        number = label[-1]
        ssh = required(values, f"TAEY_NODE{number}_SSH")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9.-]*", ssh):
            refuse(f"TAEY_NODE{number}_SSH must be a safe user@host destination")
        models = safe_absolute_path(required(values, f"TAEY_NODE{number}_MODELS"), f"TAEY_NODE{number}_MODELS")
        host = values.get(f"TAEY_NODE{number}_HOST", ssh.split("@")[-1])
        if not host or any(c.isspace() for c in host):
            refuse(f"TAEY_NODE{number}_HOST must not contain whitespace")
        nodes[label] = Node(label, ssh, host, models, consumer_declaration(values.get(f"TAEY_NODE{number}_CONSUMERS", ""), f"TAEY_NODE{number}_CONSUMERS"))
    if args.staging_node not in nodes:
        refuse("--staging-node must be node1 or node2")
    staging = nodes[args.staging_node]
    other = nodes["node1" if staging.label == "node2" else "node2"]
    sync_driver = values.get("TAEY_SYNC_DRIVER", staging.label)
    if sync_driver not in {staging.label, other.label}:
        refuse("TAEY_SYNC_DRIVER must be node1 or node2")

    candidate_source = safe_absolute_path(args.candidate_source, "--candidate-source")
    # These are explicit plan inputs for a future, separately reviewed executor.  This planner
    # never sends them to a host or executes them locally.
    if not args.bake_command.strip():
        refuse("--bake-command is required; no implicit same-node bake is permitted")
    if not args.verify_command.strip():
        refuse("--verify-command is required; no candidate may bypass verification")
    # Consume the authorization only after every non-authority input has passed validation.
    receipt_sha, receipt = read_receipt(
        Path(args.hub_decision_receipt),
        Path(args.hub_decision_signature),
        Path(args.allowed_signers),
        Path(args.receipt_consumption_ledger),
        args.receipt_max_age_seconds,
        candidate,
        rollback,
    )
    return Contract(candidate, rollback, receipt_sha, receipt["campaign_id"],
                    receipt["campaign_spec_sha256"], receipt["transition"],
                    staging, other, serve_unit, port,
                    container_models_root, sync_driver, candidate_source,
                    args.bake_command, args.verify_command)


def local_manifest_digest(directory: Path) -> str:
    """Digest only a real, regular-file release tree; reject every link/special entry."""
    try:
        root_info = directory.lstat()
    except OSError as exc:
        refuse(f"cannot inspect artifact directory: {exc}")
    if not stat.S_ISDIR(root_info.st_mode) or directory.is_symlink():
        refuse("artifact directory must be a real non-symlink directory")
    rows: list[bytes] = []
    for parent, dirs, files in os.walk(directory, topdown=True, followlinks=False):
        parent_path = Path(parent)
        for name in dirs:
            child = parent_path / name
            try:
                info = child.lstat()
            except OSError as exc:
                refuse(f"cannot inspect artifact directory entry: {exc}")
            if not stat.S_ISDIR(info.st_mode) or child.is_symlink():
                refuse("artifact contains a symlink or special directory entry")
        for name in files:
            child = parent_path / name
            try:
                info = child.lstat()
            except OSError as exc:
                refuse(f"cannot inspect artifact file entry: {exc}")
            if not stat.S_ISREG(info.st_mode) or child.is_symlink():
                refuse("artifact contains a symlink or special file entry")
            digest = hashlib.sha256()
            with child.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            rows.append(f"{digest.hexdigest()}  ./{child.relative_to(directory).as_posix()}\n".encode())
    rows.sort()
    if not rows:
        refuse("artifact directory has no regular files")
    return hashlib.sha256(b"".join(rows)).hexdigest()


def _relative_target(kind: str, digest: str) -> str:
    full_digest(digest, "pointer digest")
    if kind not in {"releases", "staging"}:
        refuse("pointer target kind is invalid")
    return f"{kind}/{digest}"


def candidate_pointer_targets(candidate_digest: str, rollback_digest: str) -> tuple[str, str]:
    """Return the only valid pointer pair for a successful candidate transition.

    ``rollback_digest`` is the artifact that was current immediately before the
    transition.  It must become ``previous``; preserving the older previous
    release would make rollback and retention skip the immediate predecessor.
    """
    return (
        _relative_target("staging", candidate_digest),
        _relative_target("releases", rollback_digest),
    )


def atomic_symlink(root: Path, name: str, relative_target: str) -> None:
    """Atomically replace one pointer, never following an existing link."""
    if name not in {"current", "previous"} or not re.fullmatch(r"(?:releases|staging)/[0-9a-f]{64}", relative_target):
        refuse("unsafe pointer update")
    root.mkdir(parents=True, exist_ok=True)
    temp = root / f".{name}.{os.getpid()}.tmp"
    try:
        temp.unlink(missing_ok=True)
        os.symlink(relative_target, temp)
        os.replace(temp, root / name)
    finally:
        temp.unlink(missing_ok=True)


def validate_pointer(root: Path, name: str, allowed_kinds: set[str] | None = None) -> tuple[str, str]:
    if name not in {"current", "previous"}:
        refuse("invalid pointer name")
    path = root / name
    if not path.is_symlink():
        refuse(f"{name} pointer is absent or not a symlink")
    raw = os.readlink(path)
    match = re.fullmatch(r"(releases|staging)/([0-9a-f]{64})", raw)
    if not match or (allowed_kinds is not None and match.group(1) not in allowed_kinds):
        refuse(f"{name} pointer has an unsafe target")
    resolved = (root / raw).resolve(strict=False)
    expected_parent = (root / match.group(1)).resolve(strict=False)
    if resolved.parent != expected_parent:
        refuse(f"{name} pointer resolves outside release root")
    return match.group(1), match.group(2)


def retention_delete_candidates(releases: Path, current_digest: str, previous_digest: str, active_staging: set[str]) -> list[Path]:
    """Return only old, full-digest release directories eligible after both-node proof."""
    full_digest(current_digest, "current digest")
    full_digest(previous_digest, "previous digest")
    if any(not SHA256_RE.fullmatch(digest) for digest in active_staging):
        refuse("active staging set contains a non-digest")
    keep = {current_digest, previous_digest} | active_staging
    if not releases.is_dir():
        return []
    return [child for child in releases.iterdir()
            if child.is_dir() and not child.is_symlink() and RELEASE_DIR_RE.fullmatch(child.name)
            and child.name not in keep]




def plan(contract: Contract) -> dict[str, Any]:
    return {
        "schema": "taey.thor_rolling_release_plan.v1",
        "mode": "dry-run",
        "campaign_id": contract.campaign_id,
        "campaign_spec_sha256": contract.campaign_spec_sha256,
        "transition": contract.transition,
        "hub_decision_receipt_sha256": contract.receipt_sha256,
        "artifact_sha256": contract.candidate_digest,
        "rollback_artifact_sha256": contract.rollback_digest,
        "consumer_aliases": list(REQUIRED_ALIASES),
        "staging_node": contract.staging_node.label,
        "steps": [
            "prove standby Thor serves rollback through node-local catalogue and generation",
            "quiesce only staging-node consumers, then stop its vLLM before any bake",
            "stage, bake, and independently verify candidate; reject symlinks and special files",
            "move byte-verified staging into immutable releases before atomically repointing current",
            "promote one Thor at a time under controller and both-node transaction locks",
            "reverify candidate through node-local HTTP, then clean up only after both-node proof",
            "on failure stop/restart/reverify every candidate-serving node before rollback and delete staging only",
        ],
        "execution": (
            "disabled: this signed planner performs no SSH, service, release-filesystem, "
            "or remote-lock action; accepted receipt_id is recorded in the local replay ledger"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fleet-env", help="site fleet.env; TAEY_* environment overrides it")
    parser.add_argument("--hub-decision-receipt", required=True)
    parser.add_argument("--hub-decision-signature", required=True)
    parser.add_argument("--allowed-signers", required=True)
    parser.add_argument("--receipt-consumption-ledger", required=True)
    parser.add_argument("--receipt-max-age-seconds", type=int, default=900)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--rollback-artifact-sha256", required=True)
    parser.add_argument("--candidate-source", required=True, help="absolute path on the staging Thor")
    parser.add_argument("--staging-node", choices=("node1", "node2"), required=True)
    parser.add_argument("--bake-command", required=True, help="declared future hook; writes $TAEY_RELEASE_STAGING")
    parser.add_argument("--verify-command", required=True, help="declared future hook; verifies $TAEY_RELEASE_STAGING")
    parser.add_argument("--apply", action="store_true", help="disabled; no live executor is shipped")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            refuse("--apply is disabled: no live rolling-release executor is shipped")
        contract = build_contract(args)
        print(json.dumps(plan(contract), indent=2, sort_keys=True))
        return 0
    except Refusal as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
