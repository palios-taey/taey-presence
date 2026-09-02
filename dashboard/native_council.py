"""Durable Taey-native council transport for the local seven-seat runtime."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import redis

from serving import council_prompt_receipt as prompt_producer
from serving import model_identity_status


log = logging.getLogger(__name__)


class CouncilTransportFailure(RuntimeError):
    pass


class CouncilRevisionSuperseded(CouncilTransportFailure):
    def __init__(self, expected_revision: int, latest_revision: int):
        self.expected_revision = expected_revision
        self.latest_revision = latest_revision
        super().__init__(
            f"prompt revision {expected_revision} was superseded by "
            f"revision {latest_revision}"
        )


@dataclass(frozen=True)
class CouncilSeat:
    seat_id: str
    role_id: str


COUNCIL_SEATS = (
    CouncilSeat("taey-council-1", "context-memory"),
    CouncilSeat("taey-council-2", "evidence-reality"),
    CouncilSeat("taey-council-3", "systems-dependencies"),
    CouncilSeat("taey-council-4", "adversarial-failure"),
    CouncilSeat("taey-council-5", "scope-intent"),
    CouncilSeat("taey-council-6", "options-alternatives"),
    CouncilSeat("taey-council-7", "control-acceptance"),
)
_COUNCIL_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "serving" / "council_seats.json"
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TERMINAL_EVENTS = frozenset({"round_completed", "round_failed"})
_RESERVED_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_type",
        "sequence",
        "recorded_at",
        "ts",
        "conversation_id",
        "round_id",
    }
)
_ENQUEUE_LUA = """
local seat_count = tonumber(ARGV[1])
local dispatch_type = redis.call('TYPE', KEYS[1])['ok']
if dispatch_type ~= 'none' and dispatch_type ~= 'set' then
    return redis.error_reply('council dispatch identity key is not a set')
end
if redis.call('EXISTS', KEYS[2]) == 1 then
    return redis.error_reply('council seat replacement is in progress')
end
for index = 1, seat_count do
    local registration_key = KEYS[2 + index]
    local expected_registration = ARGV[1 + index]
    if redis.call('GET', registration_key) ~= expected_registration
       or redis.call('TTL', registration_key) <= 0 then
        return redis.error_reply(
            'seat registration changed before atomic wave enqueue index=' .. index
        )
    end
    local inbox_key = KEYS[2 + seat_count + index]
    local inbox_type = redis.call('TYPE', inbox_key)['ok']
    if inbox_type ~= 'none' and inbox_type ~= 'list' then
        return redis.error_reply(
            'council inbox key is not a list index=' .. index
        )
    end
end
local results = {}
for index = 1, seat_count do
    local argument_index = 2 + seat_count + ((index - 1) * 2)
    local token = ARGV[argument_index]
    local encoded = ARGV[argument_index + 1]
    local inbox_key = KEYS[2 + seat_count + index]
    local enqueued = redis.call('SADD', KEYS[1], token)
    if enqueued == 1 then
        redis.call('LPUSH', inbox_key, encoded)
    end
    results[index] = enqueued
end
return results
"""
_CLEAR_ACTIVE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_READ_LIVE_REGISTRATION_LUA = """
local value = redis.call('GET', KEYS[1])
if not value then
    return {}
end
return {value, redis.call('TTL', KEYS[1])}
"""
_READ_DISPATCH_STATE_LUA = """
local dispatch_type = redis.call('TYPE', KEYS[1])['ok']
if dispatch_type ~= 'none' and dispatch_type ~= 'set' then
    return redis.error_reply('council dispatch identity key is not a set')
end
local results = {}
for index = 1, #ARGV do
    results[index] = redis.call('SISMEMBER', KEYS[1], ARGV[index])
