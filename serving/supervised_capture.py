#!/usr/bin/env python3
"""Fail-closed capture for real non-UI Taey tool trajectories."""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable


SCHEMA_VERSION = "taey.supervised-capture.v1"
STATE_PRECONDITION_VERSION = "taey.state-preconditions.v1"
EVENT_TYPES = frozenset(
    {
        "request",
        "model_request",
        "model_decision",
        "tool_call",
        "approval_required",
        "supervisor_approval",
        "approval_consumed",
        "execution_preconditions_checked",
        "tool_result",
        "turn_complete",
        "turn_failed",
        "validation_receipt",
        "control_receipt",
    }
)
CONTRACT_REF = (
    "palios-taey/palios-training@"
    "58b108042e66fa508765a6277c033cc5a8f86abd"
)
TRACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+-]{0,255}$")
PUBLIC_REPOSITORIES = frozenset(
    {
        "palios-taey/claude-code-fleet-orchestrator",
        "palios-taey/palios-training",
        "palios-taey/taey-presence",
    }
)
APPROVED_PROGRAMS = frozenset(
    {"git", "gh", "taey-notify", "taey-plan", "taey-task"}
)
APPROVED_GIT_SUBCOMMANDS = frozenset(
    {"add", "commit", "fetch", "merge", "push", "switch"}
)
MUTATION_EXECUTION_CLASSES = {
    "git add": "refusal",
    "git commit": "refusal",
    "git fetch": "refusal",
    "git merge": "refusal",
    "git push": "refusal",
    "git switch": "refusal",
    "gh pr create": "refusal",
    "gh pr comment": "refusal",
    "gh pr review": "refusal",
    "gh pr merge": "refusal",
    "taey-notify send": "refusal",
    "taey-plan assign": "refusal",
    "taey-plan ingest": "refusal",
    "taey-plan next": "refusal",
    "taey-task create": "refusal",
    "taey-task outcome": "refusal",
    "taey-task update": "refusal",
}
MUTATION_REFUSAL_REASON = (
    "state change refused: this operation has neither an atomic expected-state "
    "contract nor a trace-owned execution domain"
)
APPROVED_NOTIFICATION_TYPES = frozenset(
    {
        "command",
        "defect",
        "directive",
        "escalation",
        "message",
        "notification",
        "response_ready",
        "result",
        "status",
        "task",
    }
)
SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,158}$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


SUPERVISED_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "inspect_git",
            "description": (
                "Inspect a committed public Git repository without changing it. "
                "Choose the operation and evidence needed; the executor constructs "
                "a non-mutating git argv and returns exact stdout, stderr, and status. "
                "Topology never fetches; propose one approval-gated git fetch first "
                "when current remote refs are required."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Absolute path to a permitted public repository checkout.",
                    },
                    "operation": {
                        "type": "string",
                        "enum": [
                            "status",
                            "log",
                            "show",
                            "blame",
                            "diff",
                            "topology",
                            "worktree",
                            "branch",
                        ],
                    },
                    "ref": {"type": "string"},
                    "base": {"type": "string"},
                    "head": {"type": "string"},
                    "path": {"type": "string"},
                    "max_count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 20,
                    },
                },
                "required": ["repo", "operation"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_orchestration",
            "description": (
                "Read the public fleet-orchestrator CLI state without dispatching, "
                "notifying, claiming, closing, or otherwise changing a task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "task_status",
                            "task_list",
                            "plan_current",
                            "plan_list",
                            "plan_show",
                        ],
                    },
                    "identifier": {"type": "string"},
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_state_preconditions",
            "description": (
                "Read the exact command-specific mutable state for one proposed "
                "Git, GitHub, or orchestration mutation without executing it. Copy "
                "the returned preconditions object unchanged into the immediately "
                "following run_approved_state_change proposal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 80,
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Absolute working directory for this one argv.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 900,
                        "default": 120,
                    },
                },
                "required": ["argv", "cwd"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_approved_state_change",
            "description": (
                "Request one narrowly contracted state-changing Git/GitHub/orchestration "
                "argv. The current execution policy durably refuses every supported "
                "mutation because none yet has both command-level expected-state "
                "enforcement and a trace-owned execution domain. The refusal records "
                "the exact operation, class, reason, arguments, and inspected "
                "preconditions without creating or consuming approval authority."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 80,
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Absolute working directory for this one argv.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 900,
                        "default": 120,
                    },
                    "preconditions": {
                        "type": "object",
                        "description": (
                            "Exact object returned by inspect_state_preconditions "
                            "for the same argv, cwd, and timeout_seconds."
                        ),
                    },
                },
                "required": ["argv", "cwd", "preconditions"],
                "additionalProperties": False,
            },
        },
    },
]


class CaptureError(RuntimeError):
    pass


class StatePreconditionMismatch(CaptureError):
    def __init__(self, expected: dict[str, Any], observed: dict[str, Any]):
        super().__init__("approved mutable state changed before execution")
        self.expected = expected
        self.observed = observed


@dataclass(frozen=True)
class ApprovedCommand:
    proposal_argv: tuple[str, ...]
    execution_argv: tuple[str, ...]
    cwd: str
    public_repo: str
    program: str
    operation: str
    timeout_seconds: int
    preconditions: dict[str, Any] | None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def _validate_trace_id(trace_id: str) -> str:
    if not TRACE_ID_RE.fullmatch(trace_id or ""):
        raise CaptureError("trace_id must match the public supervised-capture contract")
    return trace_id


def _payload_bytes(event: dict[str, Any], sequence: int) -> bytes:
    encoded = event.get("payload_b64")
    if not isinstance(encoded, str):
        raise CaptureError(f"sequence {sequence}: payload_b64 must be a string")
    try:
        payload = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise CaptureError(f"sequence {sequence}: payload_b64 is invalid") from exc
    if _sha256(payload) != event.get("payload_sha256"):
        raise CaptureError(f"sequence {sequence}: payload hash mismatch")
    return payload


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise CaptureError("short write to supervised capture artifact")
        remaining = remaining[written:]


def _ensure_private_dir(path: pathlib.Path, *, create: bool) -> None:
    if path.is_symlink():
        raise CaptureError(f"private capture directory cannot be a symlink: {path}")
    if not path.exists():
        if not create:
            raise CaptureError(f"private capture directory does not exist: {path}")
        path.mkdir(parents=True, mode=0o700)
    if not path.is_dir():
        raise CaptureError(f"private capture path is not a directory: {path}")
    mode = path.stat().st_mode & 0o777
    if mode != 0o700:
        raise CaptureError(f"private capture directory must be mode 0700, got {mode:04o}: {path}")


