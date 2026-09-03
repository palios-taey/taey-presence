#!/usr/bin/env python3
"""Private supporting-seat runtime for Taey's seven-seat local council."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import redis

import council_prompt_receipt as prompt_producer
import model_identity_status
import taey_adapter as dcm_adapter
import taey_seat as executive


ROLE_ID = os.environ.get("TAEY_COUNCIL_ROLE_ID", "").strip()
SHARED_PROMPT_VALUE = os.environ.get(
    "TAEY_COUNCIL_SHARED_PROMPT_PATH",
    "",
).strip()
ROLE_PROMPT_VALUE = os.environ.get(
    "TAEY_COUNCIL_ROLE_PROMPT_PATH",
    "",
).strip()
MANIFEST_VALUE = os.environ.get(
    "TAEY_COUNCIL_MANIFEST_PATH",
    str(Path(__file__).resolve().with_name("council_seats.json")),
).strip()
SHARED_PROMPT_PATH = Path(SHARED_PROMPT_VALUE).expanduser()
ROLE_PROMPT_PATH = Path(ROLE_PROMPT_VALUE).expanduser()
MANIFEST_PATH = Path(MANIFEST_VALUE).expanduser()
RESPONSE_CONTRACT = prompt_producer.RESPONSE_CONTRACT
ROLE_CONTRACT_REVISION = prompt_producer.ROLE_CONTRACT_REVISION
DEFAULT_PROMPT_REVISION = 1
DEFAULT_IDLE_POLL_SECONDS = 0.25
DEFAULT_LIVENESS_TTL_SECONDS = 5
DEFAULT_LIVENESS_REFRESH_SECONDS = 1.0
PROCESS_GENERATION = uuid.uuid4().hex
CONTRIBUTION_FIELDS = prompt_producer.CONTRIBUTION_FIELDS
CONTRIBUTION_LIST_FIELDS = prompt_producer.CONTRIBUTION_LIST_FIELDS

_REGISTER_AT_REST_LUA = """
if redis.call('EXISTS', KEYS[6]) == 1 then
    return redis.error_reply('a live council seat generation is already registered')
end
local expected_types = {'zset', 'string', 'string', 'string', 'set', 'string'}
for index = 1, #KEYS do
    local key_type = redis.call('TYPE', KEYS[index])['ok']
    if key_type ~= 'none' and key_type ~= expected_types[index] then
        return redis.error_reply(
            'council liveness key type mismatch index=' .. index
        )
    end
end
local count = redis.call('ZCARD', KEYS[1])
redis.call('SET', KEYS[2], count)
redis.call('SADD', KEYS[5], ARGV[1])
redis.call('SET', KEYS[6], ARGV[2], 'EX', ARGV[3])
if count == 0 then
    redis.call('SET', KEYS[3], '1')
    redis.call('DEL', KEYS[4])
else
    redis.call('DEL', KEYS[3])
end
return count
"""
_PROMOTE_READY_REGISTRATION_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""
_REFRESH_REGISTRATION_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
return redis.call('EXPIRE', KEYS[1], ARGV[2])
"""
_MUTATE_OWNED_CLAIM_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return redis.error_reply('seat process generation is not current')
end
local removed = redis.call('LREM', KEYS[2], 1, ARGV[2])
if removed == 1 and ARGV[3] == 'requeue' then
    if ARGV[4] == 'LEFT' then
        redis.call('LPUSH', KEYS[3], ARGV[2])
    else
        redis.call('RPUSH', KEYS[3], ARGV[2])
    end
end
return removed
"""
_ACK_DCM_OWNED_CLAIM_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return redis.error_reply('seat process generation is not current')
end
local processing_type = redis.call('TYPE', KEYS[2])['ok']
if processing_type ~= 'none' and processing_type ~= 'list' then
    return redis.error_reply('council processing key is not a list')
end
local result_type = redis.call('TYPE', KEYS[3])['ok']
if result_type ~= 'none' and result_type ~= 'list' then
    return redis.error_reply('DCM result key is not a list')
end
local removed = redis.call('LREM', KEYS[2], 1, ARGV[2])
if removed == 1 then
    redis.call('RPUSH', KEYS[3], ARGV[3])
end
return removed
"""
_CLAIM_STALE_GENERATION_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return redis.error_reply('seat process generation is not current')
end
local queued = redis.call('LRANGE', KEYS[2], 0, -1)
for _, raw in ipairs(queued) do
    local decoded, payload = pcall(cjson.decode, raw)
    if decoded and type(payload) == 'table'
       and payload['type'] == 'council_request'
       and tostring(payload['expected_process_generation'] or '') ~= ARGV[2] then
        local removed = redis.call('LREM', KEYS[2], 1, raw)
        if removed == 1 then
            if ARGV[3] == 'LEFT' then
                redis.call('LPUSH', KEYS[3], raw)
            else
                redis.call('RPUSH', KEYS[3], raw)
            end
            return raw
        end
    end
