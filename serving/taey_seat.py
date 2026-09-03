#!/usr/bin/env python3
"""Durable tmux-hosted Taey executive seat.

The tmux pane is a transport and operator console. Conversation truth lives in
an fsync'd event log, fleet mail is claimed into Redis processing queues before
inference, and mail is acknowledged only after its outcome is durable.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import redis

if __package__:
    from .outbound_request_codec import bind_outbound_request_bytes
else:
    from outbound_request_codec import bind_outbound_request_bytes


PROXY_URL = os.environ.get(
    "TAEY_SEAT_PROXY",
    "http://127.0.0.1:8766/v1/chat/completions",
)
SESSION = os.environ.get("TAEY_SESSION_NAME", "taey")
KEY_PREFIX = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
MAX_TURNS = max(1, int(os.environ.get("TAEY_SEAT_MAX_TURNS", "60")))
MODEL = os.environ.get("TAEY_MODEL", "ep3")
CONVERSATION_ID = os.environ.get("TAEY_CONVERSATION_ID", "main")
_SEAT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_POINTER_RE = re.compile(r"^\[NOTIFY\]\s+You have \d+ messages?\b")
_TEXTUAL_TOOL_INTENT_RE = re.compile(r"<tool_call(?:>|\s)", re.IGNORECASE)


class CompletionContractError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _contains_textual_tool_intent(reply: str) -> bool:
    if _TEXTUAL_TOOL_INTENT_RE.search(reply):
        return True
    try:
        value = json.loads(reply)
    except json.JSONDecodeError:
        return False
    if not isinstance(value, dict):
        return False
    function = value.get("function", value)
    return (
        isinstance(function, dict)
        and isinstance(function.get("name"), str)
        and "arguments" in function
    )


def _terminal_reply(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise CompletionContractError("proxy_response_not_object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise CompletionContractError("proxy_choice_missing")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise CompletionContractError("proxy_choice_not_object")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise CompletionContractError("proxy_message_missing")
    if message.get("tool_calls"):
        raise CompletionContractError("proxy_structured_tool_intent_unfinished")
    if choice.get("finish_reason") != "stop":
        raise CompletionContractError("proxy_finish_reason_not_terminal")
    reply = message.get("content")
    if not isinstance(reply, str) or not reply.strip():
        raise CompletionContractError("proxy_terminal_answer_missing")
    if _contains_textual_tool_intent(reply):
        raise CompletionContractError("proxy_textual_tool_intent_unfinished")
    return reply


def _default_event_log() -> Path:
    session_root = Path(
        os.environ.get("TAEY_SESSIONS_DIR", str(Path.home() / "taey_sessions"))
    ).expanduser()
    return session_root / f"{CONVERSATION_ID}.jsonl"


EVENT_LOG = Path(
    os.environ.get(
        "TAEY_EXECUTIVE_EVENT_LOG",
        os.environ.get("TAEY_SEAT_EVENT_LOG", str(_default_event_log())),
    )
).expanduser()


def _process_log_path() -> Path:
    return Path(
        os.environ.get(
            "TAEY_SEAT_PROCESS_LOG",
            str(
                Path(
                    os.environ.get(
                        "TAEY_SESSIONS_DIR",
                        str(Path.home() / "taey_sessions"),
                    )
                ).expanduser()
                / f"{SESSION}.process.log"
            ),
        )
    ).expanduser()


def _record_process_event(message: str) -> None:
    """Write seat-process evidence outside tmux so pane death cannot erase it."""
    print(message, file=sys.stderr, flush=True)
    path = _process_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        notice = (
            f"[taey-seat] PROCESS LOG WRITE FAILED: {type(exc).__name__}: {exc}"
        )
        print(notice, file=sys.stderr, flush=True)
        print(
            f"[taey-seat] process log write failed; seat remains: {exc}",
            flush=True,
        )


class SeatFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class QueueSpec:
    name: str
    queue_key: str
    processing_key: str
    source_side: str
    processing_side: str
    requeue_side: str


@dataclass(frozen=True)
class ClaimedMessage:
    source: QueueSpec
    raw: str
    payload: dict[str, Any]
    message_id: str


@dataclass(frozen=True)
class ProxyResult:
    reply: str
    turn_id: str
    event_id: str
    correlation_id: str
    payload: dict[str, Any]


QUEUES = (
    QueueSpec(
        name="inbox",
        queue_key=f"{KEY_PREFIX}:{SESSION}:inbox",
        processing_key=f"{KEY_PREFIX}:{SESSION}:processing:inbox",
        source_side="RIGHT",
        processing_side="LEFT",
        requeue_side="RIGHT",
    ),
    QueueSpec(
        name="notifications",
        queue_key=f"{KEY_PREFIX}:{SESSION}:notifications",
        processing_key=f"{KEY_PREFIX}:{SESSION}:processing:notifications",
        source_side="LEFT",
        processing_side="RIGHT",
        requeue_side="LEFT",
    ),
    QueueSpec(
        name="orch",
        queue_key=f"{KEY_PREFIX}:notify:{SESSION}:orch",
        processing_key=f"{KEY_PREFIX}:{SESSION}:processing:orch",
        source_side="LEFT",
        processing_side="RIGHT",
        requeue_side="LEFT",
    ),
)
POINTER_BACKOFF_KEY = f"{KEY_PREFIX}:{SESSION}:pointer_inject_backoff"
NON_ACTIONABLE_MESSAGE_TYPES = frozenset({"peer_idle"})
CONSULT_TERMINAL_RECEIPT_SCHEMA = "taey.consult_terminal_receipt.v1"
CONSULT_TERMINAL_RECEIPT_SENDER = "consult-monitor"
CONSULT_TERMINAL_RECEIPT_TYPE = "result"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_HANDOFF_RECEIPT_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then
    return redis.error_reply('explicit handoff record is missing')
end
local ok, record = pcall(cjson.decode, raw)
if not ok or type(record) ~= 'table' then
    return redis.error_reply('explicit handoff record is invalid')
end
if record['kind'] ~= 'explicit_handoff'
   or tostring(record['target_session_id'] or '') ~= ARGV[1]
   or tostring(record['message_hash'] or '') ~= ARGV[2] then
    return redis.error_reply('explicit handoff record does not match claimed message')
end
redis.call('SET', KEYS[2], ARGV[3])
local state = tostring(record['state'] or '')
if state ~= 'resolved' and state ~= 'superseded' and state ~= 'dead'
   and state ~= 'receipt_acked' then
    record['state'] = 'receipt_acked'
    record['receipt_source'] = 'taey-seat-claim'
    record['receipt_acked_at'] = tonumber(ARGV[4])
    redis.call('SET', KEYS[1], cjson.encode(record))
end
return 1
"""

