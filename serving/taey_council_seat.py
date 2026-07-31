#!/usr/bin/env python3
"""Private supporting-seat runtime for Taey's seven-seat local council."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

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
local count = redis.call('ZCARD', KEYS[1])
redis.call('SET', KEYS[2], count)
redis.call('SADD', KEYS[5], ARGV[1])
redis.call('SET', KEYS[6], ARGV[2])
if count == 0 then
    redis.call('SET', KEYS[3], '1')
    redis.call('DEL', KEYS[4])
else
    redis.call('DEL', KEYS[3])
end
return count
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
        self.prompt_contract_sha256 = hashlib.sha256(
            seat_contract.encode("utf-8")
        ).hexdigest()
        super().__init__(path, max_turns)

    def messages_for(self, prompt: str) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        recorded_prompt = False
        seen_ingress: set[str] = set()
        for event in self._read_events():
            event_type = event.get("event_type")
            if event_type == "council_ingress":
                event_id = str(event.get("event_id") or "")
                if event_id and event_id in seen_ingress:
                    continue
                if event_id:
                    seen_ingress.add(event_id)
                context_role = event.get("context_role")
                context_content = event.get("context_content")
                if (
                    event.get("context_visible") is not False
                    and context_role in {"user", "assistant"}
                    and isinstance(context_content, str)
                    and context_content
                ):
                    history.append(
                        {"role": context_role, "content": context_content}
                    )
                    if context_role == "user" and context_content == prompt:
                        recorded_prompt = True
                continue
            if (
                event_type == "turn_outcome"
                and event.get("ok")
                and event.get("context_visible") is not False
            ):
                prior_reply = event.get("reply")
                if isinstance(prior_reply, str) and prior_reply:
                    history.append({"role": "assistant", "content": prior_reply})
        if not recorded_prompt:
            history.append({"role": "user", "content": prompt})
        messages = history[-(self.max_turns * 2):]
        contract = (
            "[COUNCIL ROLE CONTRACT]\n"
            f"{self.seat_contract}\n"
            "[/COUNCIL ROLE CONTRACT]"
        )
        return [{"role": "system", "content": contract}, *messages]

    def evidence_registry(
        self,
        claims: list[executive.ClaimedMessage],
        event_id: str,
    ) -> list[str]:
        references = [f"role_contract:{self.prompt_contract_sha256}"]
        history_references: list[str] = []
        for event in self._read_events():
            if (
                event.get("event_type") == "turn_outcome"
                and event.get("ok")
                and event.get("context_visible") is not False
            ):
                prior_event_id = str(event.get("event_id") or "")
                if prior_event_id:
                    history_references.append(
                        "history_event:"
                        f"{executive._safe_trace_id(prior_event_id, event_id)}"
                    )
        references.extend(history_references[-self.max_turns :])
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
    inbox: executive.ReliableInbox,
    store: CouncilEventStore,
) -> str:
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
    inbox.acknowledge(claims)
    store.completed_message_ids.update(message_ids)
    return reply


def _run_turn(
    text: str,
    *,
    inbox: executive.ReliableInbox,
    store: CouncilEventStore,
    proxy: executive.ProxyClient,
) -> str:
    claims = inbox.claim_available()
    claims, skipped_claims = executive._split_actionable_claims(claims)
    skipped_reply = ""
    if skipped_claims:
        skipped_reply = _ack_non_actionable_claims(
            skipped_claims,
            inbox=inbox,
            store=store,
        )
    if not claims and executive._POINTER_RE.match(text):
        if skipped_reply:
            return skipped_reply
        inbox.release_pointer()
        return "[taey-council-seat] pointer contained no pending messages"
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
    try:
        result = proxy.ask(
            prompt,
            event_id=event_id,
            correlation_id=correlation_id,
            messages=store.messages_for(prompt),
            response_format=_contribution_response_format(lineage),
        )
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
                context_visible=False,
                conversation_visible=False,
                **lineage,
            )
            inbox.requeue(claims)
        except Exception as recovery_exc:
            raise executive.SeatFailure(
                f"turn failed ({exc}); durable recovery failed ({recovery_exc})"
            ) from recovery_exc
        raise
    store.remember_outcome(prompt, reply, message_ids)
    inbox.acknowledge(claims)
    return reply


def _register_at_rest_liveness(
    client: redis.Redis,
    store: CouncilEventStore,
) -> int:
    prefix = f"{executive.KEY_PREFIX}:{executive.SESSION}"
    registration = json.dumps(
        {
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
            "pid": os.getpid(),
            "registered_at": time.time(),
        },
        separators=(",", ":"),
    )
    return int(
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
            registration,
        )
    )


def main() -> int:
    try:
        seat_contract = _validate_seat_contract()
        store = CouncilEventStore(
            executive.EVENT_LOG,
            executive.MAX_TURNS,
            seat_contract,
        )
        client = executive._redis_client()
        inbox = executive.ReliableInbox(client, store)
        recovery = inbox.recover()
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
        active_turns = _register_at_rest_liveness(client, store)
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
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            return 0
        try:
            reply = _run_turn(
                text,
                inbox=inbox,
                store=store,
                proxy=proxy,
            )
        except Exception as exc:
            print(
                f"[taey-council-seat] FATAL turn: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 1
        print(reply, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