end
return results
"""

SynthesizeCallback = Callable[
    [str, dict[str, Any]],
    Awaitable[dict[str, Any]],
]
TerminalCallback = Callable[
    [str, str, dict[str, Any], dict[str, Any]],
    Awaitable[None],
]


def _safe_id(value: Any) -> str:
    candidate = str(value or "").strip()
    if _ID_RE.fullmatch(candidate):
        return candidate
    return f"sha256:{hashlib.sha256(candidate.encode('utf-8')).hexdigest()}"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        for line_number, line in enumerate(handle, 1):
            if not line.endswith("\n"):
                raise CouncilTransportFailure(
                    f"partial JSONL record at {path}:{line_number}"
                )
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CouncilTransportFailure(
                    f"invalid JSONL record at {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(event, dict):
                raise CouncilTransportFailure(
                    f"non-object JSONL record at {path}:{line_number}"
                )
            events.append(event)
    return events


class RoundLedger:
    def __init__(self, sessions_dir: Path, conversation_id: str, round_id: str):
        if not _SESSION_RE.fullmatch(conversation_id):
            raise CouncilTransportFailure(
                f"invalid council conversation id: {conversation_id!r}"
            )
        if not _ID_RE.fullmatch(round_id):
            raise CouncilTransportFailure(f"invalid council round id: {round_id!r}")
        self.sessions_dir = sessions_dir.expanduser()
        self.conversation_id = conversation_id
        self.round_id = round_id
        self.path = (
            self.sessions_dir
            / "dcm"
            / conversation_id
            / f"{round_id}.jsonl"
        )

    def _prepare_directory(self) -> None:
        self.sessions_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.sessions_dir.is_symlink():
            raise CouncilTransportFailure(
                f"session directory cannot be a symlink: {self.sessions_dir}"
            )
        dcm_root = self.sessions_dir / "dcm"
        conversation_root = dcm_root / self.conversation_id
        for directory in (dcm_root, conversation_root):
            directory.mkdir(mode=0o700, exist_ok=True)
            if directory.is_symlink():
                raise CouncilTransportFailure(
                    f"council ledger directory cannot be a symlink: {directory}"
                )
            if directory.stat().st_mode & 0o077:
                raise CouncilTransportFailure(
                    f"council ledger directory is group/world accessible: "
                    f"{directory}"
                )

    def _validate_events(self, events: list[dict[str, Any]]) -> None:
        opened_count = 0
        terminal_sequence: int | None = None
        terminal_projected = False
        for expected_sequence, event in enumerate(events, 1):
            if (
                type(event.get("schema_version")) is not int
                or event["schema_version"] != 1
            ):
                raise CouncilTransportFailure(
                    f"invalid council ledger schema at sequence "
                    f"{expected_sequence}: {self.path}"
                )
            if (
                type(event.get("sequence")) is not int
                or event["sequence"] != expected_sequence
            ):
                raise CouncilTransportFailure(
                    f"non-contiguous council ledger sequence at "
                    f"{self.path}: expected {expected_sequence}, "
                    f"got {event.get('sequence')!r}"
                )
            event_type = event.get("event_type")
            if not isinstance(event_type, str) or not _EVENT_TYPE_RE.fullmatch(
                event_type
            ):
                raise CouncilTransportFailure(
                    f"invalid council event type at "
                    f"{self.path}:{expected_sequence}"
                )
            if (
                event.get("conversation_id") != self.conversation_id
                or event.get("round_id") != self.round_id
            ):
                raise CouncilTransportFailure(
                    f"council ledger identity mismatch at "
                    f"{self.path}:{expected_sequence}"
                )
            if event_type == "round_opened":
                opened_count += 1
                if expected_sequence != 1:
                    raise CouncilTransportFailure(
                        f"round_opened must be the first council event: "
                        f"{self.path}"
                    )
            if event_type in _TERMINAL_EVENTS:
                if terminal_sequence is not None:
                    raise CouncilTransportFailure(
                        f"council ledger has multiple terminal events: "
                        f"{self.path}"
                    )
                terminal_sequence = expected_sequence
                continue
            if terminal_sequence is not None:
                if terminal_projected:
                    raise CouncilTransportFailure(
                        f"council ledger continues after terminal projection: "
                        f"{self.path}:{expected_sequence}"
                    )
                if event_type not in {
                    "terminal_projection_failed",
                    "terminal_projected",
                }:
                    raise CouncilTransportFailure(
                        f"invalid post-terminal council event "
                        f"{event_type!r}: {self.path}:{expected_sequence}"
                    )
                terminal_projected = event_type == "terminal_projected"
        if events and opened_count != 1:
            raise CouncilTransportFailure(
                f"council ledger requires exactly one round_opened event: "
                f"{self.path}"
            )

    def _fsync_directory_chain(self) -> None:
        for directory_path in (
            self.path.parent,
            self.path.parent.parent,
            self.sessions_dir,
        ):
            descriptor = os.open(
                directory_path,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def acquire_coordinator_lease(self) -> int | None:
        self._prepare_directory()
        lock_path = self.path.with_suffix(".coordinator.lock")
        descriptor = os.open(
            lock_path,
            os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if os.fstat(descriptor).st_mode & 0o077:
                raise CouncilTransportFailure(
                    f"council coordinator lock is group/world accessible: "
                    f"{lock_path}"
                )
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                os.close(descriptor)
                return None
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def release_coordinator_lease(self, descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    def _append_with(
        self,
        event_type: str,
        fields_for: Callable[[list[dict[str, Any]]], dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(event_type, str) or not _EVENT_TYPE_RE.fullmatch(
            event_type
        ):
            raise CouncilTransportFailure(
                f"invalid council event type: {event_type!r}"
            )
        self._prepare_directory()
        new_log = not self.path.exists()
        descriptor = os.open(
            self.path,
            os.O_APPEND
            | os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if os.fstat(descriptor).st_mode & 0o077:
                raise CouncilTransportFailure(
                    f"council ledger is group/world accessible: {self.path}"
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            if raw and not raw.endswith(b"\n"):
                raise CouncilTransportFailure(
                    f"council ledger has a partial record: {self.path}"
                )
            events: list[dict[str, Any]] = []
            for line_number, line in enumerate(raw.splitlines(), 1):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CouncilTransportFailure(
                        f"invalid council ledger record at "
                        f"{self.path}:{line_number}: {exc}"
                    ) from exc
                if not isinstance(event, dict):
                    raise CouncilTransportFailure(
                        f"non-object council ledger record at "
                        f"{self.path}:{line_number}"
                    )
                events.append(event)
            self._validate_events(events)
            if not events and event_type != "round_opened":
                raise CouncilTransportFailure(
                    f"first council ledger event must be round_opened: "
                    f"{self.path}"
                )
            if events and event_type == "round_opened":
                raise CouncilTransportFailure(
                    f"round_opened already exists: {self.path}"
                )
            terminal = next(
                (
                    event
                    for event in events
                    if event.get("event_type") in _TERMINAL_EVENTS
                ),
                None,
            )
            if terminal is not None:
                if event_type in _TERMINAL_EVENTS:
                    raise CouncilTransportFailure(
                        f"round already has terminal event "
                        f"{terminal['event_type']}: {self.path}"
                    )
                if any(
                    event.get("event_type") == "terminal_projected"
                    for event in events
                ):
                    raise CouncilTransportFailure(
                        f"round is already terminal and projected: {self.path}"
                    )
                if event_type not in {
                    "terminal_projection_failed",
                    "terminal_projected",
                }:
                    raise CouncilTransportFailure(
                        f"cannot append {event_type!r} after terminal event: "
                        f"{self.path}"
                    )
            sequence = max(
                (int(event.get("sequence") or 0) for event in events),
                default=0,
            ) + 1
            now = time.time()
            fields = fields_for(events)
            if not isinstance(fields, dict):
                raise CouncilTransportFailure(
                    f"council event fields must be an object: {event_type}"
                )
            reserved = sorted(set(fields) & _RESERVED_EVENT_FIELDS)
            if reserved:
                raise CouncilTransportFailure(
                    f"council event attempted to override reserved fields: "
                    f"{reserved}"
                )
            row = {
                "schema_version": 1,
                "event_type": event_type,
                "sequence": sequence,
                "recorded_at": now,
                "ts": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(now),
                ),
                "conversation_id": self.conversation_id,
                "round_id": self.round_id,
                **fields,
            }
            encoded = (
                json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise CouncilTransportFailure(
                        f"council ledger append made no progress: {self.path}"
                    )
                view = view[written:]
            os.fsync(descriptor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        if new_log:
            self._fsync_directory_chain()
        return row

    def append(self, event_type: str, **fields: Any) -> dict[str, Any]:
        return self._append_with(event_type, lambda _: fields)

    def append_amendment(self, content: str) -> dict[str, Any]:
        if not content.strip():
            raise CouncilTransportFailure("user amendment cannot be empty")

        def fields_for(events: list[dict[str, Any]]) -> dict[str, Any]:
            if any(
                event.get("event_type") in _TERMINAL_EVENTS
                for event in events
            ):
                raise CouncilTransportFailure(
                    f"round {self.round_id} is already terminal"
                )
            revision = max(
                (
                    int(event.get("prompt_revision") or 0)
                    for event in events
                    if event.get("event_type")
                    in {"round_opened", "user_amendment"}
                ),
                default=0,
            ) + 1
            return {
                "prompt_revision": revision,
                "revision_id": uuid.uuid4().hex,
                "content": content.strip(),
                "status": "accepted",
            }

        return self._append_with("user_amendment", fields_for)

    def append_completed(
        self,
        expected_revision: int,
        **fields: Any,
    ) -> dict[str, Any]:
        def fields_for(events: list[dict[str, Any]]) -> dict[str, Any]:
            if any(
                event.get("event_type") in _TERMINAL_EVENTS
                for event in events
            ):
                raise CouncilTransportFailure(
                    f"round {self.round_id} is already terminal"
                )
            latest_revision = max(
                (
                    int(event.get("prompt_revision") or 0)
                    for event in events
                    if event.get("event_type")
                    in {"round_opened", "user_amendment"}
                ),
                default=0,
            )
            if latest_revision != expected_revision:
                raise CouncilRevisionSuperseded(
                    expected_revision,
                    latest_revision,
                )
            return fields

        return self._append_with("round_completed", fields_for)

    def append_failed(
        self,
        expected_revision: int,
        **fields: Any,
    ) -> dict[str, Any]:
        def fields_for(events: list[dict[str, Any]]) -> dict[str, Any]:
            if any(
                event.get("event_type") in _TERMINAL_EVENTS
                for event in events
            ):
                raise CouncilTransportFailure(
                    f"round {self.round_id} is already terminal"
                )
            latest_revision = max(
                (
                    int(event.get("prompt_revision") or 0)
                    for event in events
                    if event.get("event_type")
                    in {"round_opened", "user_amendment"}
                ),
                default=0,
            )
            if latest_revision != expected_revision:
                raise CouncilRevisionSuperseded(
                    expected_revision,
                    latest_revision,
                )
            return fields

        return self._append_with("round_failed", fields_for)

    def append_failed_current(
        self,
        failed_work_revision: int,
        **fields: Any,
    ) -> dict[str, Any]:
        if "prompt_revision" in fields or "failed_work_revision" in fields:
            raise CouncilTransportFailure(
                "current-revision failure fields cannot override revision identity"
            )

        def fields_for(events: list[dict[str, Any]]) -> dict[str, Any]:
            if any(
                event.get("event_type") in _TERMINAL_EVENTS
                for event in events
            ):
                raise CouncilTransportFailure(
                    f"round {self.round_id} is already terminal"
                )
            latest_revision = max(
                (
                    int(event.get("prompt_revision") or 0)
                    for event in events
                    if event.get("event_type")
                    in {"round_opened", "user_amendment"}
                ),
                default=0,
            )
            return {
                **fields,
                "prompt_revision": latest_revision,
                "failed_work_revision": failed_work_revision,
            }

        return self._append_with("round_failed", fields_for)

    def events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        events = _read_jsonl(self.path)
        self._validate_events(events)
        return [
            event
            for event in events
            if int(event.get("sequence") or 0) > after_sequence
        ]

    def opened_event(self) -> dict[str, Any]:
        for event in self.events():
            if event.get("event_type") == "round_opened":
                return event
        raise CouncilTransportFailure(
            f"round has no durable round_opened event: {self.round_id}"
        )

    def terminal_event(self) -> dict[str, Any] | None:
        terminal: dict[str, Any] | None = None
        for event in self.events():
            if event.get("event_type") in _TERMINAL_EVENTS:
                terminal = event
        return terminal

    def has_event(self, event_type: str) -> bool:
        return any(
            event.get("event_type") == event_type
            for event in self.events()
        )

    def latest_revision(self) -> int:
        revisions = [
            int(event.get("prompt_revision") or 0)
            for event in self.events()
            if event.get("event_type") in {"round_opened", "user_amendment"}
        ]
        if not revisions:
            raise CouncilTransportFailure(
                f"round has no prompt revision: {self.round_id}"
            )
        return max(revisions)

    def prompt_for_revision(self, revision: int) -> str:
        opened = self.opened_event()
        prompt = str(opened.get("user_prompt") or "").strip()
        if not prompt:
            raise CouncilTransportFailure(
                f"round has no user prompt: {self.round_id}"
            )
        amendments = [
            event
            for event in self.events()
            if event.get("event_type") == "user_amendment"
            and int(event.get("prompt_revision") or 0) <= revision
        ]
        sections = [
            "[CURRENT USER REQUEST]\n"
            f"{prompt}\n"
            "[/CURRENT USER REQUEST]"
        ]
        executive_context = opened.get("executive_context")
        if isinstance(executive_context, list) and executive_context:
            sections.append(
                "[EXECUTIVE CONVERSATION CONTEXT]\n"
                + json.dumps(
                    executive_context,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n[/EXECUTIVE CONVERSATION CONTEXT]"
            )
        for event in amendments:
            sections.append(
                "[USER AMENDMENT "
                f"revision={event['prompt_revision']} "
                f"revision_id={event['revision_id']}]\n"
                f"{event['content']}\n"
                "[/USER AMENDMENT]"
            )
        return "\n\n".join(sections)

    def snapshot(self) -> dict[str, Any]:
        events = self.events()
        terminal = self.terminal_event()
        return {
            "round_id": self.round_id,
            "conversation_id": self.conversation_id,
            "prompt_revision": self.latest_revision(),
            "status": (
                "completed"
                if terminal and terminal.get("event_type") == "round_completed"
                else "failed"
                if terminal
                else "open"
            ),
            "last_sequence": max(
                (int(event.get("sequence") or 0) for event in events),
                default=0,
            ),
            "terminal_event": terminal,
        }


class NativeCouncilTransport:
    def __init__(
        self,
        redis_client: redis.Redis,
        sessions_dir: Path,
        *,
        key_prefix: str = "taey",
        council_log_dir: Path | None = None,
        wave_timeout: float = 1800.0,
        poll_interval: float = 0.25,
    ):
        self.redis = redis_client
        self.sessions_dir = sessions_dir
        self.key_prefix = key_prefix
        self.council_log_dir = (
            council_log_dir
            if council_log_dir is not None
            else sessions_dir / "council"
        )
        self.wave_timeout = max(1.0, wave_timeout)
        self.poll_interval = max(0.05, poll_interval)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def _active_key(self, conversation_id: str) -> str:
        return (
            f"{self.key_prefix}:dcm:native:conversation:"
            f"{conversation_id}:active_round"
        )

    def _dispatch_key(self, round_id: str) -> str:
        return f"{self.key_prefix}:dcm:native:round:{round_id}:dispatched"

    def ledger(self, conversation_id: str, round_id: str) -> RoundLedger:
        return RoundLedger(self.sessions_dir, conversation_id, round_id)

    def active_round(self, conversation_id: str) -> dict[str, Any] | None:
        round_id = self.redis.get(self._active_key(conversation_id))
        if not round_id:
            return None
        ledger = self.ledger(conversation_id, str(round_id))
        terminal = ledger.terminal_event()
        if terminal and ledger.has_event("terminal_projected"):
            self.redis.eval(
                _CLEAR_ACTIVE_LUA,
                1,
                self._active_key(conversation_id),
                str(round_id),
            )
            return None
        return ledger.snapshot()

    def _new_round_id(self) -> str:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        return f"dcm-{stamp}-{uuid.uuid4().hex[:12]}"

    async def start_round(
        self,
        conversation_id: str,
        user_prompt: str,
        *,
        executive_event_id: str,
        executive_context: list[dict[str, str]] | None,
        synthesize: SynthesizeCallback,
        record_terminal: TerminalCallback,
    ) -> RoundLedger:
        if not user_prompt.strip():
            raise CouncilTransportFailure("council user prompt cannot be empty")
        active = self.active_round(conversation_id)
        if active is not None:
            raise CouncilTransportFailure(
                f"conversation already has open council round "
                f"{active['round_id']}"
            )
        round_id = self._new_round_id()
        ledger = self.ledger(conversation_id, round_id)
        ledger.append(
            "round_opened",
            council_protocol="taey-native-dcm/v2",
            executive_event_id=_safe_id(executive_event_id),
            user_prompt=user_prompt.strip(),
            executive_context=executive_context or [],
            prompt_revision=1,
            revision_id=uuid.uuid4().hex,
            seat_ids=[seat.seat_id for seat in COUNCIL_SEATS],
            role_ids=[seat.role_id for seat in COUNCIL_SEATS],
            status="open",
        )
        claimed = self.redis.set(
            self._active_key(conversation_id),
            round_id,
            nx=True,
        )
        if not claimed:
            active = self.active_round(conversation_id)
            ledger.append(
                "round_failed",
                prompt_revision=1,
                status="failed",
                error=(
                    "conversation acquired another council round"
                    + (f" {active['round_id']}" if active else "")
                ),
            )
            raise CouncilTransportFailure(
                "conversation acquired another council round"
                + (f" {active['round_id']}" if active else "")
            )
        self._spawn(
            ledger,
            synthesize=synthesize,
            record_terminal=record_terminal,
        )
        return ledger

    def _spawn(
        self,
        ledger: RoundLedger,
        *,
        synthesize: SynthesizeCallback,
        record_terminal: TerminalCallback,
    ) -> None:
        existing = self._tasks.get(ledger.round_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run_round(
                ledger,
                synthesize=synthesize,
                record_terminal=record_terminal,
            ),
            name=f"taey-native-council:{ledger.round_id}",
        )
        self._tasks[ledger.round_id] = task

        def forget(done: asyncio.Task[None]) -> None:
            self._tasks.pop(ledger.round_id, None)
            if done.cancelled():
                return
            failure = done.exception()
            if failure is not None:
                log.error(
                    "native council coordinator escaped durable recovery "
                    "round=%s",
                    ledger.round_id,
                    exc_info=(
                        type(failure),
                        failure,
                        failure.__traceback__,
                    ),
                )

        task.add_done_callback(forget)

    async def resume_active_rounds(
        self,
        *,
        synthesize: SynthesizeCallback,
        record_terminal: TerminalCallback,
    ) -> list[str]:
        pattern = (
            f"{self.key_prefix}:dcm:native:conversation:*:active_round"
        )
        resumed: list[str] = []
        for raw_key in self.redis.scan_iter(match=pattern):
            key = str(raw_key)
            marker = f"{self.key_prefix}:dcm:native:conversation:"
            if not key.startswith(marker) or not key.endswith(":active_round"):
                continue
            conversation_id = key[len(marker) : -len(":active_round")]
            if not _SESSION_RE.fullmatch(conversation_id):
                continue
            round_id = self.redis.get(key)
            if not round_id:
                continue
            ledger = self.ledger(conversation_id, str(round_id))
            if not ledger.path.exists():
                raise CouncilTransportFailure(
                    f"active round points to a missing ledger: {round_id}"
                )
            if (
                ledger.terminal_event()
                and ledger.has_event("terminal_projected")
            ):
                self.redis.eval(
                    _CLEAR_ACTIVE_LUA,
                    1,
                    key,
                    str(round_id),
                )
                continue
            self._spawn(
                ledger,
                synthesize=synthesize,
                record_terminal=record_terminal,
            )
            resumed.append(str(round_id))
        return resumed

    def amend(self, conversation_id: str, round_id: str, content: str) -> dict[str, Any]:
        active = self.redis.get(self._active_key(conversation_id))
        if str(active or "") != round_id:
            raise CouncilTransportFailure(
                f"round {round_id} is not active for {conversation_id}"
            )
        ledger = self.ledger(conversation_id, round_id)
        if ledger.terminal_event():
            raise CouncilTransportFailure(f"round {round_id} is already terminal")
        return ledger.append_amendment(content)

    @staticmethod
    def _dcm_adapter() -> Any:
        if not os.environ.get("DCM_NEO4J_URI") or not os.environ.get(
            "DCM_NEO4J_DATABASE"
        ):
            raise CouncilTransportFailure(
                "DCM_NEO4J_URI and DCM_NEO4J_DATABASE are required for council dispatch"
            )
        import taey_adapter

        if (
            taey_adapter.mesh.DCM_NEO4J_URI != os.environ["DCM_NEO4J_URI"]
            or taey_adapter.mesh.DCM_NEO4J_DATABASE
            != os.environ["DCM_NEO4J_DATABASE"]
        ):
            raise CouncilTransportFailure(
                "loaded DCM graph identity differs from the explicit council environment"
            )
        return taey_adapter

    @staticmethod
    def _graph_session_payload(ledger: RoundLedger) -> str:
        opened = ledger.opened_event()
        return prompt_producer.canonical_json(
            {
                "conversation_id": ledger.conversation_id,
                "executive_context": opened.get("executive_context") or [],
                "executive_event_id": opened["executive_event_id"],
                "user_prompt": opened["user_prompt"],
            }
        )

    def _ensure_graph_session(self, ledger: RoundLedger) -> dict[str, Any]:
        adapter = self._dcm_adapter()
        topic = f"Taey native council round {ledger.round_id}"
        payload = self._graph_session_payload(ledger)
        try:
            session = adapter.mesh.read_session(ledger.round_id)
        except ValueError:
            try:
                adapter.mesh.start_session(
                    topic,
                    payload,
                    [seat.role_id for seat in COUNCIL_SEATS],
                    session_id=ledger.round_id,
                )
            except adapter.mesh.SessionIdentityConflictError:
                pass
            session = adapter.mesh.read_session(ledger.round_id)
        if (
            session.get("topic") != topic
            or session.get("payload") != payload
            or session.get("status") != "open"
        ):
            raise CouncilTransportFailure(
                "DCM session identity or open state differs from the durable council round"
            )
        return session

    @staticmethod
    def _verified_prompt_contracts() -> dict[str, str]:
        manifest = prompt_producer.load_manifest(_COUNCIL_MANIFEST_PATH)
        return {
            seat.seat_id: prompt_producer.prompt_contract_receipt(
                manifest,
                seat,
            )["prompt_contract_sha256"]
            for seat in manifest.seats
        }

    @staticmethod
    def _verified_model_identity() -> dict[str, Any]:
        aliases = sorted(
            os.environ.get("TAEY_MODEL_IDENTITY_EXPECTED_ALIASES", "").split()
        )
        endpoint = os.environ.get(
            "TAEY_MODEL_IDENTITY_UPSTREAM_COMPLETION_ENDPOINT",
            "",
        )
        if not aliases or len(aliases) != len(set(aliases)) or not endpoint:
            raise CouncilTransportFailure(
                "model identity aliases and upstream completion endpoint are required"
            )
        try:
            publication, ttl_ms = model_identity_status.read_publication(
                aliases,
                endpoint,
                False,
            )
        except (
            OSError,
            TypeError,
            ValueError,
            model_identity_status.VerificationError,
        ) as exc:
            raise CouncilTransportFailure(
                f"live model identity could not be verified: {exc}"
            ) from exc
        return {
            "publication": publication,
            "ttl_ms": ttl_ms,
            "served_aliases": aliases,
        }

    def _open_graph_wave(
        self,
        *,
        ledger: RoundLedger,
        phase: str,
        prompt_revision: int,
        body: str,
        registrations: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        adapter = self._dcm_adapter()
        self._ensure_graph_session(ledger)
        prompt_contracts = self._verified_prompt_contracts()
        model_identity = self._verified_model_identity()
        publication = model_identity["publication"]
        model_receipt_sha256 = publication["receipt_sha256"]
        required_members = []
        for seat in COUNCIL_SEATS:
            registration = registrations[seat.seat_id]
            if (
                registration["dcm_v2_prompt_contract_sha256"]
                != prompt_contracts.get(seat.seat_id)
                or registration["requested_alias"]
                not in model_identity["served_aliases"]
            ):
                raise CouncilTransportFailure(
                    f"{seat.seat_id} prompt or model registration is not independently reproducible"
                )
            required_members.append(
                {
                    "seat_id": seat.seat_id,
                    "role": seat.role_id,
                    "prompt_contract_sha256": prompt_contracts[seat.seat_id],
                    "model_identity_receipt_sha256": model_receipt_sha256,
                }
            )
        prior_waves = [
            event["dcm_wave_id"]
            for event in ledger.events()
            if event.get("event_type") == "wave_dispatch_prepared"
            and isinstance(event.get("dcm_wave_id"), str)
        ]
        parent_wave_id = prior_waves[-1] if prior_waves else None
        opened = ledger.opened_event()
        wave = adapter.mesh.open_wave(
            ledger.round_id,
            round=1,
            phase=phase,
            prompt_id=str(opened["executive_event_id"]),
            prompt_revision=prompt_revision,
            prompt_messages=[{"role": "user", "content": body}],
            attachment_evidence_digests=[],
            request_revision=1,
            required_members=required_members,
            request_contract=prompt_producer.DCM_REQUEST_CONTRACT,
            parent_wave_id=parent_wave_id,
        )
        return wave, publication

    def _seat_log(self, seat: CouncilSeat) -> Path:
        return self.council_log_dir / f"{seat.seat_id}.jsonl"

    def _result_key(self, round_id: str) -> str:
        return f"{self.key_prefix}:dcm:native:round:{round_id}:results"

    def _matching_outcome(
        self,
        seat: CouncilSeat,
        *,
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        receipts = []
        for raw in self.redis.lrange(self._result_key(request["dcm_session_id"]), 0, -1):
            try:
                receipt = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as exc:
                raise CouncilTransportFailure(
                    "DCM result list contains a non-JSON receipt"
                ) from exc
            if not isinstance(receipt, dict):
                raise CouncilTransportFailure(
                    "DCM result list contains a non-object receipt"
                )
            if receipt.get("request_id") == request["request_id"]:
                receipts.append(receipt)
        if not receipts:
            return None
        if len(receipts) != 1:
            raise CouncilTransportFailure(
                f"{seat.seat_id} has duplicate DCM terminal receipts"
            )
        receipt = receipts[0]
        stated_receipt_sha256 = receipt.get("receipt_sha256")
        unhashed_receipt = {
            key: value for key, value in receipt.items() if key != "receipt_sha256"
        }
        if stated_receipt_sha256 != prompt_producer.canonical_sha256(
            unhashed_receipt
        ):
            raise CouncilTransportFailure(
                f"{seat.seat_id} DCM transport receipt digest is invalid"
            )
        expected = {
            "contract": "taey-native-dcm-receipt/v2",
            "receipt_kind": "transport",
            "session_id": request["dcm_session_id"],
            "correlation_id": request["dcm_session_id"],
            "wave_id": request["wave_id"],
            "round": request["round"],
            "phase": request["phase"],
            "seat_id": seat.seat_id,
            "role": seat.role_id,
            "request_revision": request["request_revision"],
            "request_id": request["request_id"],
            "stage": "terminal_acknowledged",
            "delivery_id": request["delivery_id"],
            "request_contract": prompt_producer.DCM_REQUEST_CONTRACT,
            "prompt_contract_sha256": request["prompt_contract_sha256"],
            "model_identity_receipt_sha256": request[
                "model_identity_receipt_sha256"
            ],
        }
        mismatched = [
            field for field, value in expected.items() if receipt.get(field) != value
        ]
        adapter = self._dcm_adapter()
        wave = adapter.mesh.read_wave(
            request["dcm_session_id"],
            request["wave_id"],
        )
        slots = [
            slot
            for slot in wave["slots"]
            if slot.get("request_id") == request["request_id"]
        ]
        if len(slots) != 1:
            raise CouncilTransportFailure(
                f"{seat.seat_id} receipt does not resolve to one graph slot"
            )
        slot = slots[0]
        request_identity = slot.get("request_identity")
        request_identity_fields = (
            "session_id",
            "wave_id",
            "round",
            "phase",
            "prompt_id",
            "prompt_revision",
            "prompt_sha256",
            "seat_id",
            "role",
            "request_revision",
            "parent_frontier_sha256",
            "process_generation_expected",
            "model_endpoint",
            "requested_alias",
            "model_manifest_sha256",
            "model_content_sha256",
            "serving_container_digest",
            "request_contract",
            "prompt_contract_sha256",
            "model_identity_receipt_sha256",
        )
        expected_request_identity = {
            field: request[field] for field in request_identity_fields
        }
        if request_identity != expected_request_identity:
            raise CouncilTransportFailure(
                f"{seat.seat_id} Redis delivery differs from graph request identity"
            )
        claim_observation = slot.get("claim_observation")
        observed_generation = (
            claim_observation.get("process_generation_observed")
            if isinstance(claim_observation, dict)
            else None
        )
        served_alias = (
            claim_observation.get("served_alias")
            if isinstance(claim_observation, dict)
            else None
        )
        expected_prompt = {
            "prompt_id": request["prompt_id"],
            "revision": request["prompt_revision"],
            "sha256": request["prompt_sha256"],
        }
        expected_frontier = {
            "parent_contribution_ids": request["parent_contribution_ids"],
            "parent_frontier_sha256": request["parent_frontier_sha256"],
            "claimed_peers": request["parent_contribution_ids"],
            "peers_present": request["parent_contribution_ids"],
        }
        receipt_execution = receipt.get("execution")
        if not isinstance(receipt_execution, dict):
            raise CouncilTransportFailure(
                f"{seat.seat_id} DCM transport receipt has no execution binding"
            )
        expected_execution = {
            "model_endpoint": request["model_endpoint"],
            "process_generation_expected": request[
                "process_generation_expected"
            ],
            "process_generation_observed": observed_generation,
            "requested_alias": request["requested_alias"],
            "served_alias": served_alias,
            "model_manifest_sha256": request["model_manifest_sha256"],
            "model_content_sha256": request["model_content_sha256"],
            "serving_container_digest": request["serving_container_digest"],
        }
        if receipt.get("prompt") != expected_prompt:
            mismatched.append("prompt")
        if receipt.get("frontier") != expected_frontier:
            mismatched.append("frontier")
        if receipt.get("execution") != expected_execution:
            mismatched.append("execution")
        if receipt.get("graph") != {
            "uri": adapter.mesh.DCM_NEO4J_URI,
            "database": adapter.mesh.DCM_NEO4J_DATABASE,
        }:
            mismatched.append("graph")
        if mismatched:
            raise CouncilTransportFailure(
                f"{seat.seat_id} DCM transport receipt changed fields "
                f"{sorted(set(mismatched))}"
            )
        if receipt.get("terminal_outcome") == "contributed":
            contributions = [
                contribution
                for contribution in wave["contributions"]
                if contribution.get("contrib_id") == receipt.get("contrib_id")
            ]
            if (
                len(contributions) != 1
                or slot.get("state") != "contributed"
                or receipt.get("inference_performed") is not True
                or slot.get("contrib_id") != receipt.get("contrib_id")
                or contributions[0].get("contribution_receipt_sha256")
                != receipt.get("contribution_receipt_sha256")
                or not isinstance(
                    contributions[0].get("structured_content"),
                    dict,
                )
            ):
                raise CouncilTransportFailure(
                    f"{seat.seat_id} contribution receipt is not graph-authoritative"
                )
            graph_receipt_sha256 = contributions[0][
                "contribution_receipt_sha256"
            ]
            contribution = contributions[0]["structured_content"]
            ok = True
            error = None
        else:
            outcome_record = slot.get("outcome_record")
            if (
                slot.get("terminal_outcome")
                != receipt.get("terminal_outcome")
                or slot.get("inference_performed")
                != receipt.get("inference_performed")
                or receipt.get("contrib_id") is not None
                or receipt.get("contribution_receipt_sha256") is not None
                or not isinstance(outcome_record, dict)
                or receipt.get("failure_stage")
                != outcome_record.get("failure_stage")
                or receipt.get("failure_detail_sha256")
                != outcome_record.get("failure_detail_sha256")
            ):
                raise CouncilTransportFailure(
                    f"{seat.seat_id} failure receipt is not graph-authoritative"
                )
            graph_receipt_sha256 = outcome_record.get("outcome_record_sha256")
            contribution = None
            ok = False
            error = (
                f"DCM request ended as {receipt.get('terminal_outcome')} at "
                f"{receipt.get('failure_stage')}"
            )
        expected_acknowledgement = prompt_producer.canonical_sha256(
            {
                "delivery_id": request["delivery_id"],
                "graph_receipt_sha256": graph_receipt_sha256,
                "request_id": request["request_id"],
                "terminal_outcome": receipt["terminal_outcome"],
            }
        )
        if receipt.get("acknowledgement_id") != expected_acknowledgement:
            raise CouncilTransportFailure(
                f"{seat.seat_id} acknowledgement is not bound to graph truth"
            )
        return {
            "ok": ok,
            "event_id": receipt["acknowledgement_id"],
            "process_generation": request["expected_process_generation"],
            "proxy_turn_id": None,
            "contribution": contribution,
            "error": error,
            "kind": (
                "council_contribution"
                if ok
                else f"dcm_{receipt['terminal_outcome']}"
            ),
            "inference_state": (
                "completed"
                if ok
                else "failed"
                if receipt["inference_performed"]
                else "not_started"
            ),
            "dcm_transport_receipt": receipt,
        }

    def _message_id(
        self,
        round_id: str,
        prompt_revision: int,
        phase: str,
        seat: CouncilSeat,
    ) -> str:
        identity = (
            f"{round_id}\0{prompt_revision}\0{phase}\0{seat.seat_id}"
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    def _request_id(
        self,
        round_id: str,
        prompt_revision: int,
        phase: str,
        seat: CouncilSeat,
    ) -> str:
        return _safe_id(
            f"{round_id}:{prompt_revision}:{phase}:{seat.seat_id}"
        )

    def _wave_body(
        self,
        *,
        phase: str,
        prompt: str,
        prompt_revision: int,
        revealed: list[dict[str, Any]] | None,
    ) -> str:
        packet: dict[str, Any] = {
            "council_protocol": "taey-native-dcm/v2",
            "phase": phase,
            "prompt_revision": prompt_revision,
            "request": prompt,
        }
        if phase == "independent":
            packet["instructions"] = (
                "Work independently in your stable role. Do not infer another "
                "seat's view. Return the required structured contribution."
            )
        else:
            packet["revealed_contributions"] = revealed or []
            packet["instructions"] = (
                "Critique the revealed packet only through your stable role. "
                "Name supported agreement, material dissent, missing evidence, "
                "and any change to your recommendation. Return the required "
                "structured contribution."
            )
        return (
            "[TAEY-NATIVE DCM REQUEST]\n"
            + json.dumps(
                packet,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n[/TAEY-NATIVE DCM REQUEST]"
        )

    def _wave_requests(
        self,
        *,
        ledger: RoundLedger,
        phase: str,
        prompt_revision: int,
        registrations: dict[str, dict[str, Any]],
        wave: dict[str, Any],
        model_identity: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        adapter = self._dcm_adapter()
        requests = {}
        for seat in COUNCIL_SEATS:
            slots = [
                slot
                for slot in wave["slots"]
                if slot.get("seat_id") == seat.seat_id
                and slot.get("role") == seat.role_id
                and slot.get("request_revision") == 1
            ]
            if len(slots) != 1:
                raise CouncilTransportFailure(
                    f"{seat.seat_id} does not resolve to one immutable graph slot"
                )
            slot = slots[0]
            registration = registrations[seat.seat_id]
            request_identity = slot.get("request_identity")
            if request_identity is None:
                if model_identity is None:
                    raise CouncilTransportFailure(
                        f"{seat.seat_id} graph request identity is missing"
                    )
                receipt = model_identity["receipt"]
                request_identity = {
                    "session_id": wave["session_id"],
                    "wave_id": wave["wave_id"],
                    "round": wave["round"],
                    "phase": wave["phase"],
                    "prompt_id": wave["prompt_id"],
                    "prompt_revision": wave["prompt_revision"],
                    "prompt_sha256": wave["prompt_sha256"],
                    "seat_id": seat.seat_id,
                    "role": seat.role_id,
                    "request_revision": 1,
                    "parent_frontier_sha256": wave[
                        "parent_frontier_sha256"
                    ],
                    "process_generation_expected": registration[
                        "process_generation"
                    ],
                    "model_endpoint": registration["model_endpoint"],
                    "requested_alias": registration["requested_alias"],
                    "model_manifest_sha256": receipt["model"][
                        "model_manifest_sha256"
                    ],
                    "model_content_sha256": receipt["model"][
                        "model_content_sha256"
                    ],
                    "serving_container_digest": receipt["serving"][
                        "image_digest"
                    ],
                    "request_contract": prompt_producer.DCM_REQUEST_CONTRACT,
                    "prompt_contract_sha256": slot[
                        "prompt_contract_sha256"
                    ],
                    "model_identity_receipt_sha256": slot[
                        "model_identity_receipt_sha256"
                    ],
                }
            expected_registration = {
                "seat_id": seat.seat_id,
                "role": seat.role_id,
                "process_generation_expected": registration[
                    "process_generation"
                ],
                "model_endpoint": registration["model_endpoint"],
                "requested_alias": registration["requested_alias"],
                "prompt_contract_sha256": registration[
                    "dcm_v2_prompt_contract_sha256"
                ],
            }
            mismatched = [
                field
                for field, value in expected_registration.items()
                if request_identity.get(field) != value
            ]
            if mismatched:
                raise CouncilTransportFailure(
                    f"{seat.seat_id} graph request differs from its durable registration: "
                    f"{sorted(mismatched)}"
                )
            request_id = adapter.mesh.canonical_wave_request_id(
                request_identity
            )
            adapter.mesh.reserve_wave_request(
                ledger.round_id,
                wave["wave_id"],
                role=seat.role_id,
                request_revision=1,
                request_identity=request_identity,
                parent_contribution_ids=wave["parent_contribution_ids"],
            )
            requests[seat.seat_id] = {
                **request_identity,
                "dcm_session_id": ledger.round_id,
                "delivery_id": self._message_id(
                    ledger.round_id,
                    prompt_revision,
                    phase,
                    seat,
                ),
                "seat_id": seat.seat_id,
                "role_id": seat.role_id,
                "message_id": self._message_id(
                    ledger.round_id,
                    prompt_revision,
                    phase,
                    seat,
                ),
                "request_id": request_id,
                "parent_contribution_ids": list(
                    wave["parent_contribution_ids"]
                ),
                "expected_process_generation": registrations[seat.seat_id][
                    "process_generation"
                ],
            }
        return requests

    def _dispatch_state(
        self,
        *,
        ledger: RoundLedger,
        phase: str,
        prompt_revision: int,
    ) -> list[bool]:
        tokens = [
            f"{prompt_revision}:{phase}:{seat.seat_id}"
            for seat in COUNCIL_SEATS
        ]
        raw_state = self.redis.eval(
            _READ_DISPATCH_STATE_LUA,
            1,
            self._dispatch_key(ledger.round_id),
            *tokens,
        )
        if not isinstance(raw_state, list) or len(raw_state) != len(
            COUNCIL_SEATS
        ):
            raise CouncilTransportFailure(
                "council wave dispatch state returned an invalid result"
            )
        return [bool(int(value)) for value in raw_state]

    def _enqueue(
        self,
        *,
        ledger: RoundLedger,
        phase: str,
        prompt_revision: int,
        body: str,
        registrations: dict[str, dict[str, Any]],
        requests: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        encoded_requests: list[tuple[str, str]] = []
        requested_at = time.time()
        for seat in COUNCIL_SEATS:
            request = requests[seat.seat_id]
            payload = {
                "from": "taey",
                "type": "council_request",
                "body": body,
                "timestamp": requested_at,
                "priority": "high",
                "msg_id": request["message_id"],
                "event_id": request["request_id"],
                "correlation_id": ledger.round_id,
                "request_id": request["request_id"],
                "council_run_id": ledger.round_id,
                "round_id": ledger.round_id,
                "prompt_revision": prompt_revision,
                "round_phase": phase,
                "expected_process_generation": request[
                    "expected_process_generation"
                ],
                "request_contract": request["request_contract"],
                "delivery_id": request["delivery_id"],
                "dcm_session_id": request["dcm_session_id"],
                "wave_id": request["wave_id"],
                "round": request["round"],
                "phase": request["phase"],
                "prompt_id": request["prompt_id"],
                "prompt_sha256": request["prompt_sha256"],
                "seat_id": request["seat_id"],
                "role": request["role"],
                "request_revision": request["request_revision"],
                "parent_contribution_ids": request[
                    "parent_contribution_ids"
                ],
                "parent_frontier_sha256": request[
                    "parent_frontier_sha256"
                ],
                "process_generation_expected": request[
                    "process_generation_expected"
                ],
                "model_endpoint": request["model_endpoint"],
                "requested_alias": request["requested_alias"],
                "model_manifest_sha256": request[
                    "model_manifest_sha256"
                ],
                "model_content_sha256": request["model_content_sha256"],
                "serving_container_digest": request[
                    "serving_container_digest"
                ],
                "prompt_contract_sha256": request[
                    "prompt_contract_sha256"
                ],
                "model_identity_receipt_sha256": request[
                    "model_identity_receipt_sha256"
                ],
            }
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            token = f"{prompt_revision}:{phase}:{seat.seat_id}"
            encoded_requests.append((token, encoded))
        keys = [
            self._dispatch_key(ledger.round_id),
            f"{self.key_prefix}:dcm:native:seat_replacement",
        ]
        keys.extend(
            f"{self.key_prefix}:{seat.seat_id}:seat_registration"
            for seat in COUNCIL_SEATS
        )
        keys.extend(
            f"{self.key_prefix}:{seat.seat_id}:inbox"
            for seat in COUNCIL_SEATS
        )
        arguments: list[Any] = [len(COUNCIL_SEATS)]
        arguments.extend(
            registrations[seat.seat_id]["raw_registration"]
            for seat in COUNCIL_SEATS
        )
        for token, encoded in encoded_requests:
            arguments.extend((token, encoded))
        enqueue_results = self.redis.eval(
            _ENQUEUE_LUA,
            len(keys),
            *keys,
            *arguments,
        )
        if not isinstance(enqueue_results, list) or len(enqueue_results) != len(
            COUNCIL_SEATS
        ):
            raise CouncilTransportFailure(
                "atomic council wave enqueue returned an invalid result"
            )
        for seat, raw_enqueued in zip(COUNCIL_SEATS, enqueue_results):
            request = requests[seat.seat_id]
            request["enqueued"] = bool(int(raw_enqueued))
            if not request["enqueued"]:
                continue
            try:
                self.redis.xadd(
                    f"{self.key_prefix}:notify_trace",
                    {
                        "ev": "enqueue",
                        "node": seat.seat_id,
                        "src": "taey-native-dcm",
                        "type": "council_request",
                        "frm": "taey",
                        "wall": f"{time.time():.3f}",
                        "msg_id": request["message_id"],
                    },
                    maxlen=50000,
                    approximate=True,
                )
            except Exception as exc:
                ledger.append(
                    "dispatch_trace_failed",
                    seat_id=seat.seat_id,
                    role_id=seat.role_id,
                    message_id=request["message_id"],
                    error=f"{type(exc).__name__}: {exc}",
                )
        return requests

    def _live_seat_registration(self, seat: CouncilSeat) -> dict[str, Any]:
        registration_key = (
            f"{self.key_prefix}:{seat.seat_id}:seat_registration"
        )
        snapshot = self.redis.eval(
            _READ_LIVE_REGISTRATION_LUA,
            1,
            registration_key,
        )
        if not isinstance(snapshot, list) or len(snapshot) != 2:
            raise CouncilTransportFailure(
                f"{seat.seat_id} has no live generation registration"
            )
        raw_registration, raw_ttl = snapshot
        try:
            registration = json.loads(raw_registration)
            ttl_seconds = int(raw_ttl)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CouncilTransportFailure(
                f"{seat.seat_id} has an invalid generation registration"
            ) from exc
        if (
            not isinstance(registration, dict)
            or registration.get("schema_version") != 1
            or registration.get("seat_id") != seat.seat_id
            or registration.get("seat_kind") != "council"
            or registration.get("role_id") != seat.role_id
            or not registration.get("process_generation")
            or registration.get("response_contract")
            != "taey-council-contribution/v1"
            or registration.get("prompt_contract_producer_state")
            != "self_asserted_unverified"
            or not model_identity_status.is_sha256(
                registration.get("dcm_v2_prompt_contract_sha256")
            )
            or not isinstance(registration.get("model_endpoint"), str)
            or not registration["model_endpoint"].strip()
            or not isinstance(registration.get("requested_alias"), str)
            or not registration["requested_alias"].strip()
            or registration.get("readiness") != "ready"
            or type(registration.get("pid")) is not int
            or registration["pid"] <= 0
            or type(registration.get("liveness_ttl_seconds")) is not int
            or registration["liveness_ttl_seconds"] < 2
            or ttl_seconds <= 0
        ):
            raise CouncilTransportFailure(
                f"{seat.seat_id} generation registration is stale or mismatched"
            )
        return {
            "process_generation": registration["process_generation"],
            "dcm_v2_prompt_contract_sha256": registration[
                "dcm_v2_prompt_contract_sha256"
            ],
            "model_endpoint": registration["model_endpoint"],
            "requested_alias": registration["requested_alias"],
            "ttl_seconds": ttl_seconds,
            "raw_registration": raw_registration,
        }

    async def _dispatch_wave(
        self,
        ledger: RoundLedger,
        *,
        phase: str,
        prompt: str,
        prompt_revision: int,
        revealed: list[dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        body = self._wave_body(
            phase=phase,
            prompt=prompt,
            prompt_revision=prompt_revision,
            revealed=revealed,
        )
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        matching_events = [
            event
            for event in ledger.events()
            if event.get("event_type") == "wave_dispatch_prepared"
            and event.get("phase") == phase
            and int(event.get("prompt_revision") or 0) == prompt_revision
        ]
        if len(matching_events) > 1:
            raise CouncilTransportFailure(
                "council wave has multiple durable dispatch preparations"
            )
        terminal_wave_events = [
            event
            for event in ledger.events()
            if event.get("event_type")
            in {
                "wave_completed",
                "superseded_wave_drained",
                "superseded_wave_drain_failed",
                "superseded_wave_not_dispatched",
            }
            and event.get("phase") == phase
            and int(event.get("prompt_revision") or 0) == prompt_revision
        ]
        if len(terminal_wave_events) > 1:
            raise CouncilTransportFailure(
                "council wave has multiple durable terminal states"
            )
        if terminal_wave_events and not matching_events:
            raise CouncilTransportFailure(
                "council wave terminal state has no durable dispatch preparation"
            )
        dispatch_state = self._dispatch_state(
            ledger=ledger,
            phase=phase,
            prompt_revision=prompt_revision,
        )
        dispatched_count = sum(dispatch_state)
        if dispatched_count not in {0, len(COUNCIL_SEATS)}:
            raise CouncilTransportFailure(
                "council wave has a partial atomic dispatch identity"
            )
        if matching_events:
            prepared = matching_events[0]
            if prepared.get("body_sha256") != body_sha256:
                raise CouncilTransportFailure(
                    "durable council wave body does not match current prompt"
                )
            prepared_registrations = prepared.get("registrations")
            if not isinstance(prepared_registrations, dict):
                raise CouncilTransportFailure(
                    "durable council wave registrations are invalid"
                )
            registrations: dict[str, dict[str, Any]] = {}
            for seat in COUNCIL_SEATS:
                registration = prepared_registrations.get(seat.seat_id)
                if (
                    not isinstance(registration, dict)
                    or registration.get("role_id") != seat.role_id
                    or not registration.get("process_generation")
                    or not isinstance(registration.get("raw_registration"), str)
                    or not model_identity_status.is_sha256(
                        registration.get("dcm_v2_prompt_contract_sha256")
                    )
                    or not isinstance(registration.get("model_endpoint"), str)
                    or not registration["model_endpoint"].strip()
                    or not isinstance(registration.get("requested_alias"), str)
                    or not registration["requested_alias"].strip()
                    or type(registration.get("ttl_seconds")) is not int
                    or registration["ttl_seconds"] <= 0
                ):
                    raise CouncilTransportFailure(
                        f"durable {seat.seat_id} wave registration is invalid"
                    )
                registrations[seat.seat_id] = registration
        else:
            if dispatched_count:
                raise CouncilTransportFailure(
                    "council wave was dispatched without durable preparation"
                )
            registrations = {
                seat.seat_id: self._live_seat_registration(seat)
                for seat in COUNCIL_SEATS
            }
        adapter = self._dcm_adapter()
        if matching_events:
            prepared = matching_events[0]
            wave_id = prepared.get("dcm_wave_id")
            if not isinstance(wave_id, str):
                raise CouncilTransportFailure(
                    "durable council wave preparation has no DCM wave identity"
                )
            wave = adapter.mesh.read_wave(ledger.round_id, wave_id)
            expected_prompt_sha256 = adapter.mesh.canonical_prompt_sha256(
                [{"role": "user", "content": body}],
                [],
            )
            if (
                wave.get("phase") != phase
                or wave.get("prompt_revision") != prompt_revision
                or wave.get("prompt_sha256") != expected_prompt_sha256
                or wave.get("wave_fingerprint")
                != prepared.get("dcm_wave_fingerprint")
                or wave.get("request_contract")
                != prompt_producer.DCM_REQUEST_CONTRACT
            ):
                raise CouncilTransportFailure(
                    "durable council preparation differs from its graph wave"
                )
            model_identity = None
        else:
            wave, model_identity = self._open_graph_wave(
                ledger=ledger,
                phase=phase,
                prompt_revision=prompt_revision,
                body=body,
                registrations=registrations,
            )
        requests = self._wave_requests(
            ledger=ledger,
            phase=phase,
            prompt_revision=prompt_revision,
            registrations=registrations,
            wave=wave,
            model_identity=model_identity,
        )
        if not matching_events:
            ledger.append(
                "wave_dispatch_prepared",
                phase=phase,
                prompt_revision=prompt_revision,
                body_sha256=body_sha256,
                request_contract=prompt_producer.DCM_REQUEST_CONTRACT,
                dcm_wave_id=wave["wave_id"],
                dcm_wave_fingerprint=wave["wave_fingerprint"],
                dcm_parent_wave_id=wave.get("parent_wave_id"),
                dcm_prompt_sha256=wave["prompt_sha256"],
                model_identity_receipt_sha256=model_identity[
                    "receipt_sha256"
                ],
                registrations={
                    seat.seat_id: {
                        "role_id": seat.role_id,
                        **registrations[seat.seat_id],
                    }
                    for seat in COUNCIL_SEATS
                },
            )
        if terminal_wave_events or dispatched_count == len(COUNCIL_SEATS):
            for request in requests.values():
                request["enqueued"] = False
        else:
            requests = self._enqueue(
                ledger=ledger,
                phase=phase,
                prompt_revision=prompt_revision,
                body=body,
                registrations=registrations,
                requests=requests,
            )
        wave_started = [
            event
            for event in ledger.events()
            if event.get("event_type") == "wave_started"
            and event.get("phase") == phase
            and int(event.get("prompt_revision") or 0) == prompt_revision
        ]
        if not wave_started:
            ledger.append(
                "wave_started",
                phase=phase,
                prompt_revision=prompt_revision,
                expected_seats=len(COUNCIL_SEATS),
            )
        for seat in COUNCIL_SEATS:
            request = requests[seat.seat_id]
            existing_seat_started = [
                event
                for event in ledger.events()
                if event.get("event_type") == "seat_started"
                and event.get("phase") == phase
                and int(event.get("prompt_revision") or 0) == prompt_revision
                and event.get("seat_id") == seat.seat_id
            ]
            if existing_seat_started:
                existing = existing_seat_started[-1]
                if (
                    existing.get("role_id") != seat.role_id
                    or existing.get("request_id") != request["request_id"]
                    or existing.get("message_id") != request["message_id"]
                    or existing.get("process_generation")
                    != request["expected_process_generation"]
                ):
                    raise CouncilTransportFailure(
                        f"durable {seat.seat_id} dispatch identity mismatch"
                    )
                continue
            ledger.append(
                "seat_started",
                phase=phase,
                prompt_revision=prompt_revision,
                seat_id=seat.seat_id,
                role_id=seat.role_id,
                request_id=request["request_id"],
                message_id=request["message_id"],
                dispatch_state=(
                    "enqueued" if request["enqueued"] else "already_enqueued"
                ),
                registration_observed=True,
                process_generation=(
                    registrations[seat.seat_id]["process_generation"]
                ),
                registration_ttl_seconds=(
                    registrations[seat.seat_id]["ttl_seconds"]
                ),
            )
        return requests

    def _record_contribution(
        self,
        ledger: RoundLedger,
        *,
        seat: CouncilSeat,
        phase: str,
        prompt_revision: int,
        expected_process_generation: str,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        contribution = outcome.get("contribution")
        if not isinstance(contribution, dict):
            raise CouncilTransportFailure(
                f"{seat.seat_id} durable outcome omitted contribution"
            )
        if (
            contribution.get("seat_id") != seat.seat_id
            or contribution.get("role_id") != seat.role_id
            or int(contribution.get("prompt_revision") or 0) != prompt_revision
            or outcome.get("process_generation")
            != expected_process_generation
        ):
            raise CouncilTransportFailure(
                f"{seat.seat_id} contribution identity/revision mismatch"
            )
        self._append_wave_event_once(
            ledger,
            "seat_status",
            identity={
                "phase": phase,
                "prompt_revision": prompt_revision,
                "seat_id": seat.seat_id,
            },
            role_id=seat.role_id,
            status="completed",
            process_generation=expected_process_generation,
            outcome_event_id=outcome.get("event_id"),
            proxy_turn_id=outcome.get("proxy_turn_id"),
        )
        self._append_wave_event_once(
            ledger,
            "evidence",
            identity={
                "phase": phase,
                "prompt_revision": prompt_revision,
                "seat_id": seat.seat_id,
            },
            role_id=seat.role_id,
            observations=contribution.get("observations") or [],
            unknowns=contribution.get("unknowns") or [],
            evidence_refs=contribution.get("evidence_refs") or [],
        )
        self._append_wave_event_once(
            ledger,
            "hypothesis",
            identity={
                "phase": phase,
                "prompt_revision": prompt_revision,
                "seat_id": seat.seat_id,
            },
            role_id=seat.role_id,
            inferences=contribution.get("inferences") or [],
            recommendation=contribution.get("recommendation") or "",
            confidence=contribution.get("confidence"),
        )
        if phase == "critique":
            concerns = contribution.get("concerns") or []
            unknowns = contribution.get("unknowns") or []
            self._append_wave_event_once(
                ledger,
                "dissent",
                identity={
                    "phase": phase,
                    "prompt_revision": prompt_revision,
                    "seat_id": seat.seat_id,
                },
                role_id=seat.role_id,
                present=bool(concerns or unknowns),
                concerns=concerns,
                unknowns=unknowns,
                recommendation=contribution.get("recommendation") or "",
            )
        self._append_wave_event_once(
            ledger,
            "contribution",
            identity={
                "phase": phase,
                "prompt_revision": prompt_revision,
                "seat_id": seat.seat_id,
            },
            role_id=seat.role_id,
            outcome_event_id=outcome.get("event_id"),
            contribution=contribution,
        )
        return contribution

    def _append_wave_event_once(
        self,
        ledger: RoundLedger,
        event_type: str,
        *,
        identity: dict[str, Any],
        **fields: Any,
    ) -> dict[str, Any]:
        matches = [
            event
            for event in ledger.events()
            if event.get("event_type") == event_type
            and all(event.get(key) == value for key, value in identity.items())
        ]
        if len(matches) > 1:
            raise CouncilTransportFailure(
                f"council wave has duplicate {event_type} identity {identity}"
            )
        expected = {**identity, **fields}
        if matches:
            existing = matches[0]
            mismatches = [
                key for key, value in expected.items() if existing.get(key) != value
            ]
            if mismatches:
                raise CouncilTransportFailure(
                    f"durable {event_type} changed fields {mismatches}"
                )
            return existing
        return ledger.append(event_type, **expected)

    def _close_graph_wave(
        self,
        requests: dict[str, dict[str, Any]],
        *,
        superseded_by_prompt_revision: int | None = None,
    ) -> dict[str, Any]:
        identities = {
            (request["dcm_session_id"], request["wave_id"])
            for request in requests.values()
        }
        if len(identities) != 1:
            raise CouncilTransportFailure(
                "council requests do not share one graph wave identity"
            )
        session_id, wave_id = identities.pop()
        adapter = self._dcm_adapter()
        closed = adapter.mesh.close_wave(
            session_id,
            wave_id,
            superseded_by_prompt_revision=superseded_by_prompt_revision,
        )
        expected_outcome = (
            "superseded_revision"
            if superseded_by_prompt_revision is not None
            else "complete"
        )
        verification = adapter.mesh.verify_wave_coordination(
            session_id,
            wave_id,
        )
        if (
            closed.get("status") != "closed"
            or closed.get("close_outcome") != expected_outcome
            or not verification.get("coordinated")
        ):
            raise CouncilTransportFailure(
                f"graph wave did not close as {expected_outcome} with verified coordination"
            )
        return closed

    def publish_graph_final(
        self,
        round_id: str,
        final: str,
    ) -> dict[str, Any]:
        adapter = self._dcm_adapter()
        session = adapter.mesh.read_session(round_id)
        if session.get("final") is None:
            adapter.mesh.publish_final(round_id, final)
            session = adapter.mesh.read_session(round_id)
        if session.get("status") != "closed" or session.get("final") != final:
            raise CouncilTransportFailure(
                "Main synthesis differs from the terminal DCM session"
            )
        return {
            "dcm_session_id": round_id,
            "dcm_graph_status": session["status"],
            "dcm_final_sha256": prompt_producer.text_sha256(final),
        }

    async def _wait_wave(
        self,
        ledger: RoundLedger,
        *,
        phase: str,
        prompt_revision: int,
        requests: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.wave_timeout
        pending = {seat.seat_id: seat for seat in COUNCIL_SEATS}
        contributions: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        superseded_revision: int | None = None
        stale_seats: set[str] = set()
        seat_order = {
            seat.seat_id: index for index, seat in enumerate(COUNCIL_SEATS)
        }
        events = ledger.events()

        def matching_events(event_type: str) -> list[dict[str, Any]]:
            return [
                event
                for event in events
                if event.get("event_type") == event_type
                and event.get("phase") == phase
                and int(event.get("prompt_revision") or 0) == prompt_revision
            ]

        contribution_events = matching_events("contribution")
        failure_events = matching_events("seat_failed")
        status_events = matching_events("seat_status")
        for seat in COUNCIL_SEATS:
            seat_contributions = [
                event
                for event in contribution_events
                if event.get("seat_id") == seat.seat_id
            ]
            seat_failures = [
                event
                for event in failure_events
                if event.get("seat_id") == seat.seat_id
            ]
            if len(seat_contributions) > 1 or len(seat_failures) > 1:
                raise CouncilTransportFailure(
                    f"durable {seat.seat_id} wave result is duplicated"
                )
            if seat_contributions and seat_failures:
                raise CouncilTransportFailure(
                    f"durable {seat.seat_id} wave result is contradictory"
                )
            if seat_contributions:
                event = seat_contributions[0]
                contribution = event.get("contribution")
                seat_statuses = [
                    status
                    for status in status_events
                    if status.get("seat_id") == seat.seat_id
                ]
                if (
                    event.get("role_id") != seat.role_id
                    or len(seat_statuses) != 1
                    or seat_statuses[0].get("role_id") != seat.role_id
                    or seat_statuses[0].get("status") != "completed"
                    or seat_statuses[0].get("process_generation")
                    != requests[seat.seat_id]["expected_process_generation"]
                    or not isinstance(contribution, dict)
                    or contribution.get("seat_id") != seat.seat_id
                    or contribution.get("role_id") != seat.role_id
                    or int(contribution.get("prompt_revision") or 0)
                    != prompt_revision
                ):
                    raise CouncilTransportFailure(
                        f"durable {seat.seat_id} contribution is invalid"
                    )
                contributions.append(contribution)
                pending.pop(seat.seat_id)
            elif seat_failures:
                event = seat_failures[0]
                if (
                    event.get("role_id") != seat.role_id
                    or event.get("process_generation")
                    != requests[seat.seat_id][
                        "expected_process_generation"
                    ]
                ):
                    raise CouncilTransportFailure(
                        f"durable {seat.seat_id} failure identity is invalid"
                    )
                failures.append(
                    {
                        "seat_id": seat.seat_id,
                        "role_id": seat.role_id,
                        "reason": str(event.get("reason") or "unknown failure"),
                    }
                )
                pending.pop(seat.seat_id)
                if event.get("inference_state") == "side_effect_uncertain":
                    raise CouncilTransportFailure(
                        f"{seat.seat_id} durable inference side effect is "
                        "uncertain; the council round cannot continue"
                    )
        contributions.sort(key=lambda value: seat_order[str(value["seat_id"])])
        failures.sort(key=lambda value: seat_order[str(value["seat_id"])])

        terminal_wave_events = [
            event
            for event_type in (
                "wave_completed",
                "superseded_wave_drained",
                "superseded_wave_drain_failed",
                "superseded_wave_not_dispatched",
            )
            for event in matching_events(event_type)
        ]
        if len(terminal_wave_events) > 1:
            raise CouncilTransportFailure(
                "council wave has multiple durable terminal states"
            )
        if terminal_wave_events:
            terminal_wave = terminal_wave_events[0]
            if (
                int(terminal_wave.get("contribution_count") or 0)
                != len(contributions)
                or int(terminal_wave.get("failure_count") or 0) != len(failures)
            ):
                raise CouncilTransportFailure(
                    "durable council wave terminal counts do not match results"
                )
            if terminal_wave["event_type"] == "superseded_wave_drain_failed":
                raise CouncilTransportFailure(
                    f"superseded {phase} wave revision {prompt_revision} "
                    "previously failed to drain"
                )
            self._close_graph_wave(
                requests,
                superseded_by_prompt_revision=(
                    int(terminal_wave["latest_prompt_revision"])
                    if terminal_wave["event_type"]
                    in {
                        "superseded_wave_drained",
                        "superseded_wave_not_dispatched",
                    }
                    else None
                ),
            )
            return {
                "superseded": terminal_wave["event_type"]
                in {
                    "superseded_wave_drained",
                    "superseded_wave_not_dispatched",
                },
                "contributions": contributions,
                "failures": failures,
            }

        wave_superseded_events = matching_events("wave_superseded")
        if len(wave_superseded_events) > 1:
            raise CouncilTransportFailure(
                "council wave has duplicate supersession state"
            )
        if wave_superseded_events:
            superseded_revision = int(
                wave_superseded_events[0].get("latest_prompt_revision") or 0
            )
        stale_seats = {
            str(event.get("seat_id"))
            for event in matching_events("contribution_stale")
        }

        def observe_supersession(latest_revision: int) -> None:
            nonlocal superseded_revision
            if latest_revision <= prompt_revision:
                return
            first_observation = superseded_revision is None
            superseded_revision = max(
                latest_revision,
                superseded_revision or latest_revision,
            )
            for contribution in contributions:
                seat_id = str(contribution["seat_id"])
                if seat_id in stale_seats:
                    continue
                self._append_wave_event_once(
                    ledger,
                    "contribution_stale",
                    identity={
                        "phase": phase,
                        "prompt_revision": prompt_revision,
                        "seat_id": seat_id,
                    },
                    role_id=contribution["role_id"],
                    latest_prompt_revision=superseded_revision,
                )
                stale_seats.add(seat_id)
            if first_observation:
                self._append_wave_event_once(
                    ledger,
                    "wave_superseded",
                    identity={
                        "phase": phase,
                        "prompt_revision": prompt_revision,
                    },
                    latest_prompt_revision=superseded_revision,
                    completed_seats=len(contributions),
                        pending_seats=sorted(pending),
                    )

        observe_supersession(ledger.latest_revision())
        while pending and time.monotonic() < deadline:
            observe_supersession(ledger.latest_revision())
            for seat_id, seat in list(pending.items()):
                request = requests[seat_id]
                outcome = self._matching_outcome(
                    seat,
                    request=request,
                )
                if outcome is None:
                    continue
                if outcome.get("ok") is not True:
                    pending.pop(seat_id)
                    failure = {
                        "seat_id": seat.seat_id,
                        "role_id": seat.role_id,
                        "reason": str(
                            outcome.get("error")
                            or "seat returned a failed durable outcome"
                        ),
                    }
                    failures.append(failure)
                    self._append_wave_event_once(
                        ledger,
                        "seat_failed",
                        identity={
                            "phase": phase,
                            "prompt_revision": prompt_revision,
                            "seat_id": seat.seat_id,
                        },
                        process_generation=request[
                            "expected_process_generation"
                        ],
                        outcome_kind=outcome.get("kind"),
                        inference_state=outcome.get("inference_state"),
                        **failure,
                    )
                    if (
                        outcome.get("inference_state")
                        == "side_effect_uncertain"
                    ):
                        raise CouncilTransportFailure(
                            f"{seat.seat_id} inference side effect is uncertain; "
                            "the council round cannot continue"
                        )
                    continue
                pending.pop(seat_id)
                try:
                    contribution = self._record_contribution(
                        ledger,
                        seat=seat,
                        phase=phase,
                        prompt_revision=prompt_revision,
                        expected_process_generation=request[
                            "expected_process_generation"
                        ],
                        outcome=outcome,
                    )
                except Exception as exc:
                    failure = {
                        "seat_id": seat.seat_id,
                        "role_id": seat.role_id,
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                    failures.append(failure)
                    self._append_wave_event_once(
                        ledger,
                        "seat_failed",
                        identity={
                            "phase": phase,
                            "prompt_revision": prompt_revision,
                            "seat_id": seat.seat_id,
                        },
                        process_generation=request[
                            "expected_process_generation"
                        ],
                        outcome_kind="contribution_validation_failed",
                        inference_state="completed_invalid",
                        **failure,
                    )
                    continue
                contributions.append(contribution)
                if superseded_revision is not None:
                    self._append_wave_event_once(
                        ledger,
                        "contribution_stale",
                        identity={
                            "phase": phase,
                            "prompt_revision": prompt_revision,
                            "seat_id": contribution["seat_id"],
                        },
                        role_id=contribution["role_id"],
                        latest_prompt_revision=superseded_revision,
                    )
                    stale_seats.add(str(contribution["seat_id"]))
            observe_supersession(ledger.latest_revision())
            if pending:
                await asyncio.sleep(self.poll_interval)
        for seat in pending.values():
            failure = {
                "seat_id": seat.seat_id,
                "role_id": seat.role_id,
                "reason": f"no durable {phase} outcome within "
                f"{self.wave_timeout:.1f}s",
            }
            failures.append(failure)
            self._append_wave_event_once(
                ledger,
                "seat_failed",
                identity={
                    "phase": phase,
                    "prompt_revision": prompt_revision,
                    "seat_id": seat.seat_id,
                },
                process_generation=requests[seat.seat_id][
                    "expected_process_generation"
                ],
                outcome_kind="outcome_timeout",
                inference_state="side_effect_uncertain",
                **failure,
            )
        contributions.sort(key=lambda value: seat_order[str(value["seat_id"])])
        failures.sort(key=lambda value: seat_order[str(value["seat_id"])])
        if superseded_revision is not None:
            if pending:
                self._append_wave_event_once(
                    ledger,
                    "superseded_wave_drain_failed",
                    identity={
                        "phase": phase,
                        "prompt_revision": prompt_revision,
                    },
                    latest_prompt_revision=superseded_revision,
                    contribution_count=len(contributions),
                    failure_count=len(failures),
                    missing_seats=sorted(pending),
                )
                raise CouncilTransportFailure(
                    f"superseded {phase} wave revision {prompt_revision} "
                    f"did not drain within {self.wave_timeout:.1f}s; "
                    f"pending seats: {', '.join(sorted(pending))}"
                )
            self._append_wave_event_once(
                ledger,
                "superseded_wave_drained",
                identity={
                    "phase": phase,
                    "prompt_revision": prompt_revision,
                },
                latest_prompt_revision=superseded_revision,
                contribution_count=len(contributions),
                failure_count=len(failures),
            )
            self._close_graph_wave(
                requests,
                superseded_by_prompt_revision=superseded_revision,
            )
            return {
                "superseded": True,
                "contributions": contributions,
                "failures": failures,
            }
        if pending:
            raise CouncilTransportFailure(
                f"{phase} wave timed out without durable outcomes; "
                "seat inference side effects are uncertain"
            )
        self._append_wave_event_once(
            ledger,
            "wave_completed",
            identity={
                "phase": phase,
                "prompt_revision": prompt_revision,
            },
            contribution_count=len(contributions),
            failure_count=len(failures),
            missing_seats=sorted(pending),
        )
        self._close_graph_wave(requests)
        return {
            "superseded": False,
            "contributions": contributions,
            "failures": failures,
        }

    async def _run_round(
        self,
        ledger: RoundLedger,
        *,
        synthesize: SynthesizeCallback,
        record_terminal: TerminalCallback,
    ) -> None:
        lease_descriptor = ledger.acquire_coordinator_lease()
        if lease_descriptor is None:
            return
        try:
            opened = ledger.opened_event()
            failure_revision = ledger.latest_revision()
            existing_terminal = ledger.terminal_event()
            if existing_terminal is not None:
                await record_terminal(
                    ledger.conversation_id,
                    ledger.round_id,
                    opened,
                    existing_terminal,
                )
                ledger.append(
                    "terminal_projected",
                    terminal_event_type=existing_terminal["event_type"],
                )
                return
            existing_events = ledger.events()
            recovery_revision = ledger.latest_revision()
            resolved_synthesis_revisions = {
                int(event.get("prompt_revision") or 0)
                for event in existing_events
                if event.get("event_type") in {"synthesis", "synthesis_stale"}
            }
            unresolved_synthesis_revisions = sorted(
                {
                    int(event.get("prompt_revision") or 0)
                    for event in existing_events
                    if event.get("event_type") == "synthesis_started"
                    and int(event.get("prompt_revision") or 0)
                    not in resolved_synthesis_revisions
                }
            )
            if unresolved_synthesis_revisions:
                failed_work_revision = unresolved_synthesis_revisions[0]
                terminal = ledger.append_failed_current(
                    failed_work_revision,
                    status="failed",
                    kind="main_synthesis_side_effect_uncertain",
                    unresolved_synthesis_revisions=(
                        unresolved_synthesis_revisions
                    ),
                    error=(
                        "coordinator ended after durable synthesis start "
                        "without a durable synthesis result or stale-result "
                        "receipt; immutable synthesis identity was not retried"
                    ),
                )
                await record_terminal(
                    ledger.conversation_id,
                    ledger.round_id,
                    opened,
                    terminal,
                )
                ledger.append(
                    "terminal_projected",
                    terminal_event_type=terminal["event_type"],
                )
                return
            uncertain_seat_revisions = sorted(
                {
                    int(event.get("prompt_revision") or 0)
                    for event in existing_events
                    if event.get("event_type") == "seat_failed"
                    and event.get("inference_state")
                    == "side_effect_uncertain"
                }
            )
            if uncertain_seat_revisions:
                failed_work_revision = uncertain_seat_revisions[0]
                terminal = ledger.append_failed_current(
                    failed_work_revision,
                    status="failed",
                    kind=(
                        "seat_inference_side_effect_uncertain"
                        if failed_work_revision == recovery_revision
                        else "seat_inference_side_effect_uncertain_"
                        "after_revision_change"
                    ),
                    uncertain_seat_revisions=uncertain_seat_revisions,
                    error=(
                        "a durable seat failure records an uncertain inference "
                        "side effect; the immutable council round was not "
                        "continued"
                    ),
                )
                await record_terminal(
                    ledger.conversation_id,
                    ledger.round_id,
                    opened,
                    terminal,
                )
                ledger.append(
                    "terminal_projected",
                    terminal_event_type=terminal["event_type"],
                )
                return
            prior_preparations = [
                event
                for event in existing_events
                if event.get("event_type") == "wave_dispatch_prepared"
                and int(event.get("prompt_revision") or 0)
                < recovery_revision
            ]
            prior_revisions = sorted(
                {
                    int(event.get("prompt_revision") or 0)
                    for event in prior_preparations
                }
            )
            for prior_revision in prior_revisions:
                failure_revision = prior_revision
                preparations = [
                    event
                    for event in prior_preparations
                    if int(event.get("prompt_revision") or 0)
                    == prior_revision
                ]
                prepared_phases = [str(event.get("phase") or "") for event in preparations]
                if (
                    len(prepared_phases) != len(set(prepared_phases))
                    or not set(prepared_phases) <= {"independent", "critique"}
                ):
                    raise CouncilTransportFailure(
                        f"revision {prior_revision} has invalid wave preparation state"
                    )
                terminal_phases = {
                    str(event.get("phase") or "")
                    for event in existing_events
                    if event.get("event_type")
                    in {
                        "wave_completed",
                        "superseded_wave_drained",
                        "superseded_wave_drain_failed",
                        "superseded_wave_not_dispatched",
                    }
                    and int(event.get("prompt_revision") or 0)
                    == prior_revision
                }
                if any(
                    event.get("event_type")
                    == "superseded_wave_drain_failed"
                    and int(event.get("prompt_revision") or 0)
                    == prior_revision
                    for event in existing_events
                ):
                    raise CouncilTransportFailure(
                        f"revision {prior_revision} contains a prior wave "
                        "that failed to drain"
                    )
                unresolved_phases = set(prepared_phases) - terminal_phases
                if not unresolved_phases:
                    continue
                if "independent" not in prepared_phases:
                    raise CouncilTransportFailure(
                        f"revision {prior_revision} has critique work without "
                        "a prepared independent wave"
                    )
                dispatched_phases: set[str] = set()
                for prior_phase in ("independent", "critique"):
                    if prior_phase not in unresolved_phases:
                        continue
                    dispatch_state = self._dispatch_state(
                        ledger=ledger,
                        phase=prior_phase,
                        prompt_revision=prior_revision,
                    )
                    dispatched_count = sum(dispatch_state)
                    if dispatched_count not in {0, len(COUNCIL_SEATS)}:
                        raise CouncilTransportFailure(
                            f"revision {prior_revision} {prior_phase} wave "
                            "has a partial dispatch identity"
                        )
                    started = [
                        event
                        for event in existing_events
                        if event.get("event_type") == "seat_started"
                        and event.get("phase") == prior_phase
                        and int(event.get("prompt_revision") or 0)
                        == prior_revision
                    ]
                    if dispatched_count == 0:
                        if started:
                            raise CouncilTransportFailure(
                                f"revision {prior_revision} {prior_phase} wave "
                                "lost its dispatch tokens after durable start"
                            )
                        self._append_wave_event_once(
                            ledger,
                            "superseded_wave_not_dispatched",
                            identity={
                                "phase": prior_phase,
                                "prompt_revision": prior_revision,
                            },
                            latest_prompt_revision=recovery_revision,
                            contribution_count=0,
                            failure_count=0,
                            missing_seats=[],
                            reason="prepared_before_supersession_without_dispatch",
                        )
                        continue
                    dispatched_phases.add(prior_phase)
                if not dispatched_phases:
                    continue
                prior_prompt = ledger.prompt_for_revision(prior_revision)
                independent_requests = await self._dispatch_wave(
                    ledger,
                    phase="independent",
                    prompt=prior_prompt,
                    prompt_revision=prior_revision,
                )
                independent = await self._wait_wave(
                    ledger,
                    phase="independent",
                    prompt_revision=prior_revision,
                    requests=independent_requests,
                )
                if "critique" in dispatched_phases:
                    critique_requests = await self._dispatch_wave(
                        ledger,
                        phase="critique",
                        prompt=prior_prompt,
                        prompt_revision=prior_revision,
                        revealed=independent["contributions"],
                    )
                    await self._wait_wave(
                        ledger,
                        phase="critique",
                        prompt_revision=prior_revision,
                        requests=critique_requests,
                    )
            failure_revision = recovery_revision
            recovered_synthesis = next(
                (
                    event
                    for event in reversed(existing_events)
                    if event.get("event_type") == "synthesis"
                    and int(event.get("prompt_revision") or 0)
                    == recovery_revision
                ),
                None,
            )
            if recovered_synthesis is not None:
                try:
                    terminal = ledger.append_completed(
                        recovery_revision,
                        prompt_revision=recovery_revision,
                        status="completed",
                        answer=recovered_synthesis["answer"],
                        independent_count=int(
                            recovered_synthesis.get("independent_count") or 0
                        ),
                        critique_count=int(
                            recovered_synthesis.get("critique_count") or 0
                        ),
                        synthesis_receipt=(
                            recovered_synthesis.get("synthesis_receipt") or {}
                        ),
                        failed_seats=(
                            recovered_synthesis.get("failed_seats") or []
                        ),
                        recovered_without_inference=True,
                    )
                except CouncilRevisionSuperseded:
                    terminal = None
                if terminal is not None:
                    await record_terminal(
                        ledger.conversation_id,
                        ledger.round_id,
                        opened,
                        terminal,
                    )
                    ledger.append(
                        "terminal_projected",
                        terminal_event_type=terminal["event_type"],
                    )
                    return
            while True:
                prompt_revision = ledger.latest_revision()
                failure_revision = prompt_revision
                prompt = ledger.prompt_for_revision(prompt_revision)
                independent_requests = await self._dispatch_wave(
                    ledger,
                    phase="independent",
                    prompt=prompt,
                    prompt_revision=prompt_revision,
                )
                independent = await self._wait_wave(
                    ledger,
                    phase="independent",
                    prompt_revision=prompt_revision,
                    requests=independent_requests,
                )
                if independent["superseded"]:
                    continue
                if not independent["contributions"]:
                    raise CouncilTransportFailure(
                        "independent wave produced no durable council contributions"
                    )
                self._append_wave_event_once(
                    ledger,
                    "reveal",
                    identity={
                        "phase": "reveal",
                        "prompt_revision": prompt_revision,
                    },
                    contribution_count=len(independent["contributions"]),
                    failure_count=len(independent["failures"]),
                    contributions=independent["contributions"],
                    failures=independent["failures"],
                )
                critique_requests = await self._dispatch_wave(
                    ledger,
                    phase="critique",
                    prompt=prompt,
                    prompt_revision=prompt_revision,
                    revealed=independent["contributions"],
                )
                critique = await self._wait_wave(
                    ledger,
                    phase="critique",
                    prompt_revision=prompt_revision,
                    requests=critique_requests,
                )
                if critique["superseded"]:
                    continue
                if not critique["contributions"]:
                    raise CouncilTransportFailure(
                        "critique wave produced no durable council contributions"
                    )
                latest_revision = ledger.latest_revision()
                if latest_revision > prompt_revision:
                    ledger.append(
                        "synthesis_deferred",
                        prompt_revision=prompt_revision,
                        latest_prompt_revision=latest_revision,
                        reason="user_amendment",
                    )
                    continue
                packet = {
                    "council_protocol": "taey-native-dcm/v2",
                    "conversation_id": ledger.conversation_id,
                    "round_id": ledger.round_id,
                    "prompt_revision": prompt_revision,
                    "user_request": prompt,
                    "independent_contributions": independent["contributions"],
                    "independent_failures": independent["failures"],
                    "critiques": critique["contributions"],
                    "critique_failures": critique["failures"],
                    "requirements": {
                        "only_ui_voice": "Main Taey",
                        "label_missing_seats": True,
                        "surface_dissent_and_uncertainty": True,
                        "hidden_chain_of_thought": False,
                    },
                }
                ledger.append(
                    "synthesis_started",
                    prompt_revision=prompt_revision,
                    independent_count=len(independent["contributions"]),
                    critique_count=len(critique["contributions"]),
                    failed_seats=independent["failures"]
                    + critique["failures"],
                )
                synthesis_result = await synthesize(
                    ledger.conversation_id,
                    packet,
                )
                answer = synthesis_result.get("answer")
                if not isinstance(answer, str) or not answer.strip():
                    raise CouncilTransportFailure(
                        "Main Taey synthesis returned no answer"
                    )
                synthesis_receipt = {
                    key: value
                    for key, value in synthesis_result.items()
                    if key != "answer"
                }
                latest_revision = ledger.latest_revision()
                if latest_revision > prompt_revision:
                    ledger.append(
                        "synthesis_stale",
                        prompt_revision=prompt_revision,
                        latest_prompt_revision=latest_revision,
                        reason="user_amendment",
                    )
                    continue
                ledger.append(
                    "synthesis",
                    prompt_revision=prompt_revision,
                    answer=answer,
                    synthesis_receipt=synthesis_receipt,
                    independent_count=len(independent["contributions"]),
                    critique_count=len(critique["contributions"]),
                    failed_seats=independent["failures"]
                    + critique["failures"],
                    dissent_count=sum(
                        1
                        for contribution in critique["contributions"]
                        if contribution.get("concerns")
                        or contribution.get("unknowns")
                    ),
                )
                try:
                    terminal = ledger.append_completed(
                        prompt_revision,
                        prompt_revision=prompt_revision,
                        status="completed",
                        answer=answer,
                        independent_count=len(independent["contributions"]),
                        critique_count=len(critique["contributions"]),
                        synthesis_receipt=synthesis_receipt,
                        failed_seats=independent["failures"]
                        + critique["failures"],
                    )
                except CouncilRevisionSuperseded as exc:
                    ledger.append(
                        "synthesis_stale",
                        prompt_revision=exc.expected_revision,
                        latest_prompt_revision=exc.latest_revision,
                        reason="user_amendment_at_completion",
                    )
                    continue
                await record_terminal(
                    ledger.conversation_id,
                    ledger.round_id,
                    opened,
                    terminal,
                )
                ledger.append(
                    "terminal_projected",
                    terminal_event_type=terminal["event_type"],
                )
                return
        except Exception as exc:
            terminal = ledger.terminal_event()
            if terminal is not None:
                ledger.append(
                    "terminal_projection_failed",
                    terminal_event_type=terminal["event_type"],
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                events = ledger.events()
                synthesis_started = any(
                    event.get("event_type") == "synthesis_started"
                    and int(event.get("prompt_revision") or 0)
                    == failure_revision
                    for event in events
                )
                synthesis_durable = any(
                    event.get("event_type") == "synthesis"
                    and int(event.get("prompt_revision") or 0)
                    == failure_revision
                    for event in events
                )
                seat_inference_uncertain = any(
                    event.get("event_type") == "seat_failed"
                    and int(event.get("prompt_revision") or 0)
                    == failure_revision
                    and event.get("inference_state")
                    == "side_effect_uncertain"
                    for event in events
                )
                kind = (
                    "main_synthesis_side_effect_uncertain"
                    if synthesis_started and not synthesis_durable
                    else "seat_inference_side_effect_uncertain"
                    if seat_inference_uncertain
                    else "coordinator_failure"
                )
                try:
                    terminal = ledger.append_failed(
                        failure_revision,
                        prompt_revision=failure_revision,
                        status="failed",
                        kind=kind,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                except CouncilRevisionSuperseded:
                    drain_failed = any(
                        event.get("event_type")
                        == "superseded_wave_drain_failed"
                        and int(event.get("prompt_revision") or 0)
                        == failure_revision
                        for event in events
                    )
                    terminal = ledger.append_failed_current(
                        failure_revision,
                        status="failed",
                        kind=(
                            "superseded_wave_drain_failed"
                            if drain_failed
                            else f"{kind}_after_revision_change"
                        ),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                try:
                    await record_terminal(
                        ledger.conversation_id,
                        ledger.round_id,
                        opened,
                        terminal,
                    )
                    ledger.append(
                        "terminal_projected",
                        terminal_event_type=terminal["event_type"],
                    )
                except Exception as projection_exc:
                    ledger.append(
                        "terminal_projection_failed",
                        terminal_event_type=terminal["event_type"],
                        error=(
                            f"{type(projection_exc).__name__}: "
                            f"{projection_exc}"
                        ),
                    )
        finally:
            try:
                ledger.release_coordinator_lease(lease_descriptor)
            finally:
                if ledger.has_event("terminal_projected"):
                    self.redis.eval(
                        _CLEAR_ACTIVE_LUA,
                        1,
                        self._active_key(ledger.conversation_id),
                        ledger.round_id,
                    )
                    self.redis.expire(
                        self._dispatch_key(ledger.round_id),
                        30 * 24 * 60 * 60,
                    )