_REQUEUE_LUA = """
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed == 1 then
    if ARGV[2] == 'LEFT' then
        redis.call('LPUSH', KEYS[2], ARGV[1])
    else
        redis.call('RPUSH', KEYS[2], ARGV[1])
    end
end
return removed
"""

_GUARDED_CLAIM_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return redis.error_reply('seat process generation is not current')
end
if ARGV[2] == '1' and redis.call('EXISTS', KEYS[2]) == 1 then
    return nil
end
return redis.call('LMOVE', KEYS[3], KEYS[4], ARGV[3], ARGV[4])
"""

_QUARANTINE_LUA = """
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed == 1 then
    redis.call('LPUSH', KEYS[2], ARGV[1])
end
return removed
"""


def _require_private_event_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise SeatFailure(f"event log directory cannot be a symlink: {path}")
    if path.stat().st_mode & 0o077:
        raise SeatFailure(
            f"event log directory is group/world accessible: {path}"
        )


def _open_private_event_log(path: Path, flags: int) -> int:
    try:
        descriptor = os.open(
            path,
            flags | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise SeatFailure(f"event log cannot be opened securely: {path}") from exc
    if os.fstat(descriptor).st_mode & 0o077:
        os.close(descriptor)
        raise SeatFailure(f"event log is group/world accessible: {path}")
    return descriptor


class EventStore:
    def __init__(self, path: Path, max_turns: int):
        self.path = path
        self.max_turns = max_turns
        self.completed_message_ids: set[str] = set()
        self._load()

    def _read_events(self) -> list[dict[str, Any]]:
        _require_private_event_directory(self.path.parent)
        if self.path.is_symlink():
            raise SeatFailure(f"event log cannot be a symlink: {self.path}")
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        descriptor = _open_private_event_log(self.path, os.O_RDONLY)
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            for line_number, line in enumerate(handle, 1):
                if not line.endswith("\n"):
                    raise SeatFailure(
                        f"event log has a partial record at {self.path}:{line_number}"
                    )
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SeatFailure(
                        f"event log is invalid at {self.path}:{line_number}: {exc}"
                    ) from exc
                if not isinstance(event, dict):
                    raise SeatFailure(
                        f"event log record is not an object at {self.path}:{line_number}"
                    )
                events.append(event)
        return events

    def _load(self) -> None:
        for event in self._read_events():
            if event.get("event_type") == "turn_outcome" and event.get("ok"):
                for message_id in event.get("message_ids") or []:
                    self.completed_message_ids.add(str(message_id))

    def append(self, event_type: str, **fields: Any) -> None:
        event = {
            "schema_version": 1,
            "event_type": event_type,
            "recorded_at": time.time(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session": SESSION,
            "conversation_id": CONVERSATION_ID,
            **fields,
        }
        encoded = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        _require_private_event_directory(self.path.parent)
        new_log = not self.path.exists()
        descriptor = _open_private_event_log(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise SeatFailure(f"event log write made no progress: {self.path}")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        if new_log:
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def messages_for(
        self,
        prompt: str,
        *,
        include_history: bool = True,
    ) -> list[dict[str, str]]:
        if not include_history:
            return [{"role": "user", "content": prompt}]
        messages: list[dict[str, str]] = []
        recorded_prompt = False
        seen_ingress: set[str] = set()
        for event in self._read_events():
            role = event.get("role")
            content = event.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content:
                messages.append({"role": role, "content": content})
                continue
            if event.get("event_type") == "executive_ingress":
                event_id = str(event.get("event_id") or "")
                if event_id and event_id in seen_ingress:
                    continue
                if event_id:
                    seen_ingress.add(event_id)
                context_role = event.get("context_role")
                context_content = event.get("context_content")
                if (
                    event.get("conversation_visible") is not False
                    and context_role in {"user", "assistant"}
                    and isinstance(context_content, str)
                    and context_content
                ):
                    messages.append(
                        {"role": context_role, "content": context_content}
                    )
                    if context_role == "user" and context_content == prompt:
                        recorded_prompt = True
                continue
            if (
                event.get("event_type") == "turn_outcome"
                and event.get("ok")
                and event.get("conversation_visible") is not False
            ):
                prior_prompt = event.get("prompt")
                prior_reply = event.get("reply")
                if isinstance(prior_prompt, str) and isinstance(prior_reply, str):
                    messages.append({"role": "user", "content": prior_prompt})
                    messages.append({"role": "assistant", "content": prior_reply})
        if not recorded_prompt:
            messages.append({"role": "user", "content": prompt})
        return messages[-(self.max_turns * 2):]

    def remember_outcome(self, prompt: str, reply: str, message_ids: list[str]) -> None:
        self.completed_message_ids.update(message_ids)


def _decode_message(raw: str) -> tuple[dict[str, Any], str]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        value = {"type": "unparseable", "body": raw, "raw": raw}
    if not isinstance(value, dict):
        value = {"type": "unparseable", "body": raw, "raw": raw}
    message_id = str(
        value.get("msg_id")
        or value.get("message_id")
        or value.get("id")
        or f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"
    )
    return value, message_id


class ReliableInbox:
    def __init__(
        self,
        client: redis.Redis,
        store: EventStore,
        *,
        processing_generation: str | None = None,
        claim_guard: tuple[str, str] | None = None,
        claim_block_key: str | None = None,
    ):
        self.client = client
        self.store = store
        if processing_generation is not None:
            if not _TRACE_ID_RE.fullmatch(processing_generation):
                raise SeatFailure(
                    f"invalid processing generation: {processing_generation!r}"
                )
            self.queues = tuple(
                QueueSpec(
                    name=source.name,
                    queue_key=source.queue_key,
                    processing_key=(
                        f"{source.processing_key}:{processing_generation}"
                    ),
                    source_side=source.source_side,
                    processing_side=source.processing_side,
                    requeue_side=source.requeue_side,
                )
                for source in QUEUES
            )
        else:
            self.queues = QUEUES
        if claim_block_key is not None and claim_guard is None:
            raise SeatFailure("claim_block_key requires claim_guard")
        self.claim_guard = claim_guard
        self.claim_block_key = claim_block_key

    def _claim_raw(self, source: QueueSpec) -> str | None:
        if self.claim_guard is None:
            return self.client.lmove(
                source.queue_key,
                source.processing_key,
                source.source_side,
                source.processing_side,
            )
        guard_key, guard_value = self.claim_guard
        return self.client.eval(
            _GUARDED_CLAIM_LUA,
            4,
            guard_key,
            self.claim_block_key or guard_key,
            source.queue_key,
            source.processing_key,
            guard_value,
            "1" if self.claim_block_key else "0",
            source.source_side,
            source.processing_side,
        )

    def _ack(self, claim: ClaimedMessage) -> None:
        removed = int(
            self.client.lrem(claim.source.processing_key, 1, claim.raw)
        )
        if removed != 1:
            raise SeatFailure(
                f"ack lost claim source={claim.source.name} msg_id={claim.message_id}"
            )

    def _requeue(self, claim: ClaimedMessage) -> None:
        removed = int(
            self.client.eval(
                _REQUEUE_LUA,
                2,
                claim.source.processing_key,
                claim.source.queue_key,
                claim.raw,
                claim.source.requeue_side,
            )
        )
        if removed != 1:
            raise SeatFailure(
                f"requeue lost claim source={claim.source.name} "
                f"msg_id={claim.message_id}"
            )

    def _record_handoff_receipt(self, claim: ClaimedMessage) -> None:
        payload = claim.payload
        if payload.get("handoff_kind") != "explicit_handoff":
            return
        dispatcher = str(payload.get("dispatcher_session_id") or "")
        target = str(payload.get("target_session_id") or "")
        message_hash = str(payload.get("message_hash") or "")
        if not dispatcher or target != SESSION or not message_hash:
            raise SeatFailure(
                f"invalid explicit handoff envelope msg_id={claim.message_id}"
            )
        body_hash = hashlib.sha256(
            str(payload.get("body") or "").encode("utf-8")
        ).hexdigest()
        if body_hash != message_hash:
            raise SeatFailure(
                f"explicit handoff body hash mismatch msg_id={claim.message_id}"
            )
        receipt = json.dumps(
            {"ack_by": SESSION, "message_hash": message_hash},
            separators=(",", ":"),
        )
        self.client.eval(
            _HANDOFF_RECEIPT_LUA,
            2,
            f"{KEY_PREFIX}:handoff:{dispatcher}:{claim.message_id}",
            (
                f"{KEY_PREFIX}:handoff-ack:{dispatcher}:"
                f"{SESSION}:{claim.message_id}"
            ),
            SESSION,
            message_hash,
            receipt,
            time.time(),
        )

    def claim_available(self) -> list[ClaimedMessage]:
        claims: list[ClaimedMessage] = []
        try:
            for source in self.queues:
                while not claims:
                    raw = self._claim_raw(source)
                    if raw is None:
                        break
                    payload, message_id = _decode_message(raw)
                    claim = ClaimedMessage(source, raw, payload, message_id)
                    if message_id in self.store.completed_message_ids:
                        self._ack(claim)
                        continue
                    claims.append(claim)
                if claims:
                    break
            if claims:
                self.store.append(
                    "delivery_claim",
                    message_ids=[claim.message_id for claim in claims],
                    sources=[claim.source.name for claim in claims],
                )
                for claim in claims:
                    self._record_handoff_receipt(claim)
            return claims
        except Exception as exc:
            try:
                self.requeue(claims)
            except Exception as requeue_exc:
                raise SeatFailure(
                    f"claim failed ({exc}); recovery also failed ({requeue_exc})"
                ) from requeue_exc
            raise SeatFailure(f"claim failed: {type(exc).__name__}: {exc}") from exc

    def acknowledge(self, claims: list[ClaimedMessage]) -> None:
        for claim in claims:
            self._ack(claim)
        if claims:
            self.client.delete(POINTER_BACKOFF_KEY)

    def release_pointer(self) -> None:
        self.client.delete(POINTER_BACKOFF_KEY)

    def requeue(self, claims: list[ClaimedMessage]) -> None:
        by_source: dict[str, list[ClaimedMessage]] = {}
        for claim in claims:
            by_source.setdefault(claim.source.name, []).append(claim)
        for source in self.queues:
            source_claims = by_source.get(source.name, [])
            for claim in reversed(source_claims):
                self._requeue(claim)
        if claims:
            self.client.delete(POINTER_BACKOFF_KEY)

    def recover(self) -> dict[str, int]:
        recovered = 0
        acknowledged = 0
        for source in self.queues:
            raws = list(self.client.lrange(source.processing_key, 0, -1))
            # LMOVE stores inbox claims newest-first (LEFT) but the other two
            # sources oldest-first (RIGHT); this produces the inverse push order
            # each source needs to restore its original consumer-visible FIFO.
            if source.processing_side == "RIGHT":
                raws.reverse()
            for raw in raws:
                payload, message_id = _decode_message(raw)
                claim = ClaimedMessage(source, raw, payload, message_id)
                if message_id in self.store.completed_message_ids:
                    self._ack(claim)
                    acknowledged += 1
                else:
                    self._requeue(claim)
                    recovered += 1
        self.client.delete(POINTER_BACKOFF_KEY)
        return {"requeued": recovered, "acknowledged": acknowledged}


class ExecutiveInbox(ReliableInbox):
    @staticmethod
    def _quarantine_key(source: QueueSpec) -> str:
        return f"{KEY_PREFIX}:{SESSION}:quarantine:{source.name}"

    def _quarantine(self, claim: ClaimedMessage) -> None:
        moved = int(
            self.client.eval(
                _QUARANTINE_LUA,
                2,
                claim.source.processing_key,
                self._quarantine_key(claim.source),
                claim.raw,
            )
        )
        if moved != 1:
            raise SeatFailure(
                f"quarantine lost claim source={claim.source.name} "
                f"msg_id={claim.message_id}"
            )

    def quarantine(self, claims: list[ClaimedMessage]) -> None:
        for claim in claims:
            self._quarantine(claim)
        if claims:
            self.client.delete(POINTER_BACKOFF_KEY)

    def recover(self) -> dict[str, int]:
        quarantined = 0
        acknowledged = 0
        for source in QUEUES:
            for raw in self.client.lrange(source.processing_key, 0, -1):
                payload, message_id = _decode_message(raw)
                claim = ClaimedMessage(source, raw, payload, message_id)
                if message_id in self.store.completed_message_ids:
                    self._ack(claim)
                    acknowledged += 1
                else:
                    self._quarantine(claim)
                    quarantined += 1
                    self.store.append(
                        "claim_quarantine",
                        message_ids=[message_id],
                        sources=[source.name],
                        reason="seat_recovered_nonterminal_claim",
                        quarantine_key=self._quarantine_key(source),
                    )
        self.client.delete(POINTER_BACKOFF_KEY)
        return {"quarantined": quarantined, "acknowledged": acknowledged}


class ProxyClient:
    @staticmethod
    def model_request_body(
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
        *,
        max_rounds: int | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        request_body: dict[str, Any] = {
            "model": MODEL,
            "messages": messages,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if response_format is not None:
            request_body["response_format"] = response_format
        if max_rounds is not None:
            request_body["max_rounds"] = max_rounds
        if max_tokens is not None:
            request_body["max_tokens"] = max_tokens
        return request_body

    def ask(
        self,
        prompt: str,
        *,
        event_id: str,
        correlation_id: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
        max_rounds: int | None = None,
        max_tokens: int | None = None,
        tool_profile: str | None = None,
        outbound_request_bytes: bytes | None = None,
    ) -> ProxyResult:
        request_body = self.model_request_body(
            messages,
            response_format,
            max_rounds=max_rounds,
            max_tokens=max_tokens,
        )
        if outbound_request_bytes is None:
            body = json.dumps(request_body).encode("utf-8")
        else:
            try:
                body = bind_outbound_request_bytes(
                    request_body,
                    outbound_request_bytes,
                )
            except (TypeError, ValueError) as exc:
                raise SeatFailure(
                    f"outbound request bytes drifted from the encoded model request "
                    f"for correlation={correlation_id}: {exc}"
                ) from exc
        headers = {
            "Content-Type": "application/json",
            "X-Taey-Seat-Id": SESSION,
            "X-Taey-Event-Id": event_id,
            "X-Taey-Correlation-Id": correlation_id,
        }
        if tool_profile is not None:
            headers["X-Taey-Tool-Profile"] = tool_profile
        request = urllib.request.Request(
            PROXY_URL,
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                response_headers = response.headers
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:4000]
            raise SeatFailure(
                f"proxy HTTP {exc.code} for correlation={correlation_id}: {detail}"
            ) from exc
        except Exception as exc:
            raise SeatFailure(
                f"proxy request failed for correlation={correlation_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SeatFailure(
                f"proxy returned invalid JSON for correlation={correlation_id}: {exc}"
            ) from exc
        reply = (
            ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(reply, str) or not reply.strip():
            raise SeatFailure(
                f"proxy returned no assistant content for correlation={correlation_id}"
            )
        returned_turn_id = str(response_headers.get("X-Taey-Turn-Id") or "")
        returned_event_id = str(response_headers.get("X-Taey-Event-Id") or "")
        returned_correlation_id = str(
            response_headers.get("X-Taey-Correlation-Id") or ""
        )
        returned_tool_profile = str(
            response_headers.get("X-Taey-Tool-Profile") or ""
        )
        if not returned_turn_id:
            raise SeatFailure(
                f"proxy omitted X-Taey-Turn-Id for correlation={correlation_id}"
            )
        if returned_event_id != event_id or returned_correlation_id != correlation_id:
            raise SeatFailure(
                "proxy lineage mismatch "
                f"expected=({event_id},{correlation_id}) "
                f"returned=({returned_event_id},{returned_correlation_id})"
            )
        if tool_profile is not None and returned_tool_profile != tool_profile:
            raise SeatFailure(
                "proxy tool-profile mismatch "
                f"expected={tool_profile!r} returned={returned_tool_profile!r}"
            )
        return ProxyResult(
            reply=reply,
            turn_id=returned_turn_id,
            event_id=returned_event_id,
            correlation_id=returned_correlation_id,
            payload=payload,
        )


def _safe_trace_id(value: Any, fallback: str) -> str:
    candidate = str(value or fallback).strip()
    if _TRACE_ID_RE.fullmatch(candidate):
        return candidate
    return f"sha256:{hashlib.sha256(candidate.encode('utf-8')).hexdigest()}"


def _lineage(claims: list[ClaimedMessage]) -> tuple[str, str]:
    if not claims:
        event_id = uuid.uuid4().hex
        return event_id, event_id
    if len(claims) == 1:
        payload = claims[0].payload
        event_id = _safe_trace_id(
            payload.get("event_id") or claims[0].message_id,
            claims[0].message_id,
        )
        correlation_id = _safe_trace_id(
            payload.get("correlation_id") or payload.get("trace_id") or event_id,
            event_id,
        )
        return event_id, correlation_id
    identity = "\x00".join(claim.message_id for claim in claims)
    event_id = f"batch-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
    correlations = {
        str(
            claim.payload.get("correlation_id")
            or claim.payload.get("trace_id")
            or ""
        )
        for claim in claims
    }
    correlations.discard("")
    correlation_id = (
        _safe_trace_id(next(iter(correlations)), event_id)
        if len(correlations) == 1
        else event_id
    )
    return event_id, correlation_id


def _format_claims(claims: list[ClaimedMessage]) -> str:
    sections: list[str] = []
    for claim in claims:
        payload = claim.payload
        metadata = {
            "source": claim.source.name,
            "msg_id": claim.message_id,
            "from": payload.get("from", "unknown"),
            "type": payload.get("type", "message"),
            "priority": payload.get("priority", "normal"),
            "timestamp": payload.get("timestamp"),
        }
        body = payload.get("body", claim.raw)
        sections.append(
            "[FLEET MESSAGE "
            + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
            + "]\n"
            + str(body)
            + "\n[/FLEET MESSAGE]"
        )
    return "\n\n".join(sections)


def _message_type(claim: ClaimedMessage) -> str:
    return str(claim.payload.get("type") or "message").strip().lower()


def _split_actionable_claims(
    claims: list[ClaimedMessage],
) -> tuple[list[ClaimedMessage], list[ClaimedMessage]]:
    actionable: list[ClaimedMessage] = []
    skipped: list[ClaimedMessage] = []
    for claim in claims:
        if _message_type(claim) in NON_ACTIONABLE_MESSAGE_TYPES:
            skipped.append(claim)
        else:
            actionable.append(claim)
    return actionable, skipped


def _consult_terminal_receipt(claim: ClaimedMessage) -> dict[str, Any] | None:
    if (
        claim.payload.get("type") != CONSULT_TERMINAL_RECEIPT_TYPE
        or claim.payload.get("from") != CONSULT_TERMINAL_RECEIPT_SENDER
    ):
        return None
    body = claim.payload.get("body")
    if not isinstance(body, str):
        return None
    try:
        receipt = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(receipt, dict):
        return None
    if receipt.get("schema") != CONSULT_TERMINAL_RECEIPT_SCHEMA:
        return None

    required_strings = ("monitor_id", "platform", "display", "extraction_status")
    if any(
        not isinstance(receipt.get(field), str) or not receipt[field].strip()
        for field in required_strings
    ):
        return None
    if not re.fullmatch(r":\d+", receipt["display"]):
        return None
    if receipt.get("terminal") is not True:
        return None

    extraction_status = receipt["extraction_status"]
    if extraction_status == "succeeded":
        success_strings = (
            "response_file",
            "request_json",
            "headers",
            "response_json",
            "event",
            "correlation",
        )
        if any(
            not isinstance(receipt.get(field), str) or not receipt[field].strip()
            for field in success_strings
        ):
            return None
        if (
            isinstance(receipt.get("bytes"), bool)
            or not isinstance(receipt.get("bytes"), int)
            or receipt["bytes"] <= 0
        ):
            return None
        if (
            not isinstance(receipt.get("sha"), str)
            or not _SHA256_RE.fullmatch(receipt["sha"])
        ):
            return None
    elif extraction_status == "failed":
        if not isinstance(receipt.get("error"), str) or not receipt["error"].strip():
            return None
    else:
        return None
    return receipt


def _split_record_only_receipts(
    claims: list[ClaimedMessage],
) -> tuple[
    list[ClaimedMessage],
    list[tuple[ClaimedMessage, dict[str, Any]]],
]:
    actionable: list[ClaimedMessage] = []
    receipts: list[tuple[ClaimedMessage, dict[str, Any]]] = []
    for claim in claims:
        receipt = _consult_terminal_receipt(claim)
        if receipt is None:
            actionable.append(claim)
        else:
            receipts.append((claim, receipt))
    return actionable, receipts


def _format_non_actionable_reply(claims: list[ClaimedMessage]) -> str:
    message_types = sorted({_message_type(claim) for claim in claims})
    return (
        f"[taey-seat] acknowledged {len(claims)} non-actionable fleet "
        f"message(s): {', '.join(message_types)}"
    )


def _ack_non_actionable_claims(
    claims: list[ClaimedMessage],
    *,
    inbox: ReliableInbox,
    store: EventStore,
) -> str:
    prompt = _format_claims(claims)
    reply = _format_non_actionable_reply(claims)
    event_id, correlation_id = _lineage(claims)
    message_ids = [claim.message_id for claim in claims]
    fields = {
        "event_id": event_id,
        "correlation_id": correlation_id,
        "message_ids": message_ids,
        "prompt": prompt,
        "skipped_inference": True,
    }
    store.append("turn_attempt", **fields)
    store.append(
        "turn_outcome",
        ok=True,
        reply=reply,
        conversation_visible=False,
        **fields,
    )
    inbox.acknowledge(claims)
    store.completed_message_ids.update(message_ids)
    return reply


def _record_consult_terminal_receipts(
    receipt_claims: list[tuple[ClaimedMessage, dict[str, Any]]],
    *,
    inbox: ReliableInbox,
    store: EventStore,
) -> str:
    claims = [claim for claim, _receipt in receipt_claims]
    receipts = [receipt for _claim, receipt in receipt_claims]
    prompt = _format_claims(claims)
    event_id, correlation_id = _lineage(claims)
    message_ids = [claim.message_id for claim in claims]
    summary = ", ".join(
        f"{receipt['platform']}{receipt['display']}:{receipt['extraction_status']}"
        for receipt in receipts
    )
    reply = (
        f"[taey-seat] recorded {len(receipts)} consultation terminal "
        f"receipt(s): {summary}"
    )
    fields = {
        "event_id": event_id,
        "correlation_id": correlation_id,
        "message_ids": message_ids,
        "prompt": prompt,
        "skipped_inference": True,
        "record_only_receipts": receipts,
    }
    store.append("turn_attempt", **fields)
    store.append(
        "turn_outcome",
        ok=True,
        reply=reply,
        kind="consult_terminal_receipt",
        conversation_visible=False,
        **fields,
    )
    inbox.acknowledge(claims)
    store.completed_message_ids.update(message_ids)
    return reply


def _prompt_for(text: str, claims: list[ClaimedMessage]) -> str:
    if not claims:
        return text
    prompt = _format_claims(claims)
    if text and not _POINTER_RE.match(text):
        prompt += f"\n\n[TMUX OPERATOR INPUT]\n{text}\n[/TMUX OPERATOR INPUT]"
    return prompt


def _redis_client() -> redis.Redis:
    client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=8,
    )
    client.ping()
    return client


def _run_turn(
    text: str,
    *,
    inbox: ReliableInbox,
    store: EventStore,
    proxy: ProxyClient,
) -> str:
    claims = inbox.claim_available()
    claims, skipped_claims = _split_actionable_claims(claims)
    claims, receipt_claims = _split_record_only_receipts(claims)
    acknowledgement_replies: list[str] = []
    if skipped_claims:
        acknowledgement_replies.append(
            _ack_non_actionable_claims(
                skipped_claims,
                inbox=inbox,
                store=store,
            )
        )
    if receipt_claims:
        acknowledgement_replies.append(
            _record_consult_terminal_receipts(
                receipt_claims,
                inbox=inbox,
                store=store,
            )
        )
    if not claims and _POINTER_RE.match(text):
        if acknowledgement_replies:
            return "\n".join(acknowledgement_replies)
        inbox.release_pointer()
        return "[taey-seat] notification pointer contained no pending messages"
    prompt = _prompt_for(text, claims)
    event_id, correlation_id = _lineage(claims)
    message_ids = [claim.message_id for claim in claims]
    store.append(
        "executive_ingress",
        event_id=event_id,
        correlation_id=correlation_id,
        message_ids=message_ids,
        source="fleet" if claims else "tmux",
        source_id=message_ids[0] if len(message_ids) == 1 else event_id,
        kind="fleet_message" if claims else "operator_command",
        context_role="user",
        context_content=prompt,
        conversation_visible=True,
    )
    store.append(
        "turn_attempt",
        attempt_id=uuid.uuid4().hex,
        event_id=event_id,
        correlation_id=correlation_id,
        message_ids=message_ids,
        prompt=prompt,
    )
    try:
        # Claimed fleet packets are self-contained; unrelated prior turns violate their context bound.
        result = proxy.ask(
            prompt,
            event_id=event_id,
            correlation_id=correlation_id,
            messages=store.messages_for(prompt, include_history=not claims),
        )
        reply = _terminal_reply(result.payload)
        store.append(
            "turn_outcome",
            ok=True,
            event_id=event_id,
            correlation_id=correlation_id,
            proxy_turn_id=result.turn_id,
            message_ids=message_ids,
            prompt=prompt,
            reply=reply,
            role="assistant",
            content=reply,
            source="taey",
            source_id=result.turn_id,
            kind="assistant_raise" if claims else "assistant_reply",
            conversation_visible=True,
        )
    except Exception as exc:
        try:
            inbox.quarantine(claims)
            store.append(
                "turn_outcome",
                ok=False,
                event_id=event_id,
                correlation_id=correlation_id,
                message_ids=message_ids,
                prompt=prompt,
                error=f"{type(exc).__name__}: {exc}",
                claim_state="quarantined" if claims else "not_applicable",
                continuation="reconciliation_required",
            )
        except Exception as recovery_exc:
            raise SeatFailure(
                f"turn failed ({exc}); durable recovery failed ({recovery_exc})"
            ) from recovery_exc
        raise
    store.remember_outcome(prompt, reply, message_ids)
    inbox.acknowledge(claims)
    return reply


def main() -> int:
    if not _SEAT_ID_RE.fullmatch(SESSION):
        print(
            "[taey-seat] FATAL: TAEY_SESSION_NAME must match "
            "[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    try:
        store = EventStore(EVENT_LOG, MAX_TURNS)
        client = _redis_client()
        inbox = ExecutiveInbox(client, store)
        recovery = inbox.recover()
    except Exception as exc:
        _record_process_event(
            f"[taey-seat] FATAL startup: {type(exc).__name__}: {exc}"
        )
        return 1
    print(
        f"[taey-seat] session={SESSION} proxy={PROXY_URL} "
        f"event_log={EVENT_LOG} recovered={recovery}",
        flush=True,
    )
    proxy = ProxyClient()
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            return 0
        try:
            reply = _run_turn(text, inbox=inbox, store=store, proxy=proxy)
        except Exception as exc:
            _record_process_event(
                f"[taey-seat] TURN ERROR: {type(exc).__name__}: {exc}"
            )
            print(
                f"[taey-seat] turn failed; seat remains: {type(exc).__name__}: {exc}",
                flush=True,
            )
            continue
        print(reply, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
