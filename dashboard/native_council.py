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
if redis.call('SADD', KEYS[1], ARGV[1]) == 0 then
    return 0
end
redis.call('LPUSH', KEYS[2], ARGV[2])
return 1
"""
_CLEAR_ACTIVE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
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
            council_protocol="taey-native-dcm/v1",
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

    def _seat_log(self, seat: CouncilSeat) -> Path:
        return self.council_log_dir / f"{seat.seat_id}.jsonl"

    def _matching_outcome(
        self,
        seat: CouncilSeat,
        *,
        request_id: str,
        round_id: str,
        prompt_revision: int,
    ) -> dict[str, Any] | None:
        matching: dict[str, Any] | None = None
        for event in _read_jsonl(self._seat_log(seat)):
            if (
                event.get("event_type") == "turn_outcome"
                and str(event.get("request_id") or "") == request_id
                and str(event.get("round_id") or "") == round_id
                and int(event.get("prompt_revision") or 0) == prompt_revision
            ):
                matching = event
        return matching

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
            "council_protocol": "taey-native-dcm/v1",
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

    def _enqueue(
        self,
        *,
        ledger: RoundLedger,
        seat: CouncilSeat,
        phase: str,
        prompt_revision: int,
        body: str,
    ) -> dict[str, Any]:
        message_id = self._message_id(
            ledger.round_id,
            prompt_revision,
            phase,
            seat,
        )
        request_id = self._request_id(
            ledger.round_id,
            prompt_revision,
            phase,
            seat,
        )
        payload = {
            "from": "taey",
            "type": "council_request",
            "body": body,
            "timestamp": time.time(),
            "priority": "high",
            "msg_id": message_id,
            "event_id": request_id,
            "correlation_id": ledger.round_id,
            "request_id": request_id,
            "council_run_id": ledger.round_id,
            "round_id": ledger.round_id,
            "prompt_revision": prompt_revision,
            "round_phase": phase,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        token = f"{prompt_revision}:{phase}:{seat.seat_id}"
        enqueued = int(
            self.redis.eval(
                _ENQUEUE_LUA,
                2,
                self._dispatch_key(ledger.round_id),
                f"{self.key_prefix}:{seat.seat_id}:inbox",
                token,
                encoded,
            )
        )
        if enqueued:
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
                        "msg_id": message_id,
                    },
                    maxlen=50000,
                    approximate=True,
                )
            except Exception as exc:
                ledger.append(
                    "dispatch_trace_failed",
                    seat_id=seat.seat_id,
                    role_id=seat.role_id,
                    message_id=message_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return {
            "seat_id": seat.seat_id,
            "role_id": seat.role_id,
            "message_id": message_id,
            "request_id": request_id,
            "enqueued": bool(enqueued),
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
        ledger.append(
            "wave_started",
            phase=phase,
            prompt_revision=prompt_revision,
            expected_seats=len(COUNCIL_SEATS),
        )
        requests: dict[str, dict[str, Any]] = {}
        for seat in COUNCIL_SEATS:
            registration = self.redis.get(
                f"{self.key_prefix}:{seat.seat_id}:seat_registration"
            )
            request = self._enqueue(
                ledger=ledger,
                seat=seat,
                phase=phase,
                prompt_revision=prompt_revision,
                body=body,
            )
            requests[seat.seat_id] = request
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
                registration_observed=registration is not None,
            )
        return requests

    def _record_contribution(
        self,
        ledger: RoundLedger,
        *,
        seat: CouncilSeat,
        phase: str,
        prompt_revision: int,
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
        ):
            raise CouncilTransportFailure(
                f"{seat.seat_id} contribution identity/revision mismatch"
            )
        ledger.append(
            "seat_status",
            phase=phase,
            prompt_revision=prompt_revision,
            seat_id=seat.seat_id,
            role_id=seat.role_id,
            status="completed",
            outcome_event_id=outcome.get("event_id"),
            proxy_turn_id=outcome.get("proxy_turn_id"),
        )
        ledger.append(
            "evidence",
            phase=phase,
            prompt_revision=prompt_revision,
            seat_id=seat.seat_id,
            role_id=seat.role_id,
            observations=contribution.get("observations") or [],
            unknowns=contribution.get("unknowns") or [],
            evidence_refs=contribution.get("evidence_refs") or [],
        )
        ledger.append(
            "hypothesis",
            phase=phase,
            prompt_revision=prompt_revision,
            seat_id=seat.seat_id,
            role_id=seat.role_id,
            inferences=contribution.get("inferences") or [],
            recommendation=contribution.get("recommendation") or "",
            confidence=contribution.get("confidence"),
        )
        ledger.append(
            "contribution",
            phase=phase,
            prompt_revision=prompt_revision,
            seat_id=seat.seat_id,
            role_id=seat.role_id,
            outcome_event_id=outcome.get("event_id"),
            contribution=contribution,
        )
        if phase == "critique":
            concerns = contribution.get("concerns") or []
            unknowns = contribution.get("unknowns") or []
            ledger.append(
                "dissent",
                phase=phase,
                prompt_revision=prompt_revision,
                seat_id=seat.seat_id,
                role_id=seat.role_id,
                present=bool(concerns or unknowns),
                concerns=concerns,
                unknowns=unknowns,
                recommendation=contribution.get("recommendation") or "",
            )
        return contribution

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
        retry_failures: dict[str, dict[str, Any]] = {}
        observed_failed_outcomes: dict[str, tuple[Any, str]] = {}
        superseded_revision: int | None = None
        stale_seats: set[str] = set()

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
                ledger.append(
                    "contribution_stale",
                    phase=phase,
                    prompt_revision=prompt_revision,
                    latest_prompt_revision=superseded_revision,
                    seat_id=seat_id,
                    role_id=contribution["role_id"],
                )
                stale_seats.add(seat_id)
            if first_observation:
                ledger.append(
                    "wave_superseded",
                    phase=phase,
                    prompt_revision=prompt_revision,
                    latest_prompt_revision=superseded_revision,
                    completed_seats=len(contributions),
                    pending_seats=sorted(pending),
                )

        while pending and time.monotonic() < deadline:
            observe_supersession(ledger.latest_revision())
            for seat_id, seat in list(pending.items()):
                request = requests[seat_id]
                outcome = self._matching_outcome(
                    seat,
                    request_id=request["request_id"],
                    round_id=ledger.round_id,
                    prompt_revision=prompt_revision,
                )
                if outcome is None:
                    continue
                if outcome.get("ok") is not True:
                    failure = {
                        "seat_id": seat.seat_id,
                        "role_id": seat.role_id,
                        "reason": str(
                            outcome.get("error")
                            or "seat returned a failed durable outcome"
                        ),
                    }
                    retry_failures[seat_id] = failure
                    marker = (
                        outcome.get("recorded_at") or outcome.get("ts"),
                        failure["reason"],
                    )
                    if observed_failed_outcomes.get(seat_id) != marker:
                        observed_failed_outcomes[seat_id] = marker
                        ledger.append(
                            "seat_status",
                            phase=phase,
                            prompt_revision=prompt_revision,
                            seat_id=seat.seat_id,
                            role_id=seat.role_id,
                            status="retrying",
                            outcome_event_id=outcome.get("event_id"),
                            error=failure["reason"],
                        )
                    continue
                pending.pop(seat_id)
                try:
                    contribution = self._record_contribution(
                        ledger,
                        seat=seat,
                        phase=phase,
                        prompt_revision=prompt_revision,
                        outcome=outcome,
                    )
                except Exception as exc:
                    failure = {
                        "seat_id": seat.seat_id,
                        "role_id": seat.role_id,
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                    failures.append(failure)
                    ledger.append(
                        "seat_failed",
                        phase=phase,
                        prompt_revision=prompt_revision,
                        **failure,
                    )
                    continue
                contributions.append(contribution)
                if superseded_revision is not None:
                    ledger.append(
                        "contribution_stale",
                        phase=phase,
                        prompt_revision=prompt_revision,
                        latest_prompt_revision=superseded_revision,
                        seat_id=contribution["seat_id"],
                        role_id=contribution["role_id"],
                    )
                    stale_seats.add(str(contribution["seat_id"]))
            observe_supersession(ledger.latest_revision())
            if pending:
                await asyncio.sleep(self.poll_interval)
        for seat in pending.values():
            prior_failure = retry_failures.get(seat.seat_id)
            failure = {
                "seat_id": seat.seat_id,
                "role_id": seat.role_id,
                "reason": (
                    f"{prior_failure['reason']}; retry deadline exceeded "
                    f"after {self.wave_timeout:.1f}s"
                    if prior_failure
                    else f"no durable {phase} outcome within "
                    f"{self.wave_timeout:.1f}s"
                ),
            }
            failures.append(failure)
            ledger.append(
                "seat_failed",
                phase=phase,
                prompt_revision=prompt_revision,
                **failure,
            )
        if superseded_revision is not None:
            if pending:
                ledger.append(
                    "superseded_wave_drain_failed",
                    phase=phase,
                    prompt_revision=prompt_revision,
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
            ledger.append(
                "superseded_wave_drained",
                phase=phase,
                prompt_revision=prompt_revision,
                latest_prompt_revision=superseded_revision,
                contribution_count=len(contributions),
                failure_count=len(failures),
            )
            return {
                "superseded": True,
                "contributions": contributions,
                "failures": failures,
            }
        ledger.append(
            "wave_completed",
            phase=phase,
            prompt_revision=prompt_revision,
            contribution_count=len(contributions),
            failure_count=len(failures),
            missing_seats=sorted(pending),
        )
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
            while True:
                prompt_revision = ledger.latest_revision()
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
                ledger.append(
                    "reveal",
                    phase="reveal",
                    prompt_revision=prompt_revision,
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
                    "council_protocol": "taey-native-dcm/v1",
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
                terminal = ledger.append(
                    "round_failed",
                    prompt_revision=ledger.latest_revision(),
                    status="failed",
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
