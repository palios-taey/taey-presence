#!/usr/bin/env python3
"""Private supporting-seat runtime for Taey's seven-seat local council."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import redis

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
SHARED_PROMPT_PATH = Path(SHARED_PROMPT_VALUE).expanduser()
ROLE_PROMPT_PATH = Path(ROLE_PROMPT_VALUE).expanduser()
RESPONSE_CONTRACT = "taey-council-contribution/v1"
ROLE_CONTRACT_REVISION = 1
DEFAULT_PROMPT_REVISION = 1
DEFAULT_IDLE_POLL_SECONDS = 0.25
DEFAULT_LIVENESS_TTL_SECONDS = 5
DEFAULT_LIVENESS_REFRESH_SECONDS = 1.0
PROCESS_GENERATION = uuid.uuid4().hex
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
_COUNCIL_SEAT_RE = re.compile(r"^taey-council-[1-7]$")
ROLE_BY_SEAT = {
    "taey-council-1": "context-memory",
    "taey-council-2": "evidence-reality",
    "taey-council-3": "systems-dependencies",
    "taey-council-4": "adversarial-failure",
    "taey-council-5": "scope-intent",
    "taey-council-6": "options-alternatives",
    "taey-council-7": "control-acceptance",
}

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


def _read_prompt_file(value: str, path: Path, env_name: str) -> str:
    if not value:
        raise executive.SeatFailure(f"{env_name} is required")
    if not path.is_file():
        raise executive.SeatFailure(f"{env_name} is not a readable file: {path}")
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise executive.SeatFailure(f"{env_name} is empty: {path}")
    return content


def _seat_role_contract() -> str:
    shared_prompt = _read_prompt_file(
        SHARED_PROMPT_VALUE,
        SHARED_PROMPT_PATH,
        "TAEY_COUNCIL_SHARED_PROMPT_PATH",
    )
    role_prompt = _read_prompt_file(
        ROLE_PROMPT_VALUE,
        ROLE_PROMPT_PATH,
        "TAEY_COUNCIL_ROLE_PROMPT_PATH",
    )
    return (
        f"Immutable runtime identity: seat_id={executive.SESSION}; "
        f"role_id={ROLE_ID}; response_contract={RESPONSE_CONTRACT}; "
        f"role_contract_revision={ROLE_CONTRACT_REVISION}.\n\n"
        f"{shared_prompt}\n\n{role_prompt}"
    )


def _validate_seat_contract() -> str:
    if not _COUNCIL_SEAT_RE.fullmatch(executive.SESSION):
        raise executive.SeatFailure(
            "TAEY_SESSION_NAME must be taey-council-1 through taey-council-7"
        )
    expected_role = ROLE_BY_SEAT[executive.SESSION]
    if ROLE_ID != expected_role:
        raise executive.SeatFailure(
            f"{executive.SESSION} requires TAEY_COUNCIL_ROLE_ID={expected_role}, "
            f"got {ROLE_ID or '<empty>'}"
        )
    expected_conversation = f"council-{expected_role}"
    if executive.CONVERSATION_ID != expected_conversation:
        raise executive.SeatFailure(
            f"{executive.SESSION} requires "
            f"TAEY_CONVERSATION_ID={expected_conversation}, "
            f"got {executive.CONVERSATION_ID}"
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
    return _seat_role_contract()


class CouncilEventStore(executive.EventStore):
    def __init__(self, path: Path, max_turns: int, seat_contract: str):
        self.seat_contract = seat_contract
        self.attempted_message_ids: set[str] = set()
        self.prompt_contract_sha256 = hashlib.sha256(
            seat_contract.encode("utf-8")
        ).hexdigest()
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
        contract = (
            "[COUNCIL ROLE CONTRACT]\n"
            f"{self.seat_contract}\n"
            "[/COUNCIL ROLE CONTRACT]"
        )
        return [
            {"role": "system", "content": contract},
            {"role": "user", "content": prompt},
        ]

    def evidence_registry(
        self,
        claims: list[executive.ClaimedMessage],
        event_id: str,
    ) -> list[str]:
        references = [f"role_contract:{self.prompt_contract_sha256}"]
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
    for field_name in ("council_run_id", "round_id"):
        value = payload.get(field_name)
        if value:
            lineage[field_name] = executive._safe_trace_id(
                value,
                correlation_id,
            )
    return lineage


def _prompt_for(
    text: str,
    claims: list[executive.ClaimedMessage],
    lineage: dict[str, Any],
) -> str:
    request_contract = {
        field_name: lineage[field_name]
        for field_name in (
            "seat_id",
            "role_id",
            "request_id",
            "response_contract",
            "prompt_revision",
            "evidence_registry",
            "council_run_id",
            "round_id",
        )
        if field_name in lineage
    }
    return (
        "[COUNCIL REQUEST LINEAGE]\n"
        f"{json.dumps(request_contract, ensure_ascii=False, separators=(',', ':'))}\n"
        "[/COUNCIL REQUEST LINEAGE]\n\n"
        f"{executive._prompt_for(text, claims)}"
    )


def _contribution_response_format(lineage: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "schema_version": {"type": "integer", "const": 1},
        "seat_id": {"type": "string", "const": executive.SESSION},
        "role_id": {"type": "string", "const": ROLE_ID},
        "status": {"type": "string", "minLength": 1},
        "prompt_revision": {
            "type": "integer",
            "const": lineage["prompt_revision"],
        },
        "recommendation": {"type": "string", "minLength": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    }
    for field_name in CONTRIBUTION_LIST_FIELDS:
        item_schema: dict[str, Any] = {"type": "string", "minLength": 1}
        if field_name == "evidence_refs":
            item_schema["enum"] = lineage["evidence_registry"]
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
    lineage = _response_lineage(
        claims,
        event_id,
        correlation_id,
        store.prompt_contract_sha256,
    )
    lineage["evidence_registry"] = store.evidence_registry(claims, event_id)
    prompt = _prompt_for(text, claims, lineage)
    message_ids = [claim.message_id for claim in claims]
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
    store.append(
        "turn_attempt",
        attempt_id=uuid.uuid4().hex,
        event_id=event_id,
        correlation_id=correlation_id,
        message_ids=message_ids,
        prompt=prompt,
        **lineage,
    )
    inference_state = "side_effect_uncertain"
    try:
        liveness.assert_healthy()
        result = proxy.ask(
            prompt,
            event_id=event_id,
            correlation_id=correlation_id,
            messages=store.messages_for(prompt),
            response_format=_contribution_response_format(lineage),
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
        "response_contract": RESPONSE_CONTRACT,
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
        seat_contract = _validate_seat_contract()
        ttl_seconds, refresh_seconds = _liveness_timing()
        poll_seconds = _idle_poll_seconds()
        store = CouncilEventStore(
            executive.EVENT_LOG,
            executive.MAX_TURNS,
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