end
return nil
"""


def _validate_seat_contract() -> tuple[
    prompt_producer.CouncilManifest,
    prompt_producer.SeatConfig,
    str,
]:
    try:
        manifest = prompt_producer.load_manifest(MANIFEST_PATH)
        seat = prompt_producer.seat_for(manifest, executive.SESSION)
    except prompt_producer.CouncilManifestError as exc:
        raise executive.SeatFailure(str(exc)) from exc
    if ROLE_ID != seat.role_id:
        raise executive.SeatFailure(
            f"{executive.SESSION} requires TAEY_COUNCIL_ROLE_ID={seat.role_id}, "
            f"got {ROLE_ID or '<empty>'}"
        )
    if executive.CONVERSATION_ID != seat.conversation_id:
        raise executive.SeatFailure(
            f"{executive.SESSION} requires "
            f"TAEY_CONVERSATION_ID={seat.conversation_id}, "
            f"got {executive.CONVERSATION_ID}"
        )
    if not SHARED_PROMPT_VALUE or SHARED_PROMPT_PATH.resolve() != seat.shared_prompt:
        raise executive.SeatFailure(
            "TAEY_COUNCIL_SHARED_PROMPT_PATH must match the manifest shared_prompt"
        )
    if not ROLE_PROMPT_VALUE or ROLE_PROMPT_PATH.resolve() != seat.role_prompt:
        raise executive.SeatFailure(
            "TAEY_COUNCIL_ROLE_PROMPT_PATH must match the manifest role_prompt"
        )
    if executive.EVENT_LOG.name != f"{executive.SESSION}.jsonl":
        raise executive.SeatFailure(
            f"{executive.SESSION} requires a private event log named "
            f"{executive.SESSION}.jsonl, got {executive.EVENT_LOG}"
        )
    if executive.EVENT_LOG.is_symlink():
        raise executive.SeatFailure(
            f"council event log cannot be a symlink: {executive.EVENT_LOG}"
        )
    if executive.EVENT_LOG.exists() and executive.EVENT_LOG.stat().st_mode & 0o077:
        raise executive.SeatFailure(
            f"council event log is group/world accessible: {executive.EVENT_LOG}"
        )
    parent = executive.EVENT_LOG.parent
    if parent.exists() and parent.stat().st_mode & 0o077:
        raise executive.SeatFailure(
            f"council event-log directory is group/world accessible: {parent}"
        )
    return manifest, seat, prompt_producer.seat_role_contract(seat)


class CouncilEventStore(executive.EventStore):
    def __init__(
        self,
        path: Path,
        max_turns: int,
        manifest: prompt_producer.CouncilManifest,
        seat: prompt_producer.SeatConfig,
        seat_contract: str,
    ):
        self.manifest = manifest
        self.seat = seat
        self.seat_contract = seat_contract
        self.attempted_message_ids: set[str] = set()
        self.prompt_contract_sha256 = prompt_producer.text_sha256(
            seat_contract
        ).removeprefix("sha256:")
        self.dcm_v2_prompt_contract_receipt = (
            prompt_producer.prompt_contract_receipt(manifest, seat)
        )
        self.dcm_v2_prompt_contract_sha256 = (
            self.dcm_v2_prompt_contract_receipt["prompt_contract_sha256"]
        )
        super().__init__(path, max_turns)
        for event in self._read_events():
            if event.get("event_type") == "turn_attempt":
                self.attempted_message_ids.update(
                    str(message_id)
                    for message_id in event.get("message_ids") or []
                )
            if (
                event.get("event_type") == "turn_outcome"
                and event.get("kind")
                in {
                    "dead_generation_terminal",
                    "council_generation_terminal_failure",
                }
            ):
                self.completed_message_ids.update(
                    str(message_id)
                    for message_id in event.get("message_ids") or []
                )

    def messages_for(self, prompt: str) -> list[dict[str, str]]:
        return [
            prompt_producer.system_message(self.seat),
            {"role": "user", "content": prompt},
        ]

    def evidence_registry(
        self,
        claims: list[executive.ClaimedMessage],
        event_id: str,
        prompt_contract_sha256: str | None = None,
    ) -> list[str]:
        references = [
            f"role_contract:{prompt_contract_sha256 or self.prompt_contract_sha256}"
        ]
        if claims:
            references.extend(
                "fleet_message:"
                f"{executive._safe_trace_id(claim.message_id, event_id)}"
                for claim in claims
            )
        else:
            references.append(f"operator_probe:{event_id}")
        return list(dict.fromkeys(references))


def _response_lineage(
    claims: list[executive.ClaimedMessage],
    event_id: str,
    correlation_id: str,
    prompt_contract_sha256: str,
    request_contract: str | None = None,
) -> dict[str, Any]:
    payload = claims[0].payload if len(claims) == 1 else {}
    request_id = executive._safe_trace_id(
        payload.get("request_id") or event_id,
        event_id,
    )
    prompt_revision = payload.get(
        "prompt_revision",
        DEFAULT_PROMPT_REVISION,
    )
    try:
        normalized_revision = int(prompt_revision)
    except (TypeError, ValueError) as exc:
        raise executive.SeatFailure(
            f"prompt_revision must be an integer, got {prompt_revision!r}"
        ) from exc
    if normalized_revision < 1:
        raise executive.SeatFailure("prompt_revision must be at least 1")
    lineage: dict[str, Any] = {
        "seat_id": executive.SESSION,
        "seat_kind": "council",
        "role_id": ROLE_ID,
        "conversation_id": executive.CONVERSATION_ID,
        "process_generation": PROCESS_GENERATION,
        "request_id": request_id,
        "response_contract": RESPONSE_CONTRACT,
        "prompt_revision": normalized_revision,
        "prompt_contract_sha256": prompt_contract_sha256,
    }
    if request_contract is not None:
        lineage["request_contract"] = request_contract
        lineage["model_identity_receipt_sha256"] = payload[
            "model_identity_receipt_sha256"
        ]
        dcm_fields = (
            "delivery_id",
            "dcm_session_id",
            "wave_id",
            "round",
            "phase",
            "prompt_id",
            "prompt_sha256",
            "request_revision",
            "parent_contribution_ids",
            "parent_frontier_sha256",
            "process_generation_expected",
            "model_endpoint",
            "requested_alias",
            "model_manifest_sha256",
            "model_content_sha256",
            "serving_container_digest",
        )
        lineage.update({field: payload[field] for field in dcm_fields})
    for field_name in ("council_run_id", "round_id"):
        value = payload.get(field_name)
        if value:
            lineage[field_name] = executive._safe_trace_id(
                value,
                correlation_id,
            )
    return lineage


def _request_contract(
    claims: list[executive.ClaimedMessage],
    store: CouncilEventStore,
) -> tuple[str | None, str]:
    explicit = [
        claim for claim in claims if claim.payload.get("request_contract") is not None
    ]
    if not explicit:
        return None, store.prompt_contract_sha256
    if len(claims) != 1 or len(explicit) != 1:
        raise executive.SeatFailure(
            "an explicit council request contract requires exactly one claimed message"
        )
    payload = explicit[0].payload
    if payload.get("request_contract") != prompt_producer.DCM_REQUEST_CONTRACT:
        raise executive.SeatFailure(
            "unsupported council request_contract: "
            f"{payload.get('request_contract')!r}"
        )
    if payload.get("prompt_contract_sha256") != (
        store.dcm_v2_prompt_contract_sha256
    ):
        raise executive.SeatFailure(
            "v2 prompt_contract_sha256 does not match the produced seat contract"
        )
    model_identity = payload.get("model_identity_receipt_sha256")
    if (
        not isinstance(model_identity, str)
        or not model_identity.startswith("sha256:")
        or len(model_identity) != 71
        or any(character not in "0123456789abcdef" for character in model_identity[7:])
    ):
        raise executive.SeatFailure(
            "v2 model_identity_receipt_sha256 must be sha256:<64 lowercase hex>"
        )
    claim = explicit[0]
    expected = {
        "delivery_id": claim.message_id,
        "dcm_session_id": payload.get("round_id"),
        "seat_id": executive.SESSION,
        "role": ROLE_ID,
        "process_generation_expected": PROCESS_GENERATION,
        "expected_process_generation": PROCESS_GENERATION,
        "model_endpoint": executive.PROXY_URL,
        "requested_alias": executive.MODEL,
    }
    mismatched = [
        field for field, value in expected.items() if payload.get(field) != value
    ]
    if mismatched:
        raise executive.SeatFailure(
            f"v2 request delivery differs from the live seat: {sorted(mismatched)}"
        )
    return prompt_producer.DCM_REQUEST_CONTRACT, store.dcm_v2_prompt_contract_sha256


def _prompt_for(
    text: str,
    claims: list[executive.ClaimedMessage],
    lineage: dict[str, Any],
) -> str:
    field_names = [
        "seat_id",
        "role_id",
        "request_id",
        "response_contract",
        "prompt_revision",
        "evidence_registry",
        "council_run_id",
        "round_id",
    ]
    if lineage.get("request_contract") == prompt_producer.DCM_REQUEST_CONTRACT:
        field_names.extend(
            (
                "request_contract",
                "prompt_contract_sha256",
                "model_identity_receipt_sha256",
                "delivery_id",
                "dcm_session_id",
                "wave_id",
                "round",
                "phase",
                "prompt_id",
                "prompt_sha256",
                "request_revision",
                "parent_contribution_ids",
                "parent_frontier_sha256",
                "process_generation_expected",
                "model_endpoint",
                "requested_alias",
                "model_manifest_sha256",
                "model_content_sha256",
                "serving_container_digest",
            )
        )
    request_contract = {
        field_name: lineage[field_name]
        for field_name in field_names
        if field_name in lineage
    }
    return (
        "[COUNCIL REQUEST LINEAGE]\n"
        f"{json.dumps(request_contract, ensure_ascii=False, separators=(',', ':'))}\n"
        "[/COUNCIL REQUEST LINEAGE]\n\n"
        f"{executive._prompt_for(text, claims)}"
    )


def _contribution_response_format(
    lineage: dict[str, Any],
    seat: prompt_producer.SeatConfig,
) -> dict[str, Any]:
    return prompt_producer.response_format(
        seat,
        lineage["prompt_revision"],
        lineage["evidence_registry"],
    )


def _validated_contribution(
    reply: str,
    lineage: dict[str, Any],
) -> dict[str, Any]:
    try:
        contribution = json.loads(reply)
    except json.JSONDecodeError as exc:
        raise executive.SeatFailure(
            f"{RESPONSE_CONTRACT} reply is not valid JSON: {exc}"
        ) from exc
    if not isinstance(contribution, dict):
        raise executive.SeatFailure(f"{RESPONSE_CONTRACT} reply must be an object")
    actual_fields = set(contribution)
    expected_fields = set(CONTRIBUTION_FIELDS)
    if actual_fields != expected_fields:
        missing = sorted(expected_fields - actual_fields)
        unexpected = sorted(actual_fields - expected_fields)
        raise executive.SeatFailure(
            f"{RESPONSE_CONTRACT} fields mismatch "
            f"missing={missing} unexpected={unexpected}"
        )
    if (
        type(contribution["schema_version"]) is not int
        or contribution["schema_version"] != 1
    ):
        raise executive.SeatFailure(
            f"{RESPONSE_CONTRACT} schema_version must be integer 1"
        )
    if contribution["seat_id"] != executive.SESSION:
        raise executive.SeatFailure(
            f"{RESPONSE_CONTRACT} seat_id must be {executive.SESSION}"
        )
    if contribution["role_id"] != ROLE_ID:
        raise executive.SeatFailure(
            f"{RESPONSE_CONTRACT} role_id must be {ROLE_ID}"
        )
    if (
        type(contribution["prompt_revision"]) is not int
        or contribution["prompt_revision"] != lineage["prompt_revision"]
    ):
        raise executive.SeatFailure(
            f"{RESPONSE_CONTRACT} prompt_revision must be integer "
            f"{lineage['prompt_revision']}"
        )
    status = contribution["status"]
    if not isinstance(status, str) or not status.strip():
        raise executive.SeatFailure(
            f"{RESPONSE_CONTRACT} status must be a non-empty string"
        )
    for field_name in CONTRIBUTION_LIST_FIELDS:
        values = contribution[field_name]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise executive.SeatFailure(
                f"{RESPONSE_CONTRACT} {field_name} must be an array "
                "of non-empty strings"
            )
    unregistered_evidence = sorted(
        set(contribution["evidence_refs"]) - set(lineage["evidence_registry"])
    )
    if unregistered_evidence:
        raise executive.SeatFailure(
            f"{RESPONSE_CONTRACT} evidence_refs are not registered: "
            f"{unregistered_evidence}"
        )
    recommendation = contribution["recommendation"]
    if not isinstance(recommendation, str) or not recommendation.strip():
        raise executive.SeatFailure(
            f"{RESPONSE_CONTRACT} recommendation must be a non-empty string"
        )
    confidence = contribution["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise executive.SeatFailure(
            f"{RESPONSE_CONTRACT} confidence must be a number from 0 through 1"
        )
    return contribution


def _dcm_claim_observation(payload: dict[str, Any]) -> dict[str, Any]:
    if not os.environ.get("DCM_NEO4J_URI") or not os.environ.get(
        "DCM_NEO4J_DATABASE"
    ):
        raise executive.SeatFailure(
            "DCM_NEO4J_URI and DCM_NEO4J_DATABASE are required for a v2 request"
        )
    raw_aliases = os.environ.get("TAEY_MODEL_IDENTITY_EXPECTED_ALIASES", "")
    aliases = raw_aliases.split()
    if not aliases or len(aliases) != len(set(aliases)):
        raise executive.SeatFailure(
            "TAEY_MODEL_IDENTITY_EXPECTED_ALIASES must name unique served aliases"
        )
    upstream_endpoint = os.environ.get(
        "TAEY_MODEL_IDENTITY_UPSTREAM_COMPLETION_ENDPOINT",
        "",
    )
    if not upstream_endpoint:
        raise executive.SeatFailure(
            "TAEY_MODEL_IDENTITY_UPSTREAM_COMPLETION_ENDPOINT is required"
        )
    try:
        publication, _ = model_identity_status.read_publication(
            sorted(aliases),
            upstream_endpoint,
            False,
        )
    except (OSError, TypeError, ValueError, model_identity_status.VerificationError) as exc:
        raise executive.SeatFailure(
            f"live model identity could not be verified: {exc}"
        ) from exc
    receipt = publication["receipt"]
    requested_alias = payload.get("requested_alias")
    served_aliases = [item["id"] for item in receipt["serving"]["aliases"]]
    observed = {
        "seat_id": executive.SESSION,
        "process_generation_observed": PROCESS_GENERATION,
        "model_endpoint": executive.PROXY_URL,
        "served_alias": requested_alias,
        "model_manifest_sha256": receipt["model"]["model_manifest_sha256"],
        "model_content_sha256": receipt["model"]["model_content_sha256"],
        "serving_container_digest": receipt["serving"]["image_digest"],
        "prompt_contract_sha256": payload.get("prompt_contract_sha256"),
        "model_identity_receipt_sha256": publication["receipt_sha256"],
    }
    expected = {
        "requested_alias": requested_alias,
        "model_manifest_sha256": observed["model_manifest_sha256"],
        "model_content_sha256": observed["model_content_sha256"],
        "serving_container_digest": observed["serving_container_digest"],
        "model_identity_receipt_sha256": observed[
            "model_identity_receipt_sha256"
        ],
    }
    mismatched = [
        field for field, value in expected.items() if payload.get(field) != value
    ]
    if requested_alias not in served_aliases:
        mismatched.append("requested_alias")
    if mismatched:
        raise executive.SeatFailure(
            f"v2 request model identity differs from the signed live receipt: "
            f"{sorted(set(mismatched))}"
        )
    return observed


def _graph_contribution(request: dict[str, Any], contrib_id: str) -> dict[str, Any]:
    wave = dcm_adapter.mesh.read_wave(
        request["dcm_session_id"],
        request["wave_id"],
    )
    matches = [
        contribution
        for contribution in wave["contributions"]
        if contribution.get("contrib_id") == contrib_id
    ]
    if len(matches) != 1 or not isinstance(
        matches[0].get("structured_content"), dict
    ):
        raise executive.SeatFailure(
            "acknowledged graph contribution is missing or ambiguous"
        )
    return matches[0]["structured_content"]


def _run_dcm_turn(
    *,
    claim: executive.ClaimedMessage,
    inbox: CouncilReliableInbox,
    store: CouncilEventStore,
    proxy: executive.ProxyClient,
    liveness: SeatRegistrationLease,
    lineage: dict[str, Any],
    prompt: str,
    messages: list[dict[str, str]],
    contribution_format: dict[str, Any],
    attempt_fields: dict[str, Any],
    event_id: str,
    correlation_id: str,
    previously_attempted: bool,
) -> str:
    request = claim.payload

    def acknowledge(receipt: dict[str, Any]) -> None:
        liveness.assert_healthy()
        inbox.acknowledge_dcm(claim, receipt)

    wave = dcm_adapter.mesh.read_wave(
        request["dcm_session_id"],
        request["wave_id"],
    )
    matching_slots = [
        slot
        for slot in wave["slots"]
        if slot.get("request_id") == request.get("request_id")
    ]
    if len(matching_slots) != 1:
        raise executive.SeatFailure(
            "v2 request does not resolve to one graph-reserved role slot"
        )
    slot = matching_slots[0]
    if slot["state"] != "pending":
        recovered = dcm_adapter.recover_wave_request(
            request,
            acknowledge=acknowledge,
            emitter_process_generation=PROCESS_GENERATION,
            terminal_outcome=(
                "inference_failed" if previously_attempted else "dead_seat"
            ),
            inference_performed=previously_attempted,
            failure_stage="dead_generation_recovery",
            failure_detail=(
                "a prior seat generation ended after a durable model-attempt marker"
                if previously_attempted
                else "a prior seat generation ended before a durable model-attempt marker"
            ),
        )
        receipt = recovered["transport_receipt"]
        if receipt["terminal_outcome"] != "contributed":
            store.append(
                "turn_outcome",
                ok=False,
                event_id=event_id,
                correlation_id=correlation_id,
                message_ids=[claim.message_id],
                prompt=prompt,
                error=(
                    f"graph recovery closed request as "
                    f"{receipt['terminal_outcome']}"
                ),
                kind="dcm_graph_terminal_failure",
                skipped_inference=(
                    True
                    if (
                        receipt.get("terminal_outcome") == "session_failed"
                        and receipt.get("inference_state") == "not_started"
                    )
                    or receipt.get("inference_performed") is False
                    else False
                ),
                inference_state=(
                    receipt.get("inference_state")
                    if receipt.get("terminal_outcome") == "session_failed"
                    and receipt.get("inference_state")
                    in {"not_started", "side_effect_uncertain"}
                    else "completed"
                    if receipt.get("terminal_outcome") == "contributed"
                    else "failed"
                    if receipt.get("inference_performed") is True
                    else "not_started"
                ),
                dcm_transport_receipt=receipt,
                context_visible=False,
                conversation_visible=False,
                **lineage,
            )
            raise executive.SeatFailure(
                f"DCM request recovered as {receipt['terminal_outcome']}"
            )
        contribution = _graph_contribution(request, receipt["contrib_id"])
        reply = json.dumps(
            contribution,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        store.append(
            "turn_outcome",
            ok=True,
            event_id=event_id,
            correlation_id=correlation_id,
            proxy_turn_id=None,
            message_ids=[claim.message_id],
            prompt=prompt,
            reply=reply,
            contribution=contribution,
            role="assistant",
            content=reply,
            source=executive.SESSION,
            source_id=receipt["contrib_id"],
            kind="council_contribution",
            dcm_transport_receipt=receipt,
            context_visible=True,
            conversation_visible=False,
            **lineage,
        )
        store.remember_outcome(prompt, reply, [claim.message_id])
        return reply

    try:
        claim_observation = _dcm_claim_observation(request)
    except executive.SeatFailure as exc:
        terminal = dcm_adapter.recover_wave_request(
            request,
            acknowledge=acknowledge,
            emitter_process_generation=PROCESS_GENERATION,
            terminal_outcome="model_identity_unproven",
            inference_performed=False,
            failure_stage="model_identity_verification",
            failure_detail=f"{type(exc).__name__}: {exc}",
            duplicate_dispatch=False,
        )
        receipt = terminal["transport_receipt"]
        store.append(
            "turn_outcome",
            ok=False,
            event_id=event_id,
            correlation_id=correlation_id,
            message_ids=[claim.message_id],
            prompt=prompt,
            error=str(exc),
            kind="dcm_model_identity_terminal_failure",
            skipped_inference=True,
            inference_state="not_started",
            dcm_transport_receipt=receipt,
            context_visible=False,
            conversation_visible=False,
            **lineage,
        )
        raise

    invoked: dict[str, Any] = {}

    def invoke(_wave: dict[str, Any]) -> executive.ProxyResult:
        store.append("turn_attempt", **attempt_fields)
        liveness.assert_healthy()
        result = proxy.ask(
            prompt,
            event_id=event_id,
            correlation_id=correlation_id,
            messages=messages,
            response_format=contribution_format,
            max_rounds=prompt_producer.COUNCIL_MAX_TOOL_ROUNDS,
        )
        invoked["result"] = result
        liveness.assert_healthy()
        return result

    def validate_response(
        result: executive.ProxyResult,
        _wave: dict[str, Any],
    ) -> dict[str, Any]:
        contribution = _validated_contribution(result.reply, lineage)
        invoked["contribution"] = contribution
        return contribution

    try:
        transaction = dcm_adapter.execute_wave_request(
            request,
            claim_observation,
            invoke=invoke,
            validate_response=validate_response,
            acknowledge=acknowledge,
            emitter_component="taey-council-seat",
            emitter_process_generation=PROCESS_GENERATION,
        )
    except dcm_adapter.WaveRequestExecutionError as exc:
        receipt = exc.receipt
        store.append(
            "turn_outcome",
            ok=False,
            event_id=event_id,
            correlation_id=correlation_id,
            message_ids=[claim.message_id],
            prompt=prompt,
            error=f"{type(exc).__name__}: {exc}",
            kind="dcm_graph_terminal_failure",
            skipped_inference=False,
            inference_state="failed",
            dcm_transport_receipt=receipt,
            context_visible=False,
            conversation_visible=False,
            **lineage,
        )
        raise executive.SeatFailure(str(exc)) from exc

    terminal = transaction["graph"]
    receipt = transaction["transport_receipt"]
    contribution = _graph_contribution(request, terminal["contrib_id"])
    if invoked.get("contribution") is not None and (
        invoked["contribution"] != contribution
    ):
        raise executive.SeatFailure(
            "committed graph contribution differs from the validated model result"
        )
    reply = json.dumps(
        contribution,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    result = invoked.get("result")
    store.append(
        "turn_outcome",
        ok=True,
        event_id=event_id,
        correlation_id=correlation_id,
        proxy_turn_id=result.turn_id if result is not None else None,
        message_ids=[claim.message_id],
        prompt=prompt,
        reply=reply,
        contribution=contribution,
        role="assistant",
        content=reply,
        source=executive.SESSION,
        source_id=terminal["contrib_id"],
        kind="council_contribution",
        dcm_transport_receipt=receipt,
        context_visible=True,
        conversation_visible=False,
        **lineage,
    )
    store.remember_outcome(prompt, reply, [claim.message_id])
    return reply


def _ack_non_actionable_claims(
    claims: list[executive.ClaimedMessage],
    *,
    inbox: CouncilReliableInbox,
    store: CouncilEventStore,
    liveness: SeatRegistrationLease,
) -> str:
    liveness.assert_healthy()
    prompt = executive._format_claims(claims)
    reply = executive._format_non_actionable_reply(claims)
    event_id, correlation_id = executive._lineage(claims)
    message_ids = [claim.message_id for claim in claims]
    lineage = _response_lineage(
        claims,
        event_id,
        correlation_id,
        store.prompt_contract_sha256,
    )
    fields = {
        "event_id": event_id,
        "correlation_id": correlation_id,
        "message_ids": message_ids,
        "prompt": prompt,
        "skipped_inference": True,
        **lineage,
    }
    store.append("turn_attempt", **fields)
    store.append(
        "turn_outcome",
        ok=True,
        reply=reply,
        kind="council_non_actionable_ack",
        context_visible=False,
        conversation_visible=False,
        **fields,
    )
    liveness.assert_healthy()
    inbox.acknowledge(claims)
    store.completed_message_ids.update(message_ids)
    return reply


def _run_turn(
    text: str,
    *,
    inbox: CouncilReliableInbox,
    store: CouncilEventStore,
    proxy: executive.ProxyClient,
    liveness: SeatRegistrationLease,
    claims: list[executive.ClaimedMessage] | None = None,
) -> str:
    liveness.assert_healthy()
    claims = inbox.claim_available() if claims is None else list(claims)
    claims, skipped_claims = executive._split_actionable_claims(claims)
    skipped_reply = ""
    if skipped_claims:
        skipped_reply = _ack_non_actionable_claims(
            skipped_claims,
            inbox=inbox,
            store=store,
            liveness=liveness,
        )
    if not claims and executive._POINTER_RE.match(text):
        if skipped_reply:
            return skipped_reply
        inbox.release_pointer()
        return "[taey-council-seat] pointer contained no pending messages"
    for claim in claims:
        expected_generation = claim.payload.get("expected_process_generation")
        if claim.payload.get("type") != "council_request":
            raise executive.SeatFailure(
                f"unsupported actionable council message type "
                f"{claim.payload.get('type')!r} msg_id={claim.message_id}"
            )
        if expected_generation != PROCESS_GENERATION:
            raise SeatGenerationLost(
                f"council request expected process generation "
                f"{expected_generation or '<missing>'}, current generation is "
                f"{PROCESS_GENERATION}"
            )
    liveness.assert_healthy()
    event_id, correlation_id = executive._lineage(claims)
    request_contract, prompt_contract_sha256 = _request_contract(claims, store)
    lineage = _response_lineage(
        claims,
        event_id,
        correlation_id,
        prompt_contract_sha256,
        request_contract,
    )
    lineage["evidence_registry"] = store.evidence_registry(
        claims,
        event_id,
        prompt_contract_sha256,
    )
    prompt = _prompt_for(text, claims, lineage)
    message_ids = [claim.message_id for claim in claims]
    previously_attempted = any(
        message_id in store.attempted_message_ids for message_id in message_ids
    )
    messages = store.messages_for(prompt)
    contribution_format = _contribution_response_format(lineage, store.seat)
    model_request = proxy.model_request_body(
        messages,
        contribution_format,
        max_rounds=prompt_producer.COUNCIL_MAX_TOOL_ROUNDS,
    )
    producer_receipt = None
    if request_contract == prompt_producer.DCM_REQUEST_CONTRACT:
        try:
            producer_receipt = prompt_producer.model_request_receipt(
                manifest=store.manifest,
                seat=store.seat,
                lineage=lineage,
                model_request=model_request,
                claims=claims,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise executive.SeatFailure(
                f"cannot produce v2 model-request receipt: {exc}"
            ) from exc
    store.append(
        "council_ingress",
        event_id=event_id,
        correlation_id=correlation_id,
        message_ids=message_ids,
        source="fleet" if claims else "tmux",
        source_id=message_ids[0] if len(message_ids) == 1 else event_id,
        kind="council_request" if claims else "operator_probe",
        context_role="user",
        context_content=prompt,
        context_visible=True,
        conversation_visible=False,
        **lineage,
    )
    attempt_fields: dict[str, Any] = {
        "attempt_id": uuid.uuid4().hex,
        "event_id": event_id,
        "correlation_id": correlation_id,
        "message_ids": message_ids,
        "prompt": prompt,
        **lineage,
    }
    if producer_receipt is not None:
        attempt_fields["model_request_producer_receipt"] = producer_receipt
    if request_contract == prompt_producer.DCM_REQUEST_CONTRACT:
        return _run_dcm_turn(
            claim=claims[0],
            inbox=inbox,
            store=store,
            proxy=proxy,
            liveness=liveness,
            lineage=lineage,
            prompt=prompt,
            messages=messages,
            contribution_format=contribution_format,
            attempt_fields=attempt_fields,
            event_id=event_id,
            correlation_id=correlation_id,
            previously_attempted=previously_attempted,
        )
    store.append("turn_attempt", **attempt_fields)
    inference_state = "side_effect_uncertain"
    try:
        liveness.assert_healthy()
        result = proxy.ask(
            prompt,
            event_id=event_id,
            correlation_id=correlation_id,
            messages=messages,
            response_format=contribution_format,
            max_rounds=prompt_producer.COUNCIL_MAX_TOOL_ROUNDS,
        )
        inference_state = "completed_invalid"
        liveness.assert_healthy()
        contribution = _validated_contribution(result.reply, lineage)
        reply = json.dumps(
            contribution,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        store.append(
            "turn_outcome",
            ok=True,
            event_id=event_id,
            correlation_id=correlation_id,
            proxy_turn_id=result.turn_id,
            message_ids=message_ids,
            prompt=prompt,
            reply=reply,
            contribution=contribution,
            role="assistant",
            content=reply,
            source=executive.SESSION,
            source_id=result.turn_id,
            kind="council_contribution",
            context_visible=True,
            conversation_visible=False,
            **lineage,
        )
    except SeatGenerationLost:
        raise
    except Exception as exc:
        try:
            store.append(
                "turn_outcome",
                ok=False,
                event_id=event_id,
                correlation_id=correlation_id,
                message_ids=message_ids,
                prompt=prompt,
                error=f"{type(exc).__name__}: {exc}",
                kind="council_generation_terminal_failure",
                skipped_inference=False,
                inference_state=inference_state,
                context_visible=False,
                conversation_visible=False,
                **lineage,
            )
            liveness.assert_healthy()
            inbox.acknowledge(claims)
            store.completed_message_ids.update(message_ids)
        except Exception as recovery_exc:
            raise executive.SeatFailure(
                f"turn failed ({exc}); durable terminalization failed "
                f"({recovery_exc})"
            ) from recovery_exc
        raise
    liveness.assert_healthy()
    store.remember_outcome(prompt, reply, message_ids)
    inbox.acknowledge(claims)
    return reply


def _idle_poll_seconds() -> float:
    raw = os.environ.get(
        "TAEY_COUNCIL_IDLE_POLL_SECONDS",
        str(DEFAULT_IDLE_POLL_SECONDS),
    )
    try:
        value = float(raw)
    except ValueError as exc:
        raise executive.SeatFailure(
            f"TAEY_COUNCIL_IDLE_POLL_SECONDS must be a number, got {raw!r}"
        ) from exc
    if value < 0:
        raise executive.SeatFailure(
            "TAEY_COUNCIL_IDLE_POLL_SECONDS must be non-negative"
        )
    return value


def _liveness_timing() -> tuple[int, float]:
    raw_ttl = os.environ.get(
        "TAEY_COUNCIL_LIVENESS_TTL_SECONDS",
        str(DEFAULT_LIVENESS_TTL_SECONDS),
    )
    raw_refresh = os.environ.get(
        "TAEY_COUNCIL_LIVENESS_REFRESH_SECONDS",
        str(DEFAULT_LIVENESS_REFRESH_SECONDS),
    )
    try:
        ttl_seconds = int(raw_ttl)
        refresh_seconds = float(raw_refresh)
    except ValueError as exc:
        raise executive.SeatFailure(
            "TAEY_COUNCIL_LIVENESS_TTL_SECONDS must be an integer and "
            "TAEY_COUNCIL_LIVENESS_REFRESH_SECONDS must be a number"
        ) from exc
    if ttl_seconds < 2:
        raise executive.SeatFailure(
            "TAEY_COUNCIL_LIVENESS_TTL_SECONDS must be at least 2"
        )
    if refresh_seconds <= 0 or refresh_seconds >= ttl_seconds:
        raise executive.SeatFailure(
            "TAEY_COUNCIL_LIVENESS_REFRESH_SECONDS must be greater than 0 "
            "and less than TAEY_COUNCIL_LIVENESS_TTL_SECONDS"
        )
    return ttl_seconds, refresh_seconds


class SeatGenerationLost(executive.SeatFailure):
    pass


class SeatRegistrationLease:
    def __init__(
        self,
        client: redis.Redis,
        registration_key: str,
        registration: str,
        ttl_seconds: int,
        refresh_seconds: float,
    ):
        self.client = client
        self.registration_key = registration_key
        self.registration = registration
        self.ttl_seconds = ttl_seconds
        self.refresh_seconds = refresh_seconds
        self._stop = threading.Event()
        self._failure: Exception | None = None
        self._thread = threading.Thread(
            target=self._refresh_loop,
            name=f"{executive.SESSION}-liveness",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.refresh_seconds + 1.0)

    def assert_healthy(self) -> None:
        if self._failure is not None:
            raise SeatGenerationLost(
                f"seat registration lease failed: {self._failure}"
            ) from self._failure
        if not self._thread.is_alive() and not self._stop.is_set():
            raise SeatGenerationLost(
                "seat registration lease refresher stopped unexpectedly"
            )
        try:
            current_registration = self.client.get(self.registration_key)
        except Exception as exc:
            raise SeatGenerationLost(
                f"seat registration lease cannot be verified: {exc}"
            ) from exc
        if current_registration != self.registration:
            raise SeatGenerationLost(
                "seat registration lease is expired or owned by another generation"
            )

    def _refresh_loop(self) -> None:
        while not self._stop.wait(self.refresh_seconds):
            try:
                renewed = int(
                    self.client.eval(
                        _REFRESH_REGISTRATION_LUA,
                        1,
                        self.registration_key,
                        self.registration,
                        self.ttl_seconds,
                    )
                )
                if renewed != 1:
                    raise SeatGenerationLost(
                        "registration was expired or replaced by another generation"
                    )
            except Exception as exc:
                self._failure = exc
                return


class CouncilReliableInbox(executive.ReliableInbox):
    def __init__(
        self,
        client: redis.Redis,
        store: CouncilEventStore,
        registration_key: str,
        registration: str,
    ):
        super().__init__(
            client,
            store,
            processing_generation=PROCESS_GENERATION,
            claim_guard=(registration_key, registration),
            claim_block_key=(
                f"{executive.KEY_PREFIX}:dcm:native:seat_replacement"
            ),
        )
        self.registration_key = registration_key
        self.registration = registration

    def _mutate_owned_claim(
        self,
        claim: executive.ClaimedMessage,
        action: str,
    ) -> int:
        return int(
            self.client.eval(
                _MUTATE_OWNED_CLAIM_LUA,
                3,
                self.registration_key,
                claim.source.processing_key,
                claim.source.queue_key,
                self.registration,
                claim.raw,
                action,
                claim.source.requeue_side,
            )
        )

    def _ack(self, claim: executive.ClaimedMessage) -> None:
        if self._mutate_owned_claim(claim, "ack") != 1:
            raise executive.SeatFailure(
                f"ack lost generation-owned claim source={claim.source.name} "
                f"msg_id={claim.message_id}"
            )

    def acknowledge_dcm(
        self,
        claim: executive.ClaimedMessage,
        receipt: dict[str, Any],
    ) -> None:
        round_id = claim.payload.get("dcm_session_id")
        if (
            receipt.get("stage") != "terminal_acknowledged"
            or receipt.get("session_id") != round_id
            or receipt.get("request_id") != claim.payload.get("request_id")
            or receipt.get("delivery_id") != claim.message_id
        ):
            raise executive.SeatFailure(
                f"DCM acknowledgement does not match msg_id={claim.message_id}"
            )
        encoded = json.dumps(
            receipt,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        removed = int(
            self.client.eval(
                _ACK_DCM_OWNED_CLAIM_LUA,
                3,
                self.registration_key,
                claim.source.processing_key,
                f"{executive.KEY_PREFIX}:dcm:native:round:{round_id}:results",
                self.registration,
                claim.raw,
                encoded,
            )
        )
        if removed != 1:
            raise executive.SeatFailure(
                f"DCM ack lost generation-owned claim source={claim.source.name} "
                f"msg_id={claim.message_id}"
            )
        self.store.completed_message_ids.add(claim.message_id)

    def _requeue(self, claim: executive.ClaimedMessage) -> None:
        if self._mutate_owned_claim(claim, "requeue") != 1:
            raise executive.SeatFailure(
                f"requeue lost generation-owned claim source={claim.source.name} "
                f"msg_id={claim.message_id}"
            )

    def _terminalize_dead_generation(
        self,
        claim: executive.ClaimedMessage,
    ) -> None:
        payload = claim.payload
        request_id = executive._safe_trace_id(
            payload.get("request_id")
            or payload.get("event_id")
            or claim.message_id,
            claim.message_id,
        )
        correlation_id = executive._safe_trace_id(
            payload.get("correlation_id")
            or payload.get("round_id")
            or request_id,
            request_id,
        )
        try:
            prompt_revision = int(payload.get("prompt_revision") or 1)
        except (TypeError, ValueError):
            prompt_revision = 1
        dead_generation = str(
            payload.get("expected_process_generation") or "legacy-unbound"
        )
        inference_attempted = claim.message_id in self.store.attempted_message_ids
        if payload.get("request_contract") == prompt_producer.DCM_REQUEST_CONTRACT:
            recovered = dcm_adapter.recover_wave_request(
                payload,
                acknowledge=lambda receipt: self.acknowledge_dcm(claim, receipt),
                emitter_process_generation=PROCESS_GENERATION,
                terminal_outcome=(
                    "inference_failed" if inference_attempted else "dead_seat"
                ),
                inference_performed=inference_attempted,
                failure_stage="dead_generation_recovery",
                failure_detail=(
                    "dead generation left a durable model-attempt marker"
                    if inference_attempted
                    else "dead generation ended before a durable model-attempt marker"
                ),
            )
            receipt = recovered["transport_receipt"]
            if receipt["terminal_outcome"] == "contributed":
                contribution = _graph_contribution(payload, receipt["contrib_id"])
                reply = json.dumps(
                    contribution,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                self.store.append(
                    "turn_outcome",
                    ok=True,
                    event_id=request_id,
                    correlation_id=correlation_id,
                    message_ids=[claim.message_id],
                    request_id=request_id,
                    council_run_id=payload.get("council_run_id"),
                    round_id=payload.get("round_id"),
                    prompt_revision=prompt_revision,
                    seat_id=executive.SESSION,
                    role_id=ROLE_ID,
                    process_generation=dead_generation,
                    recovered_by_process_generation=PROCESS_GENERATION,
                    kind="dcm_contribution_recovered",
                    contribution=contribution,
                    reply=reply,
                    dcm_transport_receipt=receipt,
                    skipped_inference=True,
                    inference_state="not_started",
                    context_visible=False,
                    conversation_visible=False,
                )
            else:
                self.store.append(
                    "turn_outcome",
                    ok=False,
                    event_id=request_id,
                    correlation_id=correlation_id,
                    message_ids=[claim.message_id],
                    request_id=request_id,
                    council_run_id=payload.get("council_run_id"),
                    round_id=payload.get("round_id"),
                    prompt_revision=prompt_revision,
                    seat_id=executive.SESSION,
                    role_id=ROLE_ID,
                    process_generation=dead_generation,
                    recovered_by_process_generation=PROCESS_GENERATION,
                    kind="dcm_dead_generation_terminal",
                    dcm_transport_receipt=receipt,
                    skipped_inference=(
                        True
                        if (
                            receipt.get("terminal_outcome") == "session_failed"
                            and receipt.get("inference_state") == "not_started"
                        )
                        or receipt.get("inference_performed") is False
                        else False
                    ),
                    inference_state=(
                        receipt.get("inference_state")
                        if receipt.get("terminal_outcome") == "session_failed"
                        and receipt.get("inference_state")
                        in {"not_started", "side_effect_uncertain"}
                        else "completed"
                        if receipt.get("terminal_outcome") == "contributed"
                        else "failed"
                        if receipt.get("inference_performed") is True
                        else "not_started"
                    ),
                    error=(
                        f"graph recovery closed request as "
                        f"{receipt['terminal_outcome']}"
                    ),
                    context_visible=False,
                    conversation_visible=False,
                )
            return
        self.store.append(
            "turn_outcome",
            ok=False,
            event_id=request_id,
            correlation_id=correlation_id,
            message_ids=[claim.message_id],
            request_id=request_id,
            council_run_id=payload.get("council_run_id"),
            round_id=payload.get("round_id"),
            prompt_revision=prompt_revision,
            seat_id=executive.SESSION,
            role_id=ROLE_ID,
            process_generation=dead_generation,
            recovered_by_process_generation=PROCESS_GENERATION,
            kind="dead_generation_terminal",
            skipped_inference=not inference_attempted,
            inference_state=(
                "side_effect_uncertain" if inference_attempted else "not_started"
            ),
            error=(
                "request owner generation terminated after a durable inference "
                "attempt but before a durable outcome"
                if inference_attempted
                else "request owner generation terminated before inference"
            ),
            context_visible=False,
            conversation_visible=False,
        )
        self._ack(claim)
        self.store.completed_message_ids.add(claim.message_id)

    def _claim_stale_queued_request(
        self,
        source: executive.QueueSpec,
    ) -> executive.ClaimedMessage | None:
        raw = self.client.eval(
            _CLAIM_STALE_GENERATION_LUA,
            3,
            self.registration_key,
            source.queue_key,
            source.processing_key,
            self.registration,
            PROCESS_GENERATION,
            source.processing_side,
        )
        if raw is None:
            return None
        payload, message_id = executive._decode_message(raw)
        return executive.ClaimedMessage(source, raw, payload, message_id)

    def recover(self) -> dict[str, int]:
        terminalized = 0
        acknowledged = 0
        current_processing_keys = {
            source.name: source.processing_key for source in self.queues
        }
        for source in executive.QUEUES:
            candidate_keys = {
                source.processing_key,
                *self.client.scan_iter(match=f"{source.processing_key}:*"),
            }
            candidate_keys.discard(current_processing_keys[source.name])
            for processing_key in sorted(candidate_keys):
                raws = list(self.client.lrange(processing_key, 0, -1))
                if source.processing_side == "RIGHT":
                    raws.reverse()
                owned_source = executive.QueueSpec(
                    name=source.name,
                    queue_key=source.queue_key,
                    processing_key=processing_key,
                    source_side=source.source_side,
                    processing_side=source.processing_side,
                    requeue_side=source.requeue_side,
                )
                for raw in raws:
                    payload, message_id = executive._decode_message(raw)
                    claim = executive.ClaimedMessage(
                        owned_source,
                        raw,
                        payload,
                        message_id,
                    )
                    if message_id in self.store.completed_message_ids:
                        self._ack(claim)
                        acknowledged += 1
                    else:
                        self._terminalize_dead_generation(claim)
                        terminalized += 1
        for source in self.queues:
            while True:
                stale_claim = self._claim_stale_queued_request(source)
                if stale_claim is None:
                    break
                if stale_claim.message_id in self.store.completed_message_ids:
                    self._ack(stale_claim)
                    acknowledged += 1
                else:
                    self._terminalize_dead_generation(stale_claim)
                    terminalized += 1
        self.client.delete(executive.POINTER_BACKOFF_KEY)
        return {
            "terminalized": terminalized,
            "acknowledged": acknowledged,
        }


def _claimed_pointer_text(claims: list[executive.ClaimedMessage]) -> str:
    return f"[NOTIFY] You have {len(claims)} messages"


def _serve_next_inbox_turn(
    *,
    inbox: CouncilReliableInbox,
    store: CouncilEventStore,
    proxy: executive.ProxyClient,
    liveness: SeatRegistrationLease,
) -> str | None:
    liveness.assert_healthy()
    claims = inbox.claim_available()
    if not claims:
        return None
    return _run_turn(
        _claimed_pointer_text(claims),
        inbox=inbox,
        store=store,
        proxy=proxy,
        liveness=liveness,
        claims=claims,
    )


def _serve_inbox_loop(
    *,
    inbox: CouncilReliableInbox,
    store: CouncilEventStore,
    proxy: executive.ProxyClient,
    liveness: SeatRegistrationLease,
    poll_seconds: float,
    idle_sleep: Callable[[float], None] = time.sleep,
    max_turns: int | None = None,
) -> int:
    completed_turns = 0
    while True:
        liveness.assert_healthy()
        reply = _serve_next_inbox_turn(
            inbox=inbox,
            store=store,
            proxy=proxy,
            liveness=liveness,
        )
        liveness.assert_healthy()
        if reply is None:
            idle_sleep(poll_seconds)
            continue
        print(reply, flush=True)
        completed_turns += 1
        if max_turns is not None and completed_turns >= max_turns:
            return 0


def _register_at_rest_liveness(
    client: redis.Redis,
    store: CouncilEventStore,
    ttl_seconds: int,
) -> tuple[int, str, str]:
    prefix = f"{executive.KEY_PREFIX}:{executive.SESSION}"
    registered_at = time.time()
    registration_fields = {
        "schema_version": 1,
        "seat_id": executive.SESSION,
        "seat_kind": "council",
        "role_id": ROLE_ID,
        "conversation_id": executive.CONVERSATION_ID,
        "event_log": str(executive.EVENT_LOG),
        "process_generation": PROCESS_GENERATION,
        "role_contract_revision": ROLE_CONTRACT_REVISION,
        "prompt_contract_sha256": store.prompt_contract_sha256,
        "dcm_v2_prompt_contract_sha256": store.dcm_v2_prompt_contract_sha256,
        "prompt_contract_producer_state": "self_asserted_unverified",
        "response_contract": RESPONSE_CONTRACT,
        "model_endpoint": executive.PROXY_URL,
        "requested_alias": executive.MODEL,
        "liveness_ttl_seconds": ttl_seconds,
        "pid": os.getpid(),
        "registered_at": registered_at,
    }
    provisional_registration = json.dumps(
        {**registration_fields, "readiness": "recovering"},
        separators=(",", ":"),
    )
    ready_registration = json.dumps(
        {**registration_fields, "readiness": "ready"},
        separators=(",", ":"),
    )
    active_turns = int(
        client.eval(
            _REGISTER_AT_REST_LUA,
            6,
            f"{prefix}:active_turns",
            f"{prefix}:turns_open",
            f"{prefix}:idle",
            f"{prefix}:turn_started",
            f"{executive.KEY_PREFIX}:soma:seat_ids",
            f"{prefix}:seat_registration",
            executive.SESSION,
            provisional_registration,
            ttl_seconds,
        )
    )
    return active_turns, provisional_registration, ready_registration


def main() -> int:
    try:
        manifest, seat, seat_contract = _validate_seat_contract()
        ttl_seconds, refresh_seconds = _liveness_timing()
        poll_seconds = _idle_poll_seconds()
        store = CouncilEventStore(
            executive.EVENT_LOG,
            executive.MAX_TURNS,
            manifest,
            seat,
            seat_contract,
        )
        client = executive._redis_client()
        store.append(
            "seat_started",
            seat_id=executive.SESSION,
            seat_kind="council",
            role_id=ROLE_ID,
            process_generation=PROCESS_GENERATION,
            role_contract_revision=ROLE_CONTRACT_REVISION,
            prompt_contract_sha256=store.prompt_contract_sha256,
            dcm_v2_prompt_contract_sha256=(
                store.dcm_v2_prompt_contract_sha256
            ),
            prompt_contract_producer_state="self_asserted_unverified",
            response_contract=RESPONSE_CONTRACT,
            conversation_visible=False,
        )
        active_turns, provisional_registration, ready_registration = (
            _register_at_rest_liveness(
                client,
                store,
                ttl_seconds,
            )
        )
        registration_key = (
            f"{executive.KEY_PREFIX}:{executive.SESSION}:seat_registration"
        )
        recovery_liveness = SeatRegistrationLease(
            client,
            registration_key,
            provisional_registration,
            ttl_seconds,
            refresh_seconds,
        )
        recovery_liveness.start()
        recovery_inbox = CouncilReliableInbox(
            client,
            store,
            registration_key,
            provisional_registration,
        )
        recovery = recovery_inbox.recover()
        recovery_liveness.assert_healthy()
        recovery_liveness.stop()
        promoted = int(
            client.eval(
                _PROMOTE_READY_REGISTRATION_LUA,
                1,
                registration_key,
                provisional_registration,
                ready_registration,
                ttl_seconds,
            )
        )
        if promoted != 1:
            raise SeatGenerationLost(
                "provisional registration was lost before ready promotion"
            )
        liveness = SeatRegistrationLease(
            client,
            registration_key,
            ready_registration,
            ttl_seconds,
            refresh_seconds,
        )
        liveness.start()
        inbox = CouncilReliableInbox(
            client,
            store,
            registration_key,
            ready_registration,
        )
    except Exception as exc:
        print(
            f"[taey-council-seat] FATAL startup: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(
        f"[taey-council-seat] session={executive.SESSION} role={ROLE_ID} "
        f"proxy={executive.PROXY_URL} event_log={executive.EVENT_LOG} "
        f"generation={PROCESS_GENERATION} active_turns={active_turns} "
        f"recovered={recovery}",
        flush=True,
    )
    proxy = executive.ProxyClient()
    try:
        try:
            return _serve_inbox_loop(
                inbox=inbox,
                store=store,
                proxy=proxy,
                liveness=liveness,
                poll_seconds=poll_seconds,
            )
        except Exception as exc:
            print(
                f"[taey-council-seat] FATAL turn: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 1
    finally:
        liveness.stop()


if __name__ == "__main__":
    raise SystemExit(main())
