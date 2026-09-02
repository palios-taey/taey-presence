#!/usr/bin/env python3
"""Deterministic prompt and model-request receipts for Taey council seats."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MANIFEST_CONTRACT = "taey-local-council-seats/v1"
DCM_REQUEST_CONTRACT = "taey-native-dcm-request/v2"
PROMPT_CONTRACT = "taey-council-prompt-contract/v2"
MODEL_REQUEST_RECEIPT_CONTRACT = (
    "taey-council-model-request-producer-receipt/v1"
)
RESPONSE_CONTRACT = "taey-council-contribution/v1"
ROLE_CONTRACT_REVISION = 1
PROMPT_REVISION_MARKER = "<runtime-positive-integer>"
EVIDENCE_REGISTRY_MARKER = ("<runtime-evidence-registry>",)
_SEAT_ID_RE = re.compile(r"^taey-council-([1-9][0-9]*)$")

CONTRIBUTION_FIELDS = (
    "schema_version",
    "seat_id",
    "role_id",
    "status",
    "prompt_revision",
    "observations",
    "inferences",
    "unknowns",
    "evidence_refs",
    "concerns",
    "questions",
    "recommendation",
    "confidence",
)
CONTRIBUTION_LIST_FIELDS = (
    "observations",
    "inferences",
    "unknowns",
    "evidence_refs",
    "concerns",
    "questions",
)


class CouncilManifestError(RuntimeError):
    pass


@dataclass(frozen=True)
class SeatConfig:
    seat_id: str
    role_id: str
    conversation_id: str
    manifest_path: Path
    shared_prompt: Path
    shared_prompt_ref: str
    role_prompt: Path
    role_prompt_ref: str


@dataclass(frozen=True)
class CouncilManifest:
    path: Path
    sha256: str
    seats: tuple[SeatConfig, ...]


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_json(value).encode("utf-8")
    ).hexdigest()


def text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _required_string(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise CouncilManifestError(f"{field_name} must be a non-empty string")
    return normalized


def _prompt_path(
    manifest_path: Path,
    value: Any,
    field_name: str,
) -> tuple[str, Path]:
    reference = _required_string(value, field_name)
    relative = Path(reference)
    if relative.is_absolute():
        raise CouncilManifestError(f"{field_name} must be relative to the manifest")
    resolved = (manifest_path.parent / relative).resolve()
    try:
        resolved.relative_to(manifest_path.parent.resolve())
    except ValueError as exc:
        raise CouncilManifestError(
            f"{field_name} must remain below the manifest directory"
        ) from exc
    if not resolved.is_file():
        raise CouncilManifestError(f"{field_name} is not a file: {resolved}")
    if not resolved.read_text(encoding="utf-8").strip():
        raise CouncilManifestError(f"{field_name} is empty: {resolved}")
    return relative.as_posix(), resolved


def load_manifest(manifest_path: Path) -> CouncilManifest:
    path = manifest_path.resolve()
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise CouncilManifestError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise CouncilManifestError("manifest root must be an object")
    if set(document) != {"schema_version", "contract", "shared_prompt", "seats"}:
        raise CouncilManifestError("manifest root fields must match the v1 contract")
    if document.get("schema_version") != 1:
        raise CouncilManifestError("manifest schema_version must be 1")
    if document.get("contract") != MANIFEST_CONTRACT:
        raise CouncilManifestError(f"manifest contract must be {MANIFEST_CONTRACT}")
    shared_ref, shared_prompt = _prompt_path(
        path,
        document.get("shared_prompt"),
        "shared_prompt",
    )
    raw_seats = document.get("seats")
    if not isinstance(raw_seats, list) or not raw_seats:
        raise CouncilManifestError("manifest seats must be a non-empty array")
    seats: list[SeatConfig] = []
    seen_seats: set[str] = set()
    seen_roles: set[str] = set()
    seen_conversations: set[str] = set()
    for index, raw_seat in enumerate(raw_seats):
        if not isinstance(raw_seat, dict):
            raise CouncilManifestError(f"seats[{index}] must be an object")
        if set(raw_seat) != {
            "seat_id",
            "role_id",
            "conversation_id",
            "role_prompt",
        }:
            raise CouncilManifestError(
                f"seats[{index}] fields must match the v1 contract"
            )
        seat_id = _required_string(raw_seat.get("seat_id"), f"seats[{index}].seat_id")
        role_id = _required_string(raw_seat.get("role_id"), f"seats[{index}].role_id")
        conversation_id = _required_string(
            raw_seat.get("conversation_id"),
            f"seats[{index}].conversation_id",
        )
        if not _SEAT_ID_RE.fullmatch(seat_id):
            raise CouncilManifestError(
                f"seats[{index}].seat_id must be taey-council-<positive integer>"
            )
        if seat_id in seen_seats:
            raise CouncilManifestError(f"seat_id must be unique: {seat_id}")
        if role_id in seen_roles:
            raise CouncilManifestError(f"role_id must be unique: {role_id}")
        if conversation_id == "main" or conversation_id in seen_conversations:
            raise CouncilManifestError(
                f"conversation_id must be unique and private: {conversation_id}"
            )
        if conversation_id != f"council-{role_id}":
            raise CouncilManifestError(
                f"{seat_id} conversation_id must be council-{role_id}"
            )
        role_ref, role_prompt = _prompt_path(
            path,
            raw_seat.get("role_prompt"),
            f"seats[{index}].role_prompt",
        )
        seen_seats.add(seat_id)
        seen_roles.add(role_id)
        seen_conversations.add(conversation_id)
        seats.append(
            SeatConfig(
                seat_id=seat_id,
                role_id=role_id,
                conversation_id=conversation_id,
                manifest_path=path,
                shared_prompt=shared_prompt,
                shared_prompt_ref=shared_ref,
                role_prompt=role_prompt,
                role_prompt_ref=role_ref,
            )
        )
    return CouncilManifest(
        path=path,
        sha256=bytes_sha256(raw),
        seats=tuple(seats),
    )


def seat_for(manifest: CouncilManifest, seat_id: str) -> SeatConfig:
    matches = [seat for seat in manifest.seats if seat.seat_id == seat_id]
    if len(matches) != 1:
        raise CouncilManifestError(
            f"manifest must contain exactly one seat_id={seat_id}, got {len(matches)}"
        )
    return matches[0]


def read_prompt(path: Path, field_name: str) -> str:
    if not path.is_file():
        raise CouncilManifestError(f"{field_name} is not a readable file: {path}")
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise CouncilManifestError(f"{field_name} is empty: {path}")
    return content


def seat_role_contract(seat: SeatConfig) -> str:
    shared_prompt = read_prompt(seat.shared_prompt, "shared_prompt")
    role_prompt = read_prompt(seat.role_prompt, "role_prompt")
    return (
        f"Immutable runtime identity: seat_id={seat.seat_id}; "
        f"role_id={seat.role_id}; response_contract={RESPONSE_CONTRACT}; "
        f"role_contract_revision={ROLE_CONTRACT_REVISION}.\n\n"
        f"{shared_prompt}\n\n{role_prompt}"
    )


def system_message(seat: SeatConfig) -> dict[str, str]:
    return {
        "role": "system",
        "content": (
            "[COUNCIL ROLE CONTRACT]\n"
            f"{seat_role_contract(seat)}\n"
            "[/COUNCIL ROLE CONTRACT]"
        ),
    }


def response_format(
    seat: SeatConfig,
    prompt_revision: int | str = PROMPT_REVISION_MARKER,
    evidence_registry: list[str] | tuple[str, ...] = EVIDENCE_REGISTRY_MARKER,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "schema_version": {"type": "integer", "const": 1},
        "seat_id": {"type": "string", "const": seat.seat_id},
        "role_id": {"type": "string", "const": seat.role_id},
        "status": {"type": "string", "minLength": 1},
        "prompt_revision": {
            "type": "integer",
            "const": prompt_revision,
        },
        "recommendation": {"type": "string", "minLength": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    }
    for field_name in CONTRIBUTION_LIST_FIELDS:
        item_schema: dict[str, Any] = {"type": "string", "minLength": 1}
        if field_name == "evidence_refs":
            item_schema["enum"] = list(evidence_registry)
        properties[field_name] = {
            "type": "array",
            "items": item_schema,
        }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "taey_council_contribution_v1",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": list(CONTRIBUTION_FIELDS),
                "additionalProperties": False,
            },
        },
    }


def static_prompt_sources(manifest: CouncilManifest) -> list[dict[str, str]]:
    sources = [
        {
            "source_kind": "role",
            "seat_id": seat.seat_id,
            "role_id": seat.role_id,
            "path": seat.role_prompt_ref,
            "text_sha256": text_sha256(read_prompt(seat.role_prompt, "role_prompt")),
        }
        for seat in manifest.seats
    ]
    shared = manifest.seats[0]
    sources.append(
        {
            "source_kind": "shared",
            "seat_id": "*",
            "role_id": "*",
            "path": shared.shared_prompt_ref,
            "text_sha256": text_sha256(
                read_prompt(shared.shared_prompt, "shared_prompt")
            ),
        }
    )
    return sources


def prompt_contract(manifest: CouncilManifest, seat: SeatConfig) -> dict[str, Any]:
    return {
        "contract": PROMPT_CONTRACT,
        "manifest": {
            "contract": MANIFEST_CONTRACT,
            "path": manifest.path.name,
            "sha256": manifest.sha256,
        },
        "seat": {
            "seat_id": seat.seat_id,
            "role_id": seat.role_id,
            "conversation_id": seat.conversation_id,
        },
        "static_prompt_sources": static_prompt_sources(manifest),
        "system_message": system_message(seat),
        "response_format_template": response_format(seat),
        "request_renderer": {
            "contract": "openai-chat-completions-request/v1",
            "message_order": ["system", "user"],
            "chat_template_kwargs": {"enable_thinking": False},
            "attachments": {"state": "none", "items": []},
        },
    }


def prompt_contract_receipt(
    manifest: CouncilManifest,
    seat: SeatConfig,
) -> dict[str, Any]:
    contract = prompt_contract(manifest, seat)
    return {
        "producer_state": "self_asserted_unverified",
        "prompt_contract": contract,
        "prompt_contract_sha256": canonical_sha256(contract),
    }


def model_request_receipt(
    *,
    manifest: CouncilManifest,
    seat: SeatConfig,
    lineage: dict[str, Any],
    model_request: dict[str, Any],
    claims: list[Any],
) -> dict[str, Any]:
    if lineage.get("request_contract") != DCM_REQUEST_CONTRACT:
        raise ValueError(
            f"model request receipt requires {DCM_REQUEST_CONTRACT}"
        )
    attachments = [
        claim.payload.get("attachments")
        for claim in claims
        if claim.payload.get("attachments") not in (None, [])
    ]
    if attachments:
        raise ValueError("council v2 model requests do not support attachments")
    static_receipt = prompt_contract_receipt(manifest, seat)
    if lineage.get("prompt_contract_sha256") != static_receipt[
        "prompt_contract_sha256"
    ]:
        raise ValueError("lineage prompt contract differs from produced contract")
    if set(model_request) != {
        "model",
        "messages",
        "chat_template_kwargs",
        "response_format",
    }:
        raise ValueError("model request fields differ from the council request contract")
    messages = model_request.get("messages")
    if (
        not isinstance(model_request.get("model"), str)
        or not model_request["model"].strip()
        or model_request.get("chat_template_kwargs") != {"enable_thinking": False}
        or not isinstance(messages, list)
        or len(messages) != 2
        or messages[0] != system_message(seat)
        or not isinstance(messages[1], dict)
        or set(messages[1]) != {"role", "content"}
        or messages[1].get("role") != "user"
        or not isinstance(messages[1].get("content"), str)
    ):
        raise ValueError("model request does not contain the exact rendered council messages")
    expected_response_format = response_format(
        seat,
        lineage["prompt_revision"],
        lineage["evidence_registry"],
    )
    if model_request.get("response_format") != expected_response_format:
        raise ValueError("model request response_format differs from the rendered schema")
    evidence = [
        {
            "position": index,
            "source": claim.source.name,
            "message_id": claim.message_id,
            "raw_sha256": text_sha256(claim.raw),
        }
        for index, claim in enumerate(claims)
    ]
    attachment_state = {"state": "none", "items": []}
    body = {
        "contract": MODEL_REQUEST_RECEIPT_CONTRACT,
        "producer_state": "self_asserted_unverified",
        "request_contract": DCM_REQUEST_CONTRACT,
        "seat_id": seat.seat_id,
        "role_id": seat.role_id,
        "request_id": lineage["request_id"],
        "council_run_id": lineage.get("council_run_id"),
        "round_id": lineage.get("round_id"),
        "prompt_revision": lineage["prompt_revision"],
        "model_identity_receipt_sha256": lineage[
            "model_identity_receipt_sha256"
        ],
        "prompt_contract": static_receipt["prompt_contract"],
        "prompt_contract_sha256": static_receipt["prompt_contract_sha256"],
        "ordered_messages_sha256": canonical_sha256(messages),
        "ordered_evidence": evidence,
        "ordered_evidence_sha256": canonical_sha256(evidence),
        "attachments": attachment_state,
        "attachments_sha256": canonical_sha256(attachment_state),
        "response_format_sha256": canonical_sha256(expected_response_format),
        "model_request": model_request,
        "model_request_sha256": canonical_sha256(model_request),
    }
    return {**body, "receipt_sha256": canonical_sha256(body)}