def _open_ledger(path: pathlib.Path, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CaptureError(f"cannot open append-only ledger {path}: {exc}") from exc
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(fd)
        raise CaptureError(f"private capture ledger must be a regular file: {path}")
    if file_stat.st_nlink != 1:
        os.close(fd)
        raise CaptureError(f"private capture ledger cannot have hard links: {path}")
    mode = file_stat.st_mode & 0o777
    if mode != 0o600:
        os.close(fd)
        raise CaptureError(f"private capture ledger must be mode 0600, got {mode:04o}: {path}")
    return fd


def _decode_lines(raw: bytes, path: pathlib.Path) -> list[dict[str, Any]]:
    if raw and not raw.endswith(b"\n"):
        raise CaptureError(f"append-only ledger has a truncated final event: {path}")
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line:
            raise CaptureError(f"blank event at {path}:{line_number}")
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaptureError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
        if not isinstance(event, dict):
            raise CaptureError(f"event must be an object at {path}:{line_number}")
        events.append(event)
    return events


def _event_digest(event: dict[str, Any]) -> str:
    unsigned = dict(event)
    unsigned.pop("event_sha256", None)
    return _sha256(_canonical(unsigned))


def _verify_chain(events: Iterable[dict[str, Any]], *, trace_id: str) -> list[dict[str, Any]]:
    checked: list[dict[str, Any]] = []
    previous = ""
    request_sha = ""
    for expected_sequence, event in enumerate(events, 1):
        if event.get("schema_version") != SCHEMA_VERSION:
            raise CaptureError(f"sequence {expected_sequence}: schema_version mismatch")
        if event.get("trace_id") != trace_id:
            raise CaptureError(f"sequence {expected_sequence}: trace_id mismatch")
        if event.get("sequence") != expected_sequence:
            raise CaptureError(f"sequence {expected_sequence}: missing, duplicated, or reordered event")
        if event.get("previous_event_sha256", "") != previous:
            raise CaptureError(f"sequence {expected_sequence}: previous-event hash mismatch")
        payload = _payload_bytes(event, expected_sequence)
        if _event_digest(event) != event.get("event_sha256"):
            raise CaptureError(f"sequence {expected_sequence}: event hash mismatch")
        if expected_sequence == 1:
            if event.get("event_type") != "request":
                raise CaptureError("the first event must be the exact request")
            metadata = event.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("contract_ref") != CONTRACT_REF:
                raise CaptureError("the request is not bound to the pinned capture contract")
            request_sha = str(event.get("request_sha256") or "")
            if _sha256(payload) != request_sha:
                raise CaptureError("the request hash does not match the exact request payload")
        if not request_sha or event.get("request_sha256") != request_sha:
            raise CaptureError(f"sequence {expected_sequence}: request hash mismatch")
        previous = str(event["event_sha256"])
        checked.append(event)
    return checked


class TraceLedger:
    def __init__(self, root: str | os.PathLike[str], trace_id: str):
        self.root = pathlib.Path(root).expanduser().absolute()
        self.trace_id = _validate_trace_id(trace_id)
        self.trace_dir = self.root / self.trace_id
        self.path = self.trace_dir / "events.jsonl"

    @classmethod
    def create(cls, root: str | os.PathLike[str], trace_id: str) -> "TraceLedger":
        ledger = cls(root, trace_id)
        _ensure_private_dir(ledger.root, create=True)
        try:
            ledger.trace_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise CaptureError(
                f"trace already exists; history reconstruction and overwrite are refused: {trace_id}"
            ) from exc
        _ensure_private_dir(ledger.trace_dir, create=False)
        fd = _open_ledger(ledger.path, create=True)
        os.close(fd)
        return ledger

    @classmethod
    def open(cls, root: str | os.PathLike[str], trace_id: str) -> "TraceLedger":
        ledger = cls(root, trace_id)
        _ensure_private_dir(ledger.root, create=False)
        _ensure_private_dir(ledger.trace_dir, create=False)
        if not ledger.path.exists():
            raise CaptureError(f"trace ledger does not exist: {trace_id}")
        return ledger

    @staticmethod
    def _read_locked(fd: int, path: pathlib.Path) -> list[dict[str, Any]]:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return _decode_lines(b"".join(chunks), path)

    def read(self) -> list[dict[str, Any]]:
        fd = _open_ledger(self.path, create=False)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            events = self._read_locked(fd, self.path)
            return _verify_chain(events, trace_id=self.trace_id)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def append(
        self,
        *,
        event_type: str,
        actor: str,
        operation_class: str,
        source_ref: str,
        request_sha256: str,
        payload: bytes = b"",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fd = _open_ledger(self.path, create=False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            events = _verify_chain(
                self._read_locked(fd, self.path),
                trace_id=self.trace_id,
            )
            return self._append_locked(
                fd,
                events,
                event_type=event_type,
                actor=actor,
                operation_class=operation_class,
                source_ref=source_ref,
                request_sha256=request_sha256,
                payload=payload,
                metadata=metadata,
            )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _append_locked(
        self,
        fd: int,
        events: list[dict[str, Any]],
        *,
        event_type: str,
        actor: str,
        operation_class: str,
        source_ref: str,
        request_sha256: str,
        payload: bytes = b"",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if events and events[0].get("request_sha256") != request_sha256:
            raise CaptureError("append request hash does not match the trace")
        if not events and event_type != "request":
            raise CaptureError("the first event must be the exact request")
        event = {
            "schema_version": SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "sequence": len(events) + 1,
            "recorded_at": _utc_now(),
            "request_sha256": request_sha256,
            "actor": actor,
            "source_ref": source_ref,
            "event_type": event_type,
            "operation_class": operation_class,
            "payload_sha256": _sha256(payload),
            "payload_b64": _b64(payload),
            "metadata": metadata or {},
            "previous_event_sha256": (
                str(events[-1]["event_sha256"]) if events else ""
            ),
        }
        event["event_sha256"] = _event_digest(event)
        os.lseek(fd, 0, os.SEEK_END)
        _write_all(fd, _canonical(event) + b"\n")
        os.fsync(fd)
        return event

    def append_approval(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments_sha256: str,
        preconditions_sha256: str,
        source_ref: str,
    ) -> dict[str, Any]:
        fd = _open_ledger(self.path, create=False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            events = _verify_chain(
                self._read_locked(fd, self.path),
                trace_id=self.trace_id,
            )
            required = [
                event
                for event in events
                if event.get("event_type") == "approval_required"
                and event.get("metadata", {}).get("call_id") == call_id
                and event.get("metadata", {}).get("tool_name") == tool_name
                and event.get("metadata", {}).get("arguments_sha256")
                == arguments_sha256
                and event.get("metadata", {}).get("preconditions_sha256")
                == preconditions_sha256
            ]
            if len(required) != 1:
                raise CaptureError("approval must match exactly one recorded pending call")
            if any(
                event.get("event_type") in {"supervisor_approval", "tool_result"}
                and event.get("metadata", {}).get("call_id") == call_id
                for event in events
            ):
                raise CaptureError("the call is already approved or resolved")
            approval_id = uuid.uuid4().hex
            preconditions = required[0].get("metadata", {}).get("preconditions")
            if (
                not isinstance(preconditions, dict)
                or _preconditions_sha256(preconditions) != preconditions_sha256
            ):
                raise CaptureError("approval preconditions do not match their exact hash")
            metadata = {
                "approval_id": approval_id,
                "call_id": call_id,
                "tool_name": tool_name,
                "arguments_sha256": arguments_sha256,
                "preconditions": preconditions,
                "preconditions_sha256": preconditions_sha256,
            }
            return self._append_locked(
                fd,
                events,
                event_type="supervisor_approval",
                actor="supervisor",
                operation_class="state_change",
                source_ref=source_ref,
                request_sha256=str(events[0]["request_sha256"]),
                payload=_canonical({**metadata, "source_ref": source_ref}),
                metadata=metadata,
            )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def consume_approval(
        self,
        *,
        request_sha256: str,
        call_id: str,
        tool_name: str,
        arguments_sha256: str,
        preconditions: dict[str, Any],
        preconditions_sha256: str,
        state_reader: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        fd = _open_ledger(self.path, create=False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            events = _verify_chain(
                self._read_locked(fd, self.path),
                trace_id=self.trace_id,
            )
            consumed = {
                str(event.get("metadata", {}).get("approval_id"))
                for event in events
                if event.get("event_type") == "approval_consumed"
            }
            matches = [
                event
                for event in events
                if event.get("event_type") == "supervisor_approval"
                and event.get("metadata", {}).get("call_id") == call_id
                and event.get("metadata", {}).get("tool_name") == tool_name
                and event.get("metadata", {}).get("arguments_sha256")
                == arguments_sha256
                and event.get("metadata", {}).get("preconditions_sha256")
                == preconditions_sha256
                and event.get("metadata", {}).get("preconditions") == preconditions
                and str(event.get("metadata", {}).get("approval_id")) not in consumed
            ]
            if not matches:
                return None
            if len(matches) != 1:
                raise CaptureError("multiple unused approvals match one call; execution is ambiguous")
            approval = matches[0]
            metadata = approval["metadata"]
            observed = state_reader()
            if (
                _preconditions_sha256(observed) != preconditions_sha256
                or observed != preconditions
            ):
                raise StatePreconditionMismatch(preconditions, observed)
            self._append_locked(
                fd,
                events,
                event_type="approval_consumed",
                actor="system",
                operation_class="state_change",
                source_ref=f"approval:{metadata['approval_id']}",
                request_sha256=request_sha256,
                metadata={
                    "approval_id": metadata["approval_id"],
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "arguments_sha256": arguments_sha256,
                    "preconditions": preconditions,
                    "preconditions_sha256": preconditions_sha256,
                },
            )
            return approval, observed
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    cwd: str
    stdout: bytes
    stderr: bytes
    exit_status: int
    timed_out: bool = False

    def structured_bytes(self) -> bytes:
        return _canonical(
            {
                "argv": list(self.argv),
                "cwd": self.cwd,
                "stdout_b64": _b64(self.stdout),
                "stderr_b64": _b64(self.stderr),
                "stdout_text": self.stdout.decode("utf-8", errors="replace"),
                "stderr_text": self.stderr.decode("utf-8", errors="replace"),
                "text_decoding": "utf-8-errors-replace",
                "exit_status": self.exit_status,
                "timed_out": self.timed_out,
            }
        )


def _run(
    argv: list[str],
    *,
    cwd: str,
    timeout_seconds: int,
    environment: dict[str, str] | None = None,
) -> CommandResult:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            env=environment,
        )
        return CommandResult(
            argv=tuple(argv),
            cwd=cwd,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_status=result.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            argv=tuple(argv),
            cwd=cwd,
            stdout=exc.stdout or b"",
            stderr=exc.stderr or b"",
            exit_status=124,
            timed_out=True,
        )
    except OSError as exc:
        return CommandResult(
            argv=tuple(argv),
            cwd=cwd,
            stdout=b"",
            stderr=f"{type(exc).__name__}: {exc}".encode("utf-8"),
            exit_status=126,
        )


def _read_only_git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _normalise_remote(remote: str) -> str:
    value = remote.strip()
    for prefix in ("git@github.com:", "https://github.com/", "ssh://git@github.com/"):
        if value.startswith(prefix):
            return value[len(prefix):].removesuffix(".git").strip("/")
    return ""


def _public_repo(repo: str) -> tuple[str, str]:
    if not isinstance(repo, str) or not os.path.isabs(repo):
        raise CaptureError("repo must be an absolute path")
    resolved = os.path.realpath(repo)
    if not os.path.isdir(resolved):
        raise CaptureError(f"repo does not exist: {repo}")
    preflight = _run(
        ["git", "-C", resolved, "remote", "get-url", "origin"],
        cwd=resolved,
        timeout_seconds=30,
        environment=_read_only_git_environment(),
    )
    if preflight.exit_status != 0:
        raise CaptureError(
            "public-repo preflight failed: "
            + preflight.stderr.decode("utf-8", errors="replace")
        )
    slug = _normalise_remote(preflight.stdout.decode("utf-8", errors="replace"))
    if slug not in PUBLIC_REPOSITORIES:
        raise CaptureError(f"repo origin is not in the public contract allowlist: {slug or 'unknown'}")
    return resolved, slug


def _safe_ref(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("-") or "\x00" in text or "\n" in text:
        raise CaptureError(f"{field} is not a safe Git revision")
    return text


def _git_read_command(arguments: dict[str, Any]) -> tuple[list[str], str, str]:
    repo, slug = _public_repo(arguments.get("repo", ""))
    operation = str(arguments.get("operation") or "")
    try:
        max_count = int(arguments.get("max_count") or 20)
    except (TypeError, ValueError) as exc:
        raise CaptureError("max_count must be an integer") from exc
    max_count = max(1, min(max_count, 200))
    path = str(arguments.get("path") or "")
    prefix = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        repo,
    ]
    if operation == "status":
        argv = prefix + ["status", "--short", "--branch"]
    elif operation == "log":
        argv = prefix + [
            "log",
            f"--max-count={max_count}",
            "--format=%H%x09%aI%x09%s",
        ]
        if arguments.get("ref"):
            argv.append(_safe_ref(arguments["ref"], "ref"))
        if path:
            argv += ["--", path]
    elif operation == "show":
        argv = prefix + [
            "show",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--format=fuller",
        ]
        if arguments.get("ref"):
            argv.append(_safe_ref(arguments["ref"], "ref"))
        if path:
            argv += ["--", path]
    elif operation == "blame":
        if not path:
            raise CaptureError("blame requires path")
        argv = prefix + ["blame"]
        if arguments.get("ref"):
            argv.append(_safe_ref(arguments["ref"], "ref"))
        argv += ["--", path]
    elif operation == "diff":
        argv = prefix + ["diff", "--no-ext-diff", "--no-textconv", "--no-renames"]
        base = arguments.get("base")
        head = arguments.get("head")
        if base and head:
            argv.append(f"{_safe_ref(base, 'base')}..{_safe_ref(head, 'head')}")
        elif base:
            argv.append(_safe_ref(base, "base"))
        elif head:
            raise CaptureError("diff head requires base")
        if path:
            argv += ["--", path]
    elif operation == "topology":
        argv = prefix + ["log", "--graph", "--oneline", "--decorate", "--all", f"-{max_count}"]
    elif operation == "worktree":
        argv = prefix + ["worktree", "list", "--porcelain"]
    elif operation == "branch":
        argv = prefix + ["branch", "--all", "--verbose", "--no-abbrev"]
    else:
        raise CaptureError(f"unsupported read-only Git operation: {operation}")
    return argv, repo, slug


def _orchestration_read_command(arguments: dict[str, Any]) -> tuple[list[str], str]:
    operation = str(arguments.get("operation") or "")
    identifier = str(arguments.get("identifier") or "").strip()
    commands = {
        "task_list": ["taey-task", "list"],
        "plan_current": ["taey-plan", "current"],
        "plan_list": ["taey-plan", "list"],
    }
    if operation == "task_status":
        if not identifier or identifier.startswith("-") or "\x00" in identifier:
            raise CaptureError("task_status requires identifier")
        return ["taey-task", "status", identifier], ""
    if operation == "plan_show":
        if not identifier or identifier.startswith("-") or "\x00" in identifier:
            raise CaptureError("plan_show requires identifier")
        return ["taey-plan", "show", identifier], ""
    if operation not in commands:
        raise CaptureError(f"unsupported read-only orchestration operation: {operation}")
    return commands[operation], ""


def _arguments_bytes(raw_arguments: Any, parsed: dict[str, Any]) -> bytes:
    if isinstance(raw_arguments, str):
        return raw_arguments.encode("utf-8")
    if isinstance(raw_arguments, bytes):
        return raw_arguments
    try:
        return _canonical(raw_arguments)
    except (TypeError, ValueError):
        return _canonical(parsed)


def _safe_session(value: str, field: str) -> str:
    if not SAFE_SESSION_RE.fullmatch(value):
        raise CaptureError(f"{field} is outside the session identifier contract")
    return value


def _safe_task_id(value: str) -> str:
    if not TASK_ID_RE.fullmatch(value):
        raise CaptureError("task_id is outside the orchestration identifier contract")
    return value


def _safe_branch(value: str, field: str) -> str:
    if (
        not SAFE_BRANCH_RE.fullmatch(value)
        or ".." in value
        or "//" in value
        or value.endswith(("/", ".", ".lock"))
        or any(part.startswith(".") for part in value.split("/"))
    ):
        raise CaptureError(f"{field} is outside the branch-name contract")
    return value


def _bounded_text(value: str, field: str, *, maximum: int = 100_000) -> str:
    if not value or "\x00" in value or len(value.encode("utf-8")) > maximum:
        raise CaptureError(f"{field} must be non-empty, bounded UTF-8 text")
    return value


def _safe_repo_path(value: str, repo: str) -> str:
    if (
        not value
        or value.startswith("-")
        or os.path.isabs(value)
        or "\\" in value
        or "\x00" in value
        or "\n" in value
        or any(part in {"", ".", ".."} for part in pathlib.PurePosixPath(value).parts)
    ):
        raise CaptureError("git add paths must be explicit relative repository paths")
    resolved = os.path.realpath(os.path.join(repo, value))
    if os.path.commonpath([repo, resolved]) != repo:
        raise CaptureError("git add path escapes the approved public repository")
    return value


def _pr_number(value: str) -> str:
    if not value.isdecimal() or int(value) < 1:
        raise CaptureError("GitHub PR operations require an explicit positive PR number")
    return value


def _git_state_argv(argv: list[str], repo: str) -> list[str]:
    subcommand = argv[1]
    if subcommand not in APPROVED_GIT_SUBCOMMANDS:
        raise CaptureError(f"Git subcommand is outside the contract: {subcommand}")
    if subcommand == "add":
        if len(argv) < 4 or argv[2] != "--":
            raise CaptureError("git add requires: git add -- <explicit-path> [...]")
        for value in argv[3:]:
            _safe_repo_path(value, repo)
    elif subcommand == "switch":
        if len(argv) == 3:
            _safe_branch(argv[2], "switch branch")
        elif len(argv) == 5 and argv[2] == "-c":
            _safe_branch(argv[3], "new branch")
            if not FULL_SHA_RE.fullmatch(argv[4]):
                raise CaptureError("new branches require an exact 40-hex start commit")
        else:
            raise CaptureError(
                "git switch requires an existing branch or: git switch -c <branch> <40-hex-sha>"
            )
    elif subcommand == "commit":
        if len(argv) != 4 or argv[2] != "-m":
            raise CaptureError("git commit requires exactly: git commit -m <message>")
        _bounded_text(argv[3], "commit message")
    elif subcommand == "fetch":
        if len(argv) != 4 or argv[2] != "origin":
            raise CaptureError(
                "git fetch requires one explicit origin branch-to-remote-tracking refspec"
            )
        match = re.fullmatch(
            r"refs/heads/([^:]+):refs/remotes/origin/([^:]+)", argv[3]
        )
        if not match or match.group(1) != match.group(2):
            raise CaptureError(
                "fetch refspec must preserve one branch as refs/remotes/origin/<same-branch>"
            )
        _safe_branch(match.group(1), "fetch branch")
    elif subcommand == "merge":
        if (
            len(argv) != 5
            or argv[2:4] != ["--no-ff", "--no-edit"]
            or not FULL_SHA_RE.fullmatch(argv[4])
        ):
            raise CaptureError(
                "git merge requires exactly: git merge --no-ff --no-edit <40-hex-sha>"
            )
    elif subcommand == "push":
        if len(argv) != 4 or argv[2] != "origin":
            raise CaptureError(
                "git push requires exactly one explicit HEAD-to-branch origin refspec"
            )
        match = re.fullmatch(r"HEAD:refs/heads/(.+)", argv[3])
        if not match:
            raise CaptureError("push refspec must be HEAD:refs/heads/<branch>")
        _safe_branch(match.group(1), "push branch")
    return argv


def _gh_state_argv(argv: list[str], slug: str) -> list[str]:
    if len(argv) < 3 or argv[1] != "pr":
        raise CaptureError("GitHub state changes are limited to gh pr operations")
    operation = argv[2]
    if operation == "create":
        if (
            len(argv) not in {11, 12}
            or argv[3] != "--title"
            or argv[5] != "--body"
            or argv[7] != "--base"
            or argv[9] != "--head"
            or (len(argv) == 12 and argv[11] != "--draft")
        ):
            raise CaptureError(
                "gh pr create requires ordered --title, --body, --base, --head, and optional --draft"
            )
        _bounded_text(argv[4], "PR title", maximum=2_000)
        _bounded_text(argv[6], "PR body")
        _safe_branch(argv[8], "PR base")
        _safe_branch(argv[10], "PR head")
    elif operation == "comment":
        if len(argv) != 6 or argv[4] != "--body":
            raise CaptureError("gh pr comment requires: gh pr comment <number> --body <text>")
        _pr_number(argv[3])
        _bounded_text(argv[5], "PR comment")
    elif operation == "review":
        if (
            len(argv) != 7
            or argv[4] not in {"--approve", "--comment", "--request-changes"}
            or argv[5] != "--body"
        ):
            raise CaptureError(
                "gh pr review requires a PR number, one review verdict, and --body"
            )
        _pr_number(argv[3])
        _bounded_text(argv[6], "PR review")
    elif operation == "merge":
        if (
            len(argv) != 7
            or argv[4] not in {"--merge", "--rebase", "--squash"}
            or argv[5] != "--match-head-commit"
            or not FULL_SHA_RE.fullmatch(argv[6])
        ):
            raise CaptureError(
                "gh pr merge requires a PR number, strategy, and exact --match-head-commit SHA"
            )
        _pr_number(argv[3])
    else:
        raise CaptureError(f"GitHub PR operation is outside the contract: {operation}")
    return [*argv, "--repo", slug]


def _notify_state_argv(argv: list[str]) -> list[str]:
    if (
        len(argv) != 7
        or argv[3] != "--type"
        or argv[5] != "--priority"
        or argv[4] not in APPROVED_NOTIFICATION_TYPES
        or argv[6] not in {"high", "normal", "low"}
    ):
        raise CaptureError(
            "taey-notify requires target, message, --type <contract-type>, --priority <level>"
        )
    _safe_session(argv[1], "notification target")
    _bounded_text(argv[2], "notification message")
    return argv


def _plan_state_argv(argv: list[str], repo: str) -> list[str]:
    operation = argv[1]
    if operation == "next":
        if len(argv) not in {2, 3}:
            raise CaptureError("taey-plan next accepts at most one explicit session")
        if len(argv) == 3:
            _safe_session(argv[2], "next session")
    elif operation == "assign":
        if len(argv) != 4:
            raise CaptureError("taey-plan assign requires exactly task_id and session")
        _safe_task_id(argv[2])
        _safe_session(argv[3], "assignment session")
    elif operation == "ingest":
        if len(argv) != 3:
            raise CaptureError("taey-plan ingest requires exactly one plan path")
        requested_plan = pathlib.Path(argv[2]).expanduser()
        plan = (
            requested_plan
            if requested_plan.is_absolute()
            else pathlib.Path(repo) / requested_plan
        ).resolve()
        if (
            not plan.is_file()
            or plan.suffix.lower() != ".md"
            or os.path.commonpath([repo, str(plan)]) != repo
        ):
            raise CaptureError("plan ingest is limited to one markdown file in the approved repo")
        argv[2] = str(plan)
    else:
        raise CaptureError(f"taey-plan operation is outside the contract: {operation}")
    return argv


def _task_state_argv(argv: list[str]) -> list[str]:
    operation = argv[1]
    if operation == "create":
        if (
            len(argv) != 9
            or argv[3] != "--priority"
            or argv[5] != "--from"
            or argv[7] != "--type"
            or argv[8] not in {"standard", "micro"}
        ):
            raise CaptureError(
                "taey-task create requires description, priority, sender, and task type"
            )
        _bounded_text(argv[2], "task description")
        try:
            priority = int(argv[4])
        except ValueError as exc:
            raise CaptureError("task priority must be an integer") from exc
        if not 0 <= priority <= 100 or str(priority) != argv[4]:
            raise CaptureError("task priority must be canonical decimal in [0, 100]")
        _safe_session(argv[6], "task sender")
    elif operation == "outcome":
        if (
            len(argv) != 5
            or argv[2] not in {"done", "error", "interrupted"}
            or argv[3] != "--details"
        ):
            raise CaptureError("taey-task outcome requires verdict and --details")
        _bounded_text(argv[4], "task outcome details")
    elif operation == "update":
        if len(argv) < 5:
            raise CaptureError("taey-task update requires task_id, status, and exact evidence form")
        _safe_task_id(argv[2])
        status = argv[3]
        if status == "completed":
            if len(argv) != 6 or argv[4] != "--evidence":
                raise CaptureError("completed updates require one inline --evidence JSON object")
            try:
                evidence = json.loads(argv[5])
            except json.JSONDecodeError as exc:
                raise CaptureError("completion evidence must be exact JSON") from exc
            if (
                not isinstance(evidence, dict)
                or not FULL_SHA_RE.fullmatch(str(evidence.get("commit_sha") or ""))
                or evidence.get("repo") not in PUBLIC_REPOSITORIES
            ):
                raise CaptureError("completion evidence requires public repo and 40-hex commit_sha")
            _bounded_text(
                str(evidence.get("production_observation") or ""),
                "production observation",
            )
        elif status == "changes_requested":
            if len(argv) != 6 or argv[4] != "--reason":
                raise CaptureError("changes_requested requires one exact --reason")
            _bounded_text(argv[5], "changes-requested reason")
        elif status in {"failed", "interrupted"}:
            if len(argv) != 6 or argv[4] != "--reason":
                raise CaptureError(f"{status} requires one exact --reason")
            _bounded_text(argv[5], f"{status} reason")
        elif status == "in_progress":
            if len(argv) == 5 and argv[4] == "--clear-blocked-on":
                pass
            elif (
                len(argv) == 6
                and argv[4] == "--blocked-on"
                and argv[5].startswith("AWAIT:")
            ):
                _bounded_text(argv[5], "structured hold", maximum=2_000)
            else:
                raise CaptureError(
                    "in_progress update requires --clear-blocked-on or structured --blocked-on AWAIT:..."
                )
        else:
            raise CaptureError(f"taey-task update status is outside the contract: {status}")
    else:
        raise CaptureError(f"taey-task operation is outside the contract: {operation}")
    return argv


def _resolve_approved_state_command(
    arguments: dict[str, Any],
    *,
    require_preconditions: bool,
) -> ApprovedCommand:
    allowed_fields = {"argv", "cwd", "timeout_seconds"}
    if require_preconditions:
        allowed_fields.add("preconditions")
    if set(arguments) - allowed_fields:
        raise CaptureError("approved state-change arguments contain unknown fields")
    preconditions = arguments.get("preconditions")
    if require_preconditions and (
        not isinstance(preconditions, dict)
        or preconditions.get("schema_version") != STATE_PRECONDITION_VERSION
    ):
        raise CaptureError(
            "approved state changes require an exact inspected preconditions object"
        )
    argv_raw = arguments.get("argv")
    cwd_raw = arguments.get("cwd")
    if (
        not isinstance(argv_raw, list)
        or len(argv_raw) < 2
        or len(argv_raw) > 80
        or any(
            not isinstance(item, str)
            or not item
            or "\x00" in item
            or "\n" in item
            or "\r" in item
            for item in argv_raw
        )
    ):
        raise CaptureError("approved argv must contain at least two safe strings")
    argv = [str(item) for item in argv_raw]
    program = os.path.basename(argv[0])
    if program not in APPROVED_PROGRAMS:
        raise CaptureError(f"approved program is outside the contract: {program}")
    cwd = os.path.realpath(str(cwd_raw or ""))
    if not os.path.isabs(str(cwd_raw or "")) or not os.path.isdir(cwd):
        raise CaptureError("approved cwd must be an existing absolute directory")
    cwd, slug = _public_repo(cwd)
    if program == "git":
        argv = _git_state_argv(argv, cwd)
    elif program == "gh":
        argv = _gh_state_argv(argv, slug)
    elif program == "taey-notify":
        argv = _notify_state_argv(argv)
    elif program == "taey-plan":
        argv = _plan_state_argv(argv, cwd)
    elif program == "taey-task":
        argv = _task_state_argv(argv)
    argv[0] = program
    proposal_argv = tuple(argv)
    operation = argv[1]
    executable = shutil.which(program)
    if not executable:
        raise CaptureError(f"approved program is not installed: {program}")
    if program == "git":
        argv = [executable, "-c", "core.hooksPath=/dev/null", *argv[1:]]
    else:
        argv[0] = executable
    try:
        timeout_value = arguments.get("timeout_seconds", 120)
        if isinstance(timeout_value, bool):
            raise TypeError
        timeout_seconds = int(timeout_value)
    except (TypeError, ValueError) as exc:
        raise CaptureError("timeout_seconds must be an integer") from exc
    if not 1 <= timeout_seconds <= 900 or str(timeout_seconds) != str(timeout_value):
        raise CaptureError("timeout_seconds must be canonical decimal in [1, 900]")
    return ApprovedCommand(
        proposal_argv=proposal_argv,
        execution_argv=tuple(argv),
        cwd=cwd,
        public_repo=slug,
        program=program,
        operation=operation,
        timeout_seconds=timeout_seconds,
        preconditions=preconditions if require_preconditions else None,
    )


def _approved_state_command(arguments: dict[str, Any]) -> ApprovedCommand:
    return _resolve_approved_state_command(arguments, require_preconditions=True)


def _state_probe_command(arguments: dict[str, Any]) -> ApprovedCommand:
    return _resolve_approved_state_command(arguments, require_preconditions=False)


def _mutation_execution_class(command: ApprovedCommand) -> tuple[str, str]:
    argv = command.proposal_argv
    if command.program == "gh":
        operation = f"gh pr {argv[2]}"
    elif command.program == "taey-notify":
        operation = "taey-notify send"
    else:
        operation = f"{command.program} {argv[1]}"
    execution_class = MUTATION_EXECUTION_CLASSES.get(operation)
    if execution_class is None:
        raise CaptureError(f"state-change execution class is undefined: {operation}")
    return operation, execution_class


def _state_read(
    argv: list[str],
    *,
    cwd: str,
    allowed_exit_statuses: frozenset[int] = frozenset({0}),
) -> CommandResult:
    result = _run(
        argv,
        cwd=cwd,
        timeout_seconds=120,
        environment=(
            _read_only_git_environment()
            if os.path.basename(argv[0]) == "git"
            else None
        ),
    )
    if result.timed_out or result.exit_status not in allowed_exit_statuses:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CaptureError(
            f"mutable-state inspection failed for {os.path.basename(argv[0])}: "
            f"exit={result.exit_status} {detail}"
        )
    return result


def _state_observation(result: CommandResult) -> dict[str, Any]:
    return {
        "exit_status": result.exit_status,
        "stdout_sha256": _sha256(result.stdout),
        "stderr_sha256": _sha256(result.stderr),
    }


def _git_state_prefix(repo: str) -> list[str]:
    return [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        repo,
    ]


def _exact_oid(raw: bytes, field: str) -> str:
    value = raw.decode("ascii", errors="strict").strip()
    if not FULL_SHA_RE.fullmatch(value):
        raise CaptureError(f"{field} did not resolve to one exact 40-hex object")
    return value


def _git_local_ref(repo: str, ref: str, *, required: bool) -> str | None:
    result = _state_read(
        [*_git_state_prefix(repo), "show-ref", "--verify", "--hash", ref],
        cwd=repo,
        allowed_exit_statuses=frozenset({0, 1}),
    )
    if result.exit_status == 1:
        if required:
            raise CaptureError(f"required local ref is absent: {ref}")
        return None
    return _exact_oid(result.stdout, ref)


def _git_remote_ref(repo: str, ref: str, *, required: bool) -> str | None:
    result = _state_read(
        [*_git_state_prefix(repo), "ls-remote", "--refs", "origin", ref],
        cwd=repo,
    )
    lines = result.stdout.splitlines()
    if not lines:
        if required:
            raise CaptureError(f"required origin ref is absent: {ref}")
        return None
    if len(lines) != 1:
        raise CaptureError(f"origin ref is ambiguous: {ref}")
    try:
        oid, observed_ref = lines[0].decode("ascii").split("\t", 1)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CaptureError(f"origin ref response is malformed: {ref}") from exc
    if observed_ref != ref or not FULL_SHA_RE.fullmatch(oid):
        raise CaptureError(f"origin ref response is not exact: {ref}")
    return oid


def _git_repository_state(repo: str) -> dict[str, Any]:
    prefix = _git_state_prefix(repo)
    head = _state_read([*prefix, "rev-parse", "--verify", "HEAD^{commit}"], cwd=repo)
    head_ref = _state_read(
        [*prefix, "symbolic-ref", "--quiet", "HEAD"],
        cwd=repo,
        allowed_exit_statuses=frozenset({0, 1}),
    )
    index = _state_read([*prefix, "ls-files", "--stage", "-z"], cwd=repo)
    status = _state_read(
        [
            *prefix,
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
        ],
        cwd=repo,
    )
    worktree = _state_read(
        [
            *prefix,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
        ],
        cwd=repo,
    )
    config = _state_read(
        [*prefix, "config", "--local", "--null", "--list"],
        cwd=repo,
    )
    symbolic = None
    if head_ref.exit_status == 0:
        symbolic = head_ref.stdout.decode("utf-8", errors="strict").strip()
        if not symbolic.startswith("refs/heads/"):
            raise CaptureError("symbolic HEAD is outside refs/heads")
    return {
        "head_sha": _exact_oid(head.stdout, "HEAD"),
        "head_ref": symbolic,
        "index_sha256": _sha256(index.stdout),
        "status_sha256": _sha256(status.stdout),
        "worktree_diff_sha256": _sha256(worktree.stdout),
        "local_config_sha256": _sha256(config.stdout),
    }


def _git_path_state(repo: str, value: str) -> dict[str, Any]:
    _safe_repo_path(value, repo)
    path = pathlib.Path(repo) / value
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"path": value, "kind": "missing"}
    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(path)
        after = path.lstat()
        if (before.st_ino, before.st_mtime_ns) != (after.st_ino, after.st_mtime_ns):
            raise CaptureError(f"git add path changed during inspection: {value}")
        return {
            "path": value,
            "kind": "symlink",
            "target_sha256": _sha256(os.fsencode(target)),
        }
    if not stat.S_ISREG(before.st_mode):
        raise CaptureError("git add preconditions require explicit files, symlinks, or deletions")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise CaptureError(f"git add path changed during inspection: {value}")
    return {
        "path": value,
        "kind": "file",
        "mode": stat.S_IMODE(before.st_mode),
        "size": before.st_size,
        "sha256": _sha256(b"".join(chunks)),
    }


def _github_pr_state(command: ApprovedCommand, number: str) -> dict[str, Any]:
    result = _state_read(
        [
            "gh",
            "pr",
            "view",
            number,
            "--repo",
            command.public_repo,
            "--json",
            "number,state,isDraft,headRefOid,baseRefOid",
        ],
        cwd=command.cwd,
    )
    try:
        value = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError("GitHub PR state is not exact JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("number") != int(number)
        or not FULL_SHA_RE.fullmatch(str(value.get("headRefOid") or ""))
        or not FULL_SHA_RE.fullmatch(str(value.get("baseRefOid") or ""))
        or value.get("state") not in {"OPEN", "CLOSED", "MERGED"}
        or not isinstance(value.get("isDraft"), bool)
    ):
        raise CaptureError("GitHub PR state is incomplete")
    return {
        "number": value["number"],
        "state": value["state"],
        "is_draft": value["isDraft"],
        "head_sha": value["headRefOid"],
        "base_sha": value["baseRefOid"],
    }


def _command_state_observation(argv: list[str], cwd: str) -> dict[str, Any]:
    return _state_observation(_state_read(argv, cwd=cwd))


def _orchestration_state(command: ApprovedCommand) -> dict[str, Any]:
    argv = list(command.proposal_argv)
    if command.program == "taey-notify":
        return {
            "kind": "guarded_delivery",
            "target": argv[1],
            "execution_rechecks_target_readiness": True,
        }
    if command.program == "taey-plan":
        state = {
            "plan_list": _command_state_observation(
                ["taey-plan", "list"], command.cwd
            )
        }
        if command.operation == "assign":
            state["task"] = _command_state_observation(
                ["taey-task", "status", argv[2]], command.cwd
            )
        elif command.operation == "next":
            current_argv = ["taey-plan", "current"]
            if len(argv) == 3:
                current_argv.append(argv[2])
            state["current"] = _command_state_observation(
                current_argv, command.cwd
            )
        elif command.operation == "ingest":
            plan = pathlib.Path(argv[2])
            relative = os.path.relpath(plan, command.cwd)
            state["plan_file"] = _git_path_state(command.cwd, relative)
        return state
    if command.program == "taey-task":
        if command.operation == "update":
            return {
                "task": _command_state_observation(
                    ["taey-task", "status", argv[2]], command.cwd
                )
            }
        if command.operation == "outcome":
            return {
                "current": _command_state_observation(
                    ["taey-plan", "current"], command.cwd
                )
            }
        return {
            "task_list": _command_state_observation(
                ["taey-task", "list"], command.cwd
            )
        }
    raise CaptureError(f"no orchestration state contract for {command.program}")


def _read_state_preconditions(command: ApprovedCommand) -> dict[str, Any]:
    argv = list(command.proposal_argv)
    state: dict[str, Any] = {
        "repository": _git_repository_state(command.cwd),
    }
    if command.program == "git":
        refs: dict[str, Any] = {}
        if command.operation == "add":
            state["paths"] = [
                _git_path_state(command.cwd, value) for value in argv[3:]
            ]
        elif command.operation == "switch":
            if len(argv) == 3:
                branch = argv[2]
                refs["target"] = {
                    "ref": f"refs/heads/{branch}",
                    "sha": _git_local_ref(
                        command.cwd, f"refs/heads/{branch}", required=True
                    ),
                }
            else:
                branch = argv[3]
                refs["target"] = {
                    "ref": f"refs/heads/{branch}",
                    "sha": _git_local_ref(
                        command.cwd, f"refs/heads/{branch}", required=False
                    ),
                }
                refs["start"] = {"sha": argv[4]}
        elif command.operation == "fetch":
            branch = argv[3].split(":", 1)[0].removeprefix("refs/heads/")
            refs["remote_source"] = {
                "ref": f"refs/heads/{branch}",
                "sha": _git_remote_ref(
                    command.cwd, f"refs/heads/{branch}", required=True
                ),
            }
            refs["local_target"] = {
                "ref": f"refs/remotes/origin/{branch}",
                "sha": _git_local_ref(
                    command.cwd,
                    f"refs/remotes/origin/{branch}",
                    required=False,
                ),
            }
        elif command.operation == "merge":
            refs["source"] = {"sha": argv[4]}
        elif command.operation == "push":
            branch = argv[3].removeprefix("HEAD:refs/heads/")
            refs["remote_target"] = {
                "ref": f"refs/heads/{branch}",
                "sha": _git_remote_ref(
                    command.cwd, f"refs/heads/{branch}", required=False
                ),
            }
        if refs:
            state["refs"] = refs
    elif command.program == "gh":
        operation = argv[2]
        if operation == "create":
            base = argv[8]
            head = argv[10]
            state["github"] = {
                "base": {
                    "ref": f"refs/heads/{base}",
                    "sha": _git_remote_ref(
                        command.cwd, f"refs/heads/{base}", required=True
                    ),
                },
                "head": {
                    "ref": f"refs/heads/{head}",
                    "sha": _git_remote_ref(
                        command.cwd, f"refs/heads/{head}", required=True
                    ),
                },
            }
        else:
            pr_state = _github_pr_state(command, argv[3])
            if operation == "merge" and pr_state["head_sha"] != argv[6]:
                raise CaptureError("GitHub PR head no longer matches --match-head-commit")
            state["github"] = pr_state
    else:
        state["orchestration"] = _orchestration_state(command)
    return {
        "schema_version": STATE_PRECONDITION_VERSION,
        "program": command.program,
        "operation": command.operation,
        "public_repo": command.public_repo,
        "state": state,
    }


def _stable_state_preconditions(command: ApprovedCommand) -> dict[str, Any]:
    first = _read_state_preconditions(command)
    second = _read_state_preconditions(command)
    if _canonical(first) != _canonical(second):
        raise CaptureError("mutable state changed while preconditions were being read")
    return second


def _preconditions_sha256(preconditions: dict[str, Any]) -> str:
    if preconditions.get("schema_version") != STATE_PRECONDITION_VERSION:
        raise CaptureError("state preconditions use an unknown schema")
    return _sha256(_canonical(preconditions))


class SupervisedTrace:
    def __init__(
        self,
        ledger: TraceLedger,
        *,
        request_sha256: str,
        source_ref: str,
        approval_wait_seconds: float,
    ):
        self.ledger = ledger
        self.request_sha256 = request_sha256
        self.source_ref = source_ref
        self.approval_wait_seconds = max(0.0, approval_wait_seconds)
        self.model_decision_count = 0
        self.pending_model_call: dict[str, Any] | None = None

    @property
    def trace_id(self) -> str:
        return self.ledger.trace_id

    @classmethod
    def start(
        cls,
        *,
        root: str,
        trace_id: str,
        request_bytes: bytes,
        source_ref: str,
        approval_wait_seconds: float,
        request_metadata: dict[str, Any] | None = None,
    ) -> "SupervisedTrace":
        if not source_ref.strip():
            raise CaptureError("an immutable request source_ref is required")
        ledger = TraceLedger.create(root, trace_id)
        request_sha = _sha256(request_bytes)
        trace = cls(
            ledger,
            request_sha256=request_sha,
            source_ref=source_ref,
            approval_wait_seconds=approval_wait_seconds,
        )
        ledger.append(
            event_type="request",
            actor="system",
            operation_class="request",
            source_ref=source_ref,
            request_sha256=request_sha,
            payload=request_bytes,
            metadata={"contract_ref": CONTRACT_REF, **(request_metadata or {})},
        )
        return trace

    def record_model_request(
        self,
        payload: bytes,
        *,
        round_num: int,
        phase: str,
        caller_model: Any,
        model_identity: dict[str, Any],
        model_settings: dict[str, Any],
    ) -> str:
        if self.pending_model_call is not None:
            raise CaptureError("a model response must close the preceding model request")
        self.model_decision_count += 1
        decision_index = self.model_decision_count
        request_payload_sha = _sha256(payload)
        model_call_id = f"model-call-{decision_index:04d}-{request_payload_sha[:16]}"
        settings_payload = _canonical(model_settings)
        model_catalogue_sha = str(model_identity.get("catalogue_sha256") or "")
        selected_model = model_identity.get("selected")
        if (
            phase not in {"initial", "next", "final"}
            or not re.fullmatch(r"[0-9a-f]{64}", model_catalogue_sha)
            or not isinstance(selected_model, dict)
            or not str(selected_model.get("id") or "").strip()
        ):
            raise CaptureError("resolved model identity is incomplete")
        metadata = {
            "phase": phase,
            "decision_index": decision_index,
            "tool_round": round_num,
            "model_call_id": model_call_id,
            "caller_model": caller_model,
            "model_resolution": "single_loaded_upstream",
            "model_identity": model_identity,
            "model_catalogue_sha256": model_catalogue_sha,
            "model_settings": model_settings,
            "model_settings_sha256": _sha256(settings_payload),
            "upstream_request_sha256": request_payload_sha,
        }
        self.ledger.append(
            event_type="model_request",
            actor="system",
            operation_class="model",
            source_ref=f"upstream-request:{request_payload_sha}",
            request_sha256=self.request_sha256,
            payload=payload,
            metadata=metadata,
        )
        self.pending_model_call = metadata
        return model_call_id

    def record_model_response(
        self,
        payload: bytes,
        *,
        status_code: int,
        model_call_id: str,
    ) -> None:
        pending = self.pending_model_call
        if pending is None or pending.get("model_call_id") != model_call_id:
            raise CaptureError("model response does not match the pending model request")
        self.ledger.append(
            event_type="model_decision",
            actor="model",
            operation_class="model",
            source_ref=f"upstream-response:{_sha256(payload)}",
            request_sha256=self.request_sha256,
            payload=payload,
            metadata={
                "phase": pending["phase"],
                "decision_index": pending["decision_index"],
                "tool_round": pending["tool_round"],
                "model_call_id": model_call_id,
                "upstream_request_sha256": pending["upstream_request_sha256"],
                "http_status": status_code,
            },
        )
        self.pending_model_call = None

    def _record_tool_call(
        self,
        *,
        name: str,
        call_id: str,
        arguments_bytes: bytes,
        round_num: int,
        operation_class: str,
    ) -> str:
        arguments_sha = _sha256(arguments_bytes)
        self.ledger.append(
            event_type="tool_call",
            actor="model",
            operation_class=operation_class,
            source_ref=f"model-call:{call_id}",
            request_sha256=self.request_sha256,
            payload=arguments_bytes,
            metadata={
                "call_id": call_id,
                "tool_name": name,
                "arguments_sha256": arguments_sha,
                "tool_round": round_num,
            },
        )
        return arguments_sha

    def _record_result(
        self,
        *,
        name: str,
        call_id: str,
        arguments_sha256: str,
        operation_class: str,
        result: CommandResult,
        executed: bool,
        public_repo: str = "",
        approval_id: str = "",
        preconditions: dict[str, Any] | None = None,
        preconditions_sha256: str = "",
        observed_preconditions: dict[str, Any] | None = None,
        observed_preconditions_sha256: str = "",
        precondition_match: bool | None = None,
        execution_operation: str = "",
        execution_class: str = "",
        execution_reason: str = "",
    ) -> str:
        structured = result.structured_bytes()
        self.ledger.append(
            event_type="tool_result",
            actor="system",
            operation_class=operation_class,
            source_ref=f"tool-result:{_sha256(structured)}",
            request_sha256=self.request_sha256,
            payload=structured,
            metadata={
                "call_id": call_id,
                "tool_name": name,
                "arguments_sha256": arguments_sha256,
                "stdout_sha256": _sha256(result.stdout),
                "stderr_sha256": _sha256(result.stderr),
                "structured_result_sha256": _sha256(structured),
                "exit_status": result.exit_status,
                "timed_out": result.timed_out,
                "executed": executed,
                "public_repo": public_repo,
                "approval_id": approval_id,
                "preconditions": preconditions,
                "preconditions_sha256": preconditions_sha256,
                "observed_preconditions": observed_preconditions,
                "observed_preconditions_sha256": observed_preconditions_sha256,
                "precondition_match": precondition_match,
                "execution_operation": execution_operation,
                "execution_class": execution_class,
                "execution_reason": execution_reason,
            },
        )
        return structured.decode("utf-8")

    def execute_tool_call(
        self,
        *,
        name: str,
        call_id: str,
        parsed_arguments: dict[str, Any],
        raw_arguments: Any,
        round_num: int,
    ) -> str:
        if not call_id:
            raise CaptureError("a supervised model tool call must carry call_id")
        arguments_bytes = _arguments_bytes(raw_arguments, parsed_arguments)
        operation_class = (
            "state_change" if name == "run_approved_state_change" else "read_only"
        )
        arguments_sha = self._record_tool_call(
            name=name,
            call_id=call_id,
            arguments_bytes=arguments_bytes,
            round_num=round_num,
            operation_class=operation_class,
        )
        try:
            if not isinstance(parsed_arguments, dict):
                raise CaptureError("tool arguments must decode to a JSON object")
            if name == "inspect_git":
                argv, cwd, slug = _git_read_command(parsed_arguments)
                result = _run(
                    argv,
                    cwd=cwd,
                    timeout_seconds=120,
                    environment=_read_only_git_environment(),
                )
                return self._record_result(
                    name=name,
                    call_id=call_id,
                    arguments_sha256=arguments_sha,
                    operation_class=operation_class,
                    result=result,
                    executed=True,
                    public_repo=slug,
                )
            if name == "inspect_orchestration":
                argv, cwd = _orchestration_read_command(parsed_arguments)
                resolved_cwd = cwd or os.path.expanduser("~")
                result = _run(argv, cwd=resolved_cwd, timeout_seconds=120)
                return self._record_result(
                    name=name,
                    call_id=call_id,
                    arguments_sha256=arguments_sha,
                    operation_class=operation_class,
                    result=result,
                    executed=True,
                )
            if name == "inspect_state_preconditions":
                command = _state_probe_command(parsed_arguments)
                preconditions = _stable_state_preconditions(command)
                result = CommandResult(
                    argv=("inspect_state_preconditions", *command.proposal_argv),
                    cwd=command.cwd,
                    stdout=_canonical(preconditions),
                    stderr=b"",
                    exit_status=0,
                )
                return self._record_result(
                    name=name,
                    call_id=call_id,
                    arguments_sha256=arguments_sha,
                    operation_class=operation_class,
                    result=result,
                    executed=True,
                    public_repo=command.public_repo,
                    preconditions=preconditions,
                    preconditions_sha256=_preconditions_sha256(preconditions),
                    observed_preconditions=preconditions,
                    observed_preconditions_sha256=_preconditions_sha256(preconditions),
                    precondition_match=True,
                )
            if name != "run_approved_state_change":
                raise CaptureError(f"tool is outside the supervised contract: {name}")
            return self._execute_approved(
                call_id=call_id,
                arguments_sha256=arguments_sha,
                arguments=parsed_arguments,
            )
        except CaptureError as exc:
            result = CommandResult(
                argv=(),
                cwd="",
                stdout=b"",
                stderr=str(exc).encode("utf-8"),
                exit_status=126,
            )
            return self._record_result(
                name=name,
                call_id=call_id,
                arguments_sha256=arguments_sha,
                operation_class=operation_class,
                result=result,
                executed=False,
            )

    def _execute_approved(
        self,
        *,
        call_id: str,
        arguments_sha256: str,
        arguments: dict[str, Any],
    ) -> str:
        command = _approved_state_command(arguments)
        execution_operation, execution_class = _mutation_execution_class(command)
        if execution_class != "refusal":
            raise CaptureError(
                f"unsupported state-change execution class: {execution_class}"
            )
        preconditions = command.preconditions
        if preconditions is None:
            raise CaptureError("approved command lost its inspected preconditions")
        preconditions_sha256 = _preconditions_sha256(preconditions)
        result = CommandResult(
            argv=(),
            cwd=command.cwd,
            stdout=b"",
            stderr=MUTATION_REFUSAL_REASON.encode("utf-8"),
            exit_status=79,
        )
        return self._record_result(
            name="run_approved_state_change",
            call_id=call_id,
            arguments_sha256=arguments_sha256,
            operation_class="state_change",
            result=result,
            executed=False,
            public_repo=command.public_repo,
            preconditions=preconditions,
            preconditions_sha256=preconditions_sha256,
            precondition_match=False,
            execution_operation=execution_operation,
            execution_class=execution_class,
            execution_reason=MUTATION_REFUSAL_REASON,
        )

    def complete(self, *, http_status: int) -> None:
        if self.pending_model_call is not None:
            raise CaptureError("turn closure refuses an unmatched upstream model request")
        self.ledger.append(
            event_type="turn_complete",
            actor="system",
            operation_class="closure",
            source_ref=CONTRACT_REF,
            request_sha256=self.request_sha256,
            metadata={"http_status": http_status},
        )

    def fail(self, error: BaseException) -> None:
        self.ledger.append(
            event_type="turn_failed",
            actor="system",
            operation_class="closure",
            source_ref=CONTRACT_REF,
            request_sha256=self.request_sha256,
            payload=f"{type(error).__name__}: {error}".encode("utf-8"),
        )


def approve(
    *,
    root: str,
    trace_id: str,
    call_id: str,
    tool_name: str,
    arguments_sha256: str,
    preconditions_sha256: str,
    source_ref: str,
) -> dict[str, Any]:
    if not source_ref.strip():
        raise CaptureError("approval source_ref is required")
    if not call_id.strip():
        raise CaptureError("approval call_id is required")
    if not re.fullmatch(r"[0-9a-f]{64}", arguments_sha256):
        raise CaptureError("arguments_sha256 must be 64 lowercase hexadecimal characters")
    if not re.fullmatch(r"[0-9a-f]{64}", preconditions_sha256):
        raise CaptureError(
            "preconditions_sha256 must be 64 lowercase hexadecimal characters"
        )
    ledger = TraceLedger.open(root, trace_id)
    return ledger.append_approval(
        call_id=call_id,
        tool_name=tool_name,
        arguments_sha256=arguments_sha256,
        preconditions_sha256=preconditions_sha256,
        source_ref=source_ref,
    )


def append_receipt(
    *,
    root: str,
    trace_id: str,
    kind: str,
    source_ref: str,
    payload: bytes,
    public_ids: list[str],
) -> dict[str, Any]:
    if kind not in {"validation", "control"}:
        raise CaptureError("receipt kind must be validation or control")
    if not source_ref.strip():
        raise CaptureError("receipt source_ref is required")
    for value in public_ids:
        if not PUBLIC_ID_RE.fullmatch(value):
            raise CaptureError(f"public identifier is not export-safe: {value}")
    ledger = TraceLedger.open(root, trace_id)
    events = ledger.read()
    if not events or not any(event.get("event_type") == "turn_complete" for event in events):
        raise CaptureError("a receipt cannot precede a complete real turn")
    return ledger.append(
        event_type=f"{kind}_receipt",
        actor="supervisor",
        operation_class=kind,
        source_ref=source_ref,
        request_sha256=str(events[0]["request_sha256"]),
        payload=payload,
        metadata={"public_ids": public_ids},
    )


def _model_tool_calls(event: dict[str, Any]) -> list[tuple[str, str, bytes]]:
    sequence = int(event["sequence"])
    try:
        response = json.loads(_payload_bytes(event, sequence))
        choice = response["choices"][0]
        tool_calls = choice["message"].get("tool_calls") or []
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise CaptureError(f"sequence {sequence}: model decision is not a valid response") from exc
    if not isinstance(tool_calls, list):
        raise CaptureError(f"sequence {sequence}: model tool_calls must be an array")
    exact_calls: list[tuple[str, str, bytes]] = []
    for index, tool_call in enumerate(tool_calls):
        try:
            call_id = tool_call["id"]
            function = tool_call["function"]
            name = function["name"]
            arguments = function["arguments"]
        except (KeyError, TypeError) as exc:
            raise CaptureError(
                f"sequence {sequence}: model tool call {index} is incomplete"
            ) from exc
        if not isinstance(call_id, str) or not call_id:
            raise CaptureError(f"sequence {sequence}: model tool call {index} lacks call_id")
        if not isinstance(name, str) or not name:
            raise CaptureError(f"sequence {sequence}: model tool call {index} lacks name")
        if not isinstance(arguments, str):
            raise CaptureError(
                f"sequence {sequence}: model tool call {index} arguments are not exact bytes"
            )
        exact_calls.append((call_id, name, arguments.encode("utf-8")))
    return exact_calls


def _verify_tool_result(event: dict[str, Any]) -> dict[str, Any]:
    sequence = int(event["sequence"])
    payload = _payload_bytes(event, sequence)
    try:
        structured = json.loads(payload)
        stdout = base64.b64decode(structured["stdout_b64"], validate=True)
        stderr = base64.b64decode(structured["stderr_b64"], validate=True)
        exit_status = structured["exit_status"]
        timed_out = structured["timed_out"]
        argv = structured["argv"]
        cwd = structured["cwd"]
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CaptureError(f"sequence {sequence}: structured tool result is invalid") from exc
    if (
        not isinstance(argv, list)
        or any(not isinstance(value, str) for value in argv)
        or not isinstance(cwd, str)
        or not isinstance(exit_status, int)
        or isinstance(exit_status, bool)
        or not isinstance(timed_out, bool)
    ):
        raise CaptureError(f"sequence {sequence}: structured result types are invalid")
    metadata = event.get("metadata", {})
    expected = {
        "stdout_sha256": _sha256(stdout),
        "stderr_sha256": _sha256(stderr),
        "structured_result_sha256": _sha256(payload),
        "exit_status": exit_status,
        "timed_out": timed_out,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise CaptureError(f"sequence {sequence}: {key} does not match exact result bytes")
    return structured


def _verify_model_pairs(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        caller_body = json.loads(_payload_bytes(events[0], int(events[0]["sequence"])))
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CaptureError("exact caller request is not a JSON object") from exc
    if not isinstance(caller_body, dict):
        raise CaptureError("exact caller request is not a JSON object")
    caller_model = caller_body.get("model")
    requests = [event for event in events if event.get("event_type") == "model_request"]
    responses = [event for event in events if event.get("event_type") == "model_decision"]
    if not requests or len(requests) != len(responses):
        raise CaptureError("every upstream model request requires one exact response")
    call_ids: set[str] = set()
    first_identity: dict[str, Any] | None = None
    for expected_index, (request, response) in enumerate(
        zip(requests, responses), start=1
    ):
        request_sequence = int(request["sequence"])
        response_sequence = int(response["sequence"])
        request_metadata = request.get("metadata", {})
        response_metadata = response.get("metadata", {})
        call_id = str(request_metadata.get("model_call_id") or "")
        request_payload = _payload_bytes(request, request_sequence)
        request_payload_sha = _sha256(request_payload)
        if (
            not call_id
            or call_id in call_ids
            or response_sequence != request_sequence + 1
            or request.get("actor") != "system"
            or response.get("actor") != "model"
            or request.get("operation_class") != "model"
            or response.get("operation_class") != "model"
        ):
            raise CaptureError(
                f"sequence {request_sequence}: model request/response pairing is invalid"
            )
        call_ids.add(call_id)
        for key in ("model_call_id", "decision_index", "phase", "tool_round"):
            if response_metadata.get(key) != request_metadata.get(key):
                raise CaptureError(
                    f"sequence {request_sequence}: model pair diverges on {key}"
                )
        if (
            request_metadata.get("decision_index") != expected_index
            or request_metadata.get("caller_model") != caller_model
            or request_metadata.get("upstream_request_sha256") != request_payload_sha
            or response_metadata.get("upstream_request_sha256") != request_payload_sha
        ):
            raise CaptureError(
                f"sequence {request_sequence}: upstream request hash or order diverged"
            )
        try:
            request_body = json.loads(request_payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CaptureError(
                f"sequence {request_sequence}: upstream request is not exact JSON"
            ) from exc
        if not isinstance(request_body, dict):
            raise CaptureError(
                f"sequence {request_sequence}: upstream request is not an object"
            )
        if "model" in request_body:
            raise CaptureError(
                f"sequence {request_sequence}: upstream-loaded model resolution was bypassed"
            )
        expected_settings = {
            key: request_body[key] for key in sorted(request_body) if key != "messages"
        }
        if (
            request_metadata.get("model_settings") != expected_settings
            or request_metadata.get("model_settings_sha256")
            != _sha256(_canonical(expected_settings))
        ):
            raise CaptureError(
                f"sequence {request_sequence}: complete model settings do not match request bytes"
            )
        identity = request_metadata.get("model_identity")
        if not isinstance(identity, dict):
            raise CaptureError(
                f"sequence {request_sequence}: resolved model identity is missing"
            )
        catalogue = identity.get("catalogue")
        selected = identity.get("selected")
        models = catalogue.get("data") if isinstance(catalogue, dict) else None
        if first_identity is None:
            first_identity = identity
        if (
            request_metadata.get("model_resolution") != "single_loaded_upstream"
            or identity != first_identity
            or not isinstance(models, list)
            or len(models) != 1
            or not isinstance(selected, dict)
            or selected != models[0]
            or not str(selected.get("id") or "").strip()
            or not str(selected.get("root") or "").strip()
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(identity.get("catalogue_sha256") or "")
            )
            or request_metadata.get("model_catalogue_sha256")
            != identity.get("catalogue_sha256")
        ):
            raise CaptureError(
                f"sequence {request_sequence}: model authority is ambiguous or incomplete"
            )
    return responses


def _state_call_preconditions(
    call: dict[str, Any],
) -> tuple[dict[str, Any], str, str, str]:
    sequence = int(call["sequence"])
    try:
        proposal = json.loads(_payload_bytes(call, sequence))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CaptureError(
            f"sequence {sequence}: state-change proposal is not exact JSON"
        ) from exc
    preconditions = proposal.get("preconditions") if isinstance(proposal, dict) else None
    if not isinstance(preconditions, dict):
        raise CaptureError(
            f"sequence {sequence}: state-change proposal lacks preconditions"
        )
    argv = proposal.get("argv")
    if (
        not isinstance(argv, list)
        or len(argv) < 2
        or any(not isinstance(value, str) for value in argv)
    ):
        raise CaptureError(
            f"sequence {sequence}: state-change proposal lacks exact argv"
        )
    program = os.path.basename(argv[0])
    if program == "gh":
        if len(argv) < 3:
            raise CaptureError(f"sequence {sequence}: GitHub operation is incomplete")
        operation = f"gh pr {argv[2]}"
    elif program == "taey-notify":
        operation = "taey-notify send"
    else:
        operation = f"{program} {argv[1]}"
    execution_class = MUTATION_EXECUTION_CLASSES.get(operation)
    if execution_class is None:
        raise CaptureError(
            f"sequence {sequence}: state-change execution class is undefined"
        )
    return (
        preconditions,
        _preconditions_sha256(preconditions),
        operation,
        execution_class,
    )


def verify_trace(*, root: str, trace_id: str, admission: bool) -> dict[str, Any]:
    ledger = TraceLedger.open(root, trace_id)
    events = ledger.read()
    event_types = [str(event.get("event_type")) for event in events]
    unknown_event_types = sorted(set(event_types) - EVENT_TYPES)
    if unknown_event_types:
        raise CaptureError(f"trace contains unknown event types: {unknown_event_types}")
    if not events or event_types.count("request") != 1 or event_types[0] != "request":
        raise CaptureError("trace has no exact request")
    if event_types.count("turn_complete") != 1 or "turn_failed" in event_types:
        raise CaptureError("trace does not have exactly one successful turn closure")
    complete_sequence = next(
        int(event["sequence"])
        for event in events
        if event.get("event_type") == "turn_complete"
    )
    complete_event = next(
        event for event in events if event.get("event_type") == "turn_complete"
    )
    if not 200 <= int(complete_event.get("metadata", {}).get("http_status", 0)) < 300:
        raise CaptureError("successful turn closure must carry a 2xx HTTP status")
    if any(
        int(event["sequence"]) > complete_sequence
        and event.get("event_type") not in {"validation_receipt", "control_receipt"}
        for event in events
    ):
        raise CaptureError("only validation and CONTROL receipts may follow turn closure")
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for event in events:
        metadata = event.get("metadata", {})
        call_id = str(metadata.get("call_id") or "")
        if event.get("event_type") == "tool_call":
            if not call_id or call_id in calls:
                raise CaptureError("tool call IDs must be present and unique")
            expected_class = (
                "state_change"
                if metadata.get("tool_name") == "run_approved_state_change"
                else "read_only"
            )
            if event.get("operation_class") != expected_class:
                raise CaptureError(f"tool call {call_id} has the wrong operation class")
            if metadata.get("arguments_sha256") != event.get("payload_sha256"):
                raise CaptureError(f"tool call {call_id} argument hash mismatch")
            calls[call_id] = event
        if event.get("event_type") == "tool_result":
            if call_id not in calls or call_id in results:
                raise CaptureError("every result must match one preceding unique call")
            call = calls[call_id]
            if (
                metadata.get("tool_name") != call.get("metadata", {}).get("tool_name")
                or metadata.get("arguments_sha256")
                != call.get("metadata", {}).get("arguments_sha256")
                or event.get("operation_class") != call.get("operation_class")
            ):
                raise CaptureError(f"tool result {call_id} does not match its proposal")
            _verify_tool_result(event)
            results[call_id] = event
    if set(calls) != set(results):
        raise CaptureError("every tool call must have exactly one exact result")
    model_events = _verify_model_pairs(events)
    linked_calls: set[str] = set()
    for index, model_event in enumerate(model_events):
        model_sequence = int(model_event["sequence"])
        if not 200 <= int(model_event.get("metadata", {}).get("http_status", 0)) < 300:
            raise CaptureError(f"sequence {model_sequence}: model decision was not successful")
        next_model_sequence = (
            int(model_events[index + 1]["sequence"])
            if index + 1 < len(model_events)
            else complete_sequence
        )
        expected_calls = _model_tool_calls(model_event)
        if expected_calls and index + 1 == len(model_events):
            raise CaptureError(
                f"sequence {model_sequence}: tool results lack Taey's next model decision"
            )
        actual_calls = [
            event
            for event in events
            if event.get("event_type") == "tool_call"
            and model_sequence < int(event["sequence"]) < next_model_sequence
        ]
        if len(expected_calls) != len(actual_calls):
            raise CaptureError(
                f"sequence {model_sequence}: recorded proposals do not match model tool calls"
            )
        for expected, actual in zip(expected_calls, actual_calls):
            call_id, name, arguments = expected
            metadata = actual.get("metadata", {})
            if (
                metadata.get("call_id") != call_id
                or metadata.get("tool_name") != name
                or _payload_bytes(actual, int(actual["sequence"])) != arguments
            ):
                raise CaptureError(
                    f"sequence {model_sequence}: tool call order or exact bytes diverged"
                )
            result_sequence = int(results[call_id]["sequence"])
            if not int(actual["sequence"]) < result_sequence < next_model_sequence:
                raise CaptureError(f"tool call {call_id} result or next decision is reordered")
            linked_calls.add(call_id)
    if linked_calls != set(calls):
        raise CaptureError("one or more recorded tool calls lack a model proposal")
    executed_state_changes = 0
    successful_results = True
    for call_id, result in results.items():
        metadata = result.get("metadata", {})
        executed = metadata.get("executed") is True
        structured = _verify_tool_result(result)
        if not executed or structured["exit_status"] != 0 or structured["timed_out"]:
            successful_results = False
        if result.get("operation_class") == "state_change":
            call_sequence = int(calls[call_id]["sequence"])
            result_sequence = int(result["sequence"])
            arguments_sha = calls[call_id].get("metadata", {}).get("arguments_sha256")
            (
                preconditions,
                preconditions_sha,
                execution_operation,
                execution_class,
            ) = _state_call_preconditions(calls[call_id])
            if (
                metadata.get("preconditions") != preconditions
                or metadata.get("preconditions_sha256") != preconditions_sha
            ):
                raise CaptureError(
                    f"state change {call_id} result lost its proposed preconditions"
                )
            observed_preconditions = metadata.get("observed_preconditions")
            observed_sha = metadata.get("observed_preconditions_sha256")
            precondition_match = metadata.get("precondition_match")
            if observed_preconditions is not None:
                if (
                    not isinstance(observed_preconditions, dict)
                    or _preconditions_sha256(observed_preconditions) != observed_sha
                ):
                    raise CaptureError(
                        f"state change {call_id} observed preconditions are invalid"
                    )
                if precondition_match is True and observed_preconditions != preconditions:
                    raise CaptureError(
                        f"state change {call_id} falsely reports a state match"
                    )
                if precondition_match is False and observed_preconditions == preconditions:
                    raise CaptureError(
                        f"state change {call_id} falsely reports state drift"
                    )
            elif observed_sha or precondition_match is True:
                raise CaptureError(
                    f"state change {call_id} lacks its observed preconditions"
                )
            required = [
                event
                for event in events
                if event.get("event_type") == "approval_required"
                and event.get("metadata", {}).get("call_id") == call_id
            ]
            approvals = [
                event
                for event in events
                if event.get("event_type") == "supervisor_approval"
                and event.get("metadata", {}).get("call_id") == call_id
            ]
            consumed = [
                event
                for event in events
                if event.get("event_type") == "approval_consumed"
                and event.get("metadata", {}).get("call_id") == call_id
            ]
            execution_checks = [
                event
                for event in events
                if event.get("event_type") == "execution_preconditions_checked"
                and event.get("metadata", {}).get("call_id") == call_id
            ]
            if (
                len(required) > 1
                or len(approvals) > 1
                or len(consumed) > 1
                or len(execution_checks) > 1
            ):
                raise CaptureError(
                    f"state change {call_id} duplicated mutation authority"
                )
            if execution_class == "refusal":
                refusal_stderr = base64.b64decode(
                    structured["stderr_b64"], validate=True
                )
                if (
                    executed
                    or required
                    or approvals
                    or consumed
                    or execution_checks
                    or structured["argv"]
                    or structured["exit_status"] != 79
                    or structured["timed_out"]
                    or refusal_stderr != MUTATION_REFUSAL_REASON.encode("utf-8")
                    or metadata.get("approval_id")
                    or observed_preconditions is not None
                    or observed_sha
                    or precondition_match is not False
                    or metadata.get("execution_operation") != execution_operation
                    or metadata.get("execution_class") != execution_class
                    or metadata.get("execution_reason") != MUTATION_REFUSAL_REASON
                ):
                    raise CaptureError(
                        f"state change {call_id} did not preserve refusal authority"
                    )
                continue
            for approval_event in [*required, *approvals, *consumed]:
                approval_metadata = approval_event.get("metadata", {})
                if (
                    approval_metadata.get("tool_name") != "run_approved_state_change"
                    or approval_metadata.get("arguments_sha256") != arguments_sha
                    or approval_metadata.get("preconditions") != preconditions
                    or approval_metadata.get("preconditions_sha256")
                    != preconditions_sha
                ):
                    raise CaptureError(f"state change {call_id} approval metadata diverged")
            execution_check = execution_checks[0] if execution_checks else None
            if execution_check is not None:
                check_metadata = execution_check.get("metadata", {})
                check_observed = check_metadata.get("observed_preconditions")
                check_observed_sha = check_metadata.get(
                    "observed_preconditions_sha256"
                )
                check_match = check_metadata.get("precondition_match")
                read_error = check_metadata.get("read_error")
                if (
                    execution_check.get("actor") != "system"
                    or execution_check.get("operation_class") != "state_change"
                    or check_metadata.get("tool_name")
                    != "run_approved_state_change"
                    or check_metadata.get("arguments_sha256") != arguments_sha
                    or check_metadata.get("preconditions") != preconditions
                    or check_metadata.get("preconditions_sha256")
                    != preconditions_sha
                ):
                    raise CaptureError(
                        f"state change {call_id} final precondition check diverged"
                    )
                if isinstance(check_observed, dict):
                    if (
                        _preconditions_sha256(check_observed) != check_observed_sha
                        or read_error != ""
                        or check_match is not (check_observed == preconditions)
                    ):
                        raise CaptureError(
                            f"state change {call_id} final precondition evidence is invalid"
                        )
                elif (
                    check_observed is not None
                    or check_observed_sha
                    or check_match is not False
                    or not isinstance(read_error, str)
                    or not read_error
                ):
                    raise CaptureError(
                        f"state change {call_id} final precondition read failure is invalid"
                    )
                if (
                    metadata.get("approval_id")
                    != check_metadata.get("approval_id")
                    or observed_preconditions != check_observed
                    or observed_sha != check_observed_sha
                    or precondition_match is not check_match
                ):
                    raise CaptureError(
                        f"state change {call_id} result lost its final precondition evidence"
                    )
            if executed:
                executed_state_changes += 1
                if (
                    len(required) != 1
                    or len(approvals) != 1
                    or len(consumed) != 1
                    or len(execution_checks) != 1
                    or precondition_match is not True
                    or observed_preconditions != preconditions
                ):
                    raise CaptureError(
                        f"executed state change {call_id} lacks exact state-bound authority"
                    )
                approval_id = str(approvals[0].get("metadata", {}).get("approval_id") or "")
                if (
                    not re.fullmatch(r"[0-9a-f]{32}", approval_id)
                    or
                    not call_sequence
                    < int(required[0]["sequence"])
                    < int(approvals[0]["sequence"])
                    < int(consumed[0]["sequence"])
                    < int(execution_check["sequence"])
                    < result_sequence
                    or consumed[0].get("metadata", {}).get("approval_id") != approval_id
                    or execution_check.get("metadata", {}).get("approval_id")
                    != approval_id
                    or execution_check.get("source_ref")
                    != f"approval:{approval_id}"
                    or metadata.get("approval_id") != approval_id
                    or approvals[0].get("metadata", {}).get("arguments_sha256")
                    != arguments_sha
                    or consumed[0].get("metadata", {}).get("arguments_sha256")
                    != arguments_sha
                ):
                    raise CaptureError(f"state change {call_id} approval binding is invalid")
            else:
                if consumed:
                    approval_id = str(
                        approvals[0].get("metadata", {}).get("approval_id")
                        if len(approvals) == 1
                        else ""
                    )
                    if (
                        len(required) != 1
                        or len(approvals) != 1
                        or len(consumed) != 1
                        or len(execution_checks) != 1
                        or precondition_match is not False
                        or not re.fullmatch(r"[0-9a-f]{32}", approval_id)
                        or not call_sequence
                        < int(required[0]["sequence"])
                        < int(approvals[0]["sequence"])
                        < int(consumed[0]["sequence"])
                        < int(execution_check["sequence"])
                        < result_sequence
                        or consumed[0].get("metadata", {}).get("approval_id")
                        != approval_id
                        or execution_check.get("metadata", {}).get("approval_id")
                        != approval_id
                        or execution_check.get("source_ref")
                        != f"approval:{approval_id}"
                        or metadata.get("approval_id") != approval_id
                    ):
                        raise CaptureError(
                            f"state change {call_id} did not fail closed after consumption"
                        )
                elif execution_checks:
                    raise CaptureError(
                        f"state change {call_id} checked execution state without consuming authority"
                    )
                elif approvals:
                    if (
                        len(required) != 1
                        or len(approvals) != 1
                        or precondition_match is not False
                        or not call_sequence
                        < int(required[0]["sequence"])
                        < int(approvals[0]["sequence"])
                        < result_sequence
                    ):
                        raise CaptureError(
                            f"state change {call_id} did not fail closed after drift"
                        )
                elif required:
                    if (
                        precondition_match is not None
                        or not call_sequence
                        < int(required[0]["sequence"])
                        < result_sequence
                    ):
                        raise CaptureError(
                            f"state change {call_id} has an invalid approval timeout"
                        )
                elif precondition_match is not False:
                    raise CaptureError(
                        f"state change {call_id} bypassed proposal-state validation"
                    )
        elif any(
            event.get("metadata", {}).get("call_id") == call_id
            for event in events
            if event.get("event_type")
            in {
                "approval_required",
                "supervisor_approval",
                "approval_consumed",
                "execution_preconditions_checked",
            }
        ):
            raise CaptureError(f"read-only call {call_id} carries mutation authority events")
    validation_count = event_types.count("validation_receipt")
    control_count = event_types.count("control_receipt")
    validations = [event for event in events if event.get("event_type") == "validation_receipt"]
    controls = [event for event in events if event.get("event_type") == "control_receipt"]
    if any(
        event.get("actor") != "supervisor"
        or event.get("operation_class") != "validation"
        for event in validations
    ) or any(
        event.get("actor") != "supervisor"
        or event.get("operation_class") != "control"
        for event in controls
    ):
        raise CaptureError("validation and CONTROL receipts must be supervisor-authored")
    receipt_order_valid = (
        validation_count == 1
        and int(validations[0]["sequence"]) > complete_sequence
        and bool(_payload_bytes(validations[0], int(validations[0]["sequence"])))
        and (
            (executed_state_changes == 0 and control_count == 0)
            or (
                executed_state_changes > 0
                and control_count == 1
                and int(controls[0]["sequence"]) > int(validations[0]["sequence"])
                and bool(_payload_bytes(controls[0], int(controls[0]["sequence"])))
            )
        )
    )
    admission_ready = bool(calls) and successful_results and receipt_order_valid
    if admission and not admission_ready:
        raise CaptureError(
            "admission requires real successful tool execution, one independent validation, "
            "and ordered CONTROL for mutations"
        )
    return {
        "ok": True,
        "trace_id": trace_id,
        "contract_ref": CONTRACT_REF,
        "events": len(events),
        "model_rounds": len(model_events),
        "tool_calls": len(calls),
        "state_changes_executed": executed_state_changes,
        "validation_receipts": validation_count,
        "control_receipts": control_count,
        "last_event_sha256": events[-1]["event_sha256"],
        "admission_ready": admission_ready,
    }


def export_public(*, root: str, trace_id: str, output: str) -> dict[str, Any]:
    verification = verify_trace(root=root, trace_id=trace_id, admission=False)
    ledger = TraceLedger.open(root, trace_id)
    events = ledger.read()
    output_path = pathlib.Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(output_path, flags, 0o644)
    except OSError as exc:
        raise CaptureError(f"public export refuses overwrite or unsafe path: {exc}") from exc
    try:
        for event in events:
            metadata = event.get("metadata", {})
            public = {
                "schema_version": SCHEMA_VERSION,
                "contract_ref": CONTRACT_REF,
                "trace_id": trace_id,
                "sequence": event["sequence"],
                "recorded_at": event["recorded_at"],
                "actor": event["actor"],
                "event_type": event["event_type"],
                "operation_class": event["operation_class"],
                "request_sha256": event["request_sha256"],
                "payload_sha256": event["payload_sha256"],
                "source_ref_sha256": _sha256(str(event["source_ref"]).encode("utf-8")),
                "event_sha256": event["event_sha256"],
                "previous_event_sha256": event["previous_event_sha256"],
                "call_id_sha256": (
                    _sha256(str(metadata["call_id"]).encode("utf-8"))
                    if metadata.get("call_id")
                    else None
                ),
                "model_call_id_sha256": (
                    _sha256(str(metadata["model_call_id"]).encode("utf-8"))
                    if metadata.get("model_call_id")
                    else None
                ),
                "decision_index": metadata.get("decision_index"),
                "upstream_request_sha256": metadata.get("upstream_request_sha256"),
                "model_settings_sha256": metadata.get("model_settings_sha256"),
                "model_catalogue_sha256": metadata.get("model_catalogue_sha256"),
                "resolved_model_id_sha256": (
                    _sha256(
                        str(metadata["model_identity"]["selected"]["id"]).encode("utf-8")
                    )
                    if isinstance(metadata.get("model_identity"), dict)
                    and isinstance(metadata["model_identity"].get("selected"), dict)
                    and metadata["model_identity"]["selected"].get("id")
                    else None
                ),
                "tool_name": metadata.get("tool_name"),
                "arguments_sha256": metadata.get("arguments_sha256"),
                "preconditions_sha256": metadata.get("preconditions_sha256"),
                "observed_preconditions_sha256": metadata.get(
                    "observed_preconditions_sha256"
                ),
                "precondition_match": metadata.get("precondition_match"),
                "stdout_sha256": metadata.get("stdout_sha256"),
                "stderr_sha256": metadata.get("stderr_sha256"),
                "structured_result_sha256": metadata.get("structured_result_sha256"),
                "exit_status": metadata.get("exit_status"),
                "approval_id": metadata.get("approval_id"),
                "public_repo": metadata.get("public_repo"),
                "public_ids": metadata.get("public_ids", []),
                "admission_ready": verification["admission_ready"],
            }
            line = _canonical(public) + b"\n"
            _write_all(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    return {
        "ok": True,
        "trace_id": trace_id,
        "contract_ref": CONTRACT_REF,
        "events": len(events),
        "admission_ready": verification["admission_ready"],
        "output": str(output_path),
        "sha256": _sha256(output_path.read_bytes()),
    }


def _payload_from_args(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read()
    return pathlib.Path(path).read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Private 0700 capture root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pending_parser = subparsers.add_parser("pending")
    pending_parser.add_argument("trace_id")

    approve_parser = subparsers.add_parser("approve")
    approve_parser.add_argument("trace_id")
    approve_parser.add_argument("--call-id", required=True)
    approve_parser.add_argument("--tool-name", default="run_approved_state_change")
    approve_parser.add_argument("--arguments-sha256", required=True)
    approve_parser.add_argument("--preconditions-sha256", required=True)
    approve_parser.add_argument("--source-ref", required=True)

    receipt_parser = subparsers.add_parser("receipt")
    receipt_parser.add_argument("trace_id")
    receipt_parser.add_argument("--kind", choices=("validation", "control"), required=True)
    receipt_parser.add_argument("--source-ref", required=True)
    receipt_parser.add_argument("--payload-file", required=True)
    receipt_parser.add_argument("--public-id", action="append", default=[])

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("trace_id")
    verify_parser.add_argument("--admission", action="store_true")

    export_parser = subparsers.add_parser("export-public")
    export_parser.add_argument("trace_id")
    export_parser.add_argument("--out", required=True)

    args = parser.parse_args()
    try:
        if args.command == "pending":
            events = TraceLedger.open(args.root, args.trace_id).read()
            approved_or_resolved = {
                event.get("metadata", {}).get("call_id")
                for event in events
                if event.get("event_type") in {"supervisor_approval", "tool_result"}
            }
            calls = {
                event.get("metadata", {}).get("call_id"): event
                for event in events
                if event.get("event_type") == "tool_call"
            }
            pending = [
                {
                    **event.get("metadata", {}),
                    "arguments_b64": calls.get(
                        event.get("metadata", {}).get("call_id"), {}
                    ).get("payload_b64"),
                }
                for event in events
                if event.get("event_type") == "approval_required"
                and event.get("metadata", {}).get("call_id") not in approved_or_resolved
            ]
            print(json.dumps({"trace_id": args.trace_id, "pending": pending}, sort_keys=True))
        elif args.command == "approve":
            result = approve(
                root=args.root,
                trace_id=args.trace_id,
                call_id=args.call_id,
                tool_name=args.tool_name,
                arguments_sha256=args.arguments_sha256,
                preconditions_sha256=args.preconditions_sha256,
                source_ref=args.source_ref,
            )
            print(json.dumps(result, sort_keys=True))
        elif args.command == "receipt":
            result = append_receipt(
                root=args.root,
                trace_id=args.trace_id,
                kind=args.kind,
                source_ref=args.source_ref,
                payload=_payload_from_args(args.payload_file),
                public_ids=args.public_id,
            )
            print(json.dumps(result, sort_keys=True))
        elif args.command == "verify":
            print(
                json.dumps(
                    verify_trace(
                        root=args.root,
                        trace_id=args.trace_id,
                        admission=args.admission,
                    ),
                    sort_keys=True,
                )
            )
        elif args.command == "export-public":
            print(
                json.dumps(
                    export_public(root=args.root, trace_id=args.trace_id, output=args.out),
                    sort_keys=True,
                )
            )
    except (CaptureError, OSError) as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
