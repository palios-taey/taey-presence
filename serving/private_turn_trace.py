from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any


_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")


class PrivateTurnTraceError(RuntimeError):
    pass


class PrivateTurnTraceConfigurationError(PrivateTurnTraceError):
    pass


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ))


def _iso8601(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _open_private_directory(parent_fd: int, component: str) -> int:
    if _COMPONENT.fullmatch(component) is None:
        raise PrivateTurnTraceConfigurationError(
            "private turn trace identity component is invalid"
        )
    created = False
    try:
        os.mkdir(component, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    if created:
        os.fsync(parent_fd)
    descriptor = os.open(
        component,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_fd,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise PrivateTurnTraceConfigurationError(
                "private turn trace directories must be owned by the proxy user "
                "with mode 0700"
            )
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _validate_private_root_path(root: str) -> None:
    if (
        not root
        or not os.path.isabs(root)
        or os.path.abspath(root) != root
        or os.path.realpath(root) != root
    ):
        raise PrivateTurnTraceConfigurationError(
            "TAEY_PRIVATE_TURN_TRACE_DIR must be configured as an absolute, "
            "non-symlink path"
        )


def _open_private_root(root: str) -> int:
    _validate_private_root_path(root)
    descriptor = os.open(
        root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise PrivateTurnTraceConfigurationError(
                "TAEY_PRIVATE_TURN_TRACE_DIR must be owned by the proxy user "
                "with mode 0700"
            )
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _atomic_checkpoint_write(parent_fd: int, name: str, payload: dict) -> None:
    encoded = (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    temporary = f".{name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.geteuid()
            or stat.S_IMODE(existing.st_mode) != 0o400
        ):
            raise PrivateTurnTraceError(
                "private turn trace target is not an owned mode-0400 regular file"
            )
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while checkpointing private turn trace")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise


@dataclass
class PrivateTurnTrace:
    root: str
    identity: dict[str, Any]
    messages: list[dict[str, Any]]
    checkpoint_index: int = -1
    failed: bool = False
    terminal: bool = False
    last_sequence_state: dict[str, Any] = field(default_factory=dict)
    last_tool_rounds: int = 0
    _target_name: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        turn_id = str(self.identity.get("turn_id") or "")
        if _COMPONENT.fullmatch(turn_id) is None:
            raise PrivateTurnTraceConfigurationError(
                "private turn trace requires an exact turn_id"
            )
        self.messages = _json_clone(self.messages)
        self._target_name = f"turn_trace_{turn_id}.json"

    @classmethod
    def start(
        cls,
        *,
        root: str,
        required: bool,
        enabled: bool,
        identity: dict[str, Any],
        messages: list[dict[str, Any]],
        sequence_state: dict[str, Any],
    ) -> PrivateTurnTrace | None:
        if not enabled:
            return None
        if not root:
            if required:
                raise PrivateTurnTraceConfigurationError(
                    "TAEY_PRIVATE_TURN_TRACE_DIR must be configured as an "
                    "absolute path"
                )
            return None
        _validate_private_root_path(root)
        trace = cls(root=root, identity=_json_clone(identity), messages=messages)
        trace.checkpoint(
            phase="turn_start",
            state="in_progress",
            tool_rounds=0,
            sequence_state=sequence_state,
        )
        return trace

    def append_message(self, message: dict[str, Any]) -> None:
        if self.failed or self.terminal:
            raise PrivateTurnTraceError(
                "private turn trace cannot append after failure or terminal state"
            )
        self.messages.append(_json_clone(message))

    def checkpoint(
        self,
        *,
        phase: str,
        state: str,
        tool_rounds: int,
        sequence_state: dict[str, Any],
        usage: dict[str, Any] | None = None,
        terminal_response: Any = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if self.failed:
            raise PrivateTurnTraceError("private turn trace is already failed")
        if self.terminal:
            raise PrivateTurnTraceError("private turn trace is already terminal")
        checkpoint_index = self.checkpoint_index + 1
        updated_at = time.time()
        payload = {
            "schema": "taey_private_turn_trace_v1",
            **self.identity,
            "started_at": _iso8601(float(self.identity["started_at"])),
            "updated_at": _iso8601(updated_at),
            "checkpoint_index": checkpoint_index,
            "checkpoint_phase": phase,
            "state": state,
            "tool_rounds": tool_rounds,
            "messages": self.messages,
            "sequence_state": sequence_state,
            "usage": usage or {},
        }
        if terminal_response is not None:
            payload["terminal_response"] = terminal_response
        if error is not None:
            payload["error"] = error
        if state != "in_progress":
            payload["ended_at"] = _iso8601(updated_at)
            payload["outcome"] = state
        try:
            with ExitStack() as opened:
                root_fd = _open_private_root(self.root)
                opened.callback(os.close, root_fd)
                event_fd = _open_private_directory(
                    root_fd,
                    str(self.identity["event_id"]),
                )
                opened.callback(os.close, event_fd)
                _atomic_checkpoint_write(event_fd, self._target_name, payload)
        except Exception as exc:
            self.failed = True
            raise PrivateTurnTraceError(
                "turn_trace_checkpoint_failed"
            ) from exc
        self.checkpoint_index = checkpoint_index
        self.last_sequence_state = _json_clone(sequence_state)
        self.last_tool_rounds = tool_rounds
        self.terminal = state != "in_progress"
