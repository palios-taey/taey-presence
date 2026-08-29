#!/usr/bin/env python3
from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


SERVING_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVING_ROOT))

import private_turn_trace as trace_module  # noqa: E402
from private_turn_trace import (  # noqa: E402
    PrivateTurnTrace,
    PrivateTurnTraceConfigurationError,
    PrivateTurnTraceError,
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def read_trace(path: Path) -> dict:
    require(
        stat.S_IMODE(path.stat().st_mode) == 0o400,
        "private turn trace is not mode 0400",
    )
    return json.loads(path.read_text(encoding="utf-8"))


def function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    require(len(functions) == 1, f"{name} is not unique")
    segment = ast.get_source_segment(source, functions[0])
    require(segment is not None, f"could not read {name}")
    return str(segment)


async def validate_proxy_integration(root: Path, *, stream: bool) -> None:
    import soma_proxy

    arguments = json.dumps({"display": ":18"}, separators=(",", ":"))
    upstream = {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-001",
                    "type": "function",
                    "function": {
                        "name": "linkedin_jobs",
                        "arguments": arguments,
                    },
                }],
            },
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1},
    }

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return upstream

    class FakeHttp:
        async def post(self, *_args, **_kwargs) -> FakeResponse:
            return FakeResponse()

    async def fake_execute(*_args, **_kwargs) -> str:
        return '{"ok":true,"state":"captured"}'

    suffix = "stream" if stream else "nonstream"
    turn = soma_proxy.TurnContext(
        turn_id=f"integration-{suffix}-turn",
        seat_id="integration-seat",
        event_id=f"integration-{suffix}-event",
        correlation_id=f"integration-{suffix}-correlation",
        tool_profile="linkedin-jobs",
        proxy_namespace="integration-proxy",
        process_generation="generation-001",
        started_at=1_800_000_000.0,
    )
    context = {
        **soma_proxy._turn_payload(turn),
        "_tool_profile_state": {"terminal": None},
    }
    token = soma_proxy._request_context.set(context)
    prior = {
        "_http": soma_proxy._http,
        "execute": soma_proxy.execute_tool_call_async,
        "root": soma_proxy.TAEY_PRIVATE_TURN_TRACE_DIR,
        "required": soma_proxy.TAEY_TRACE_CAPTURE_REQUIRED,
        "prompts": soma_proxy._one_shot_system_prompts,
    }
    try:
        soma_proxy._http = FakeHttp()
        soma_proxy.execute_tool_call_async = fake_execute
        soma_proxy.TAEY_PRIVATE_TURN_TRACE_DIR = str(root)
        soma_proxy.TAEY_TRACE_CAPTURE_REQUIRED = True
        soma_proxy._one_shot_system_prompts = {
            "linkedin-jobs": "private integration system",
        }
        response = await soma_proxy._chat_completions_for_turn(
            {
                "stream": stream,
                "messages": [{"role": "user", "content": "run once"}],
            },
            turn,
            False,
        )
        require(response.status_code == 200, "integrated traced turn did not complete")
        if stream:
            async for _chunk in response.body_iterator:
                pass
    finally:
        soma_proxy._http = prior["_http"]
        soma_proxy.execute_tool_call_async = prior["execute"]
        soma_proxy.TAEY_PRIVATE_TURN_TRACE_DIR = prior["root"]
        soma_proxy.TAEY_TRACE_CAPTURE_REQUIRED = prior["required"]
        soma_proxy._one_shot_system_prompts = prior["prompts"]
        soma_proxy._request_context.reset(token)

    persisted = read_trace(
        root
        / f"integration-{suffix}-event"
        / f"turn_trace_integration-{suffix}-turn.json"
    )
    terminal_response = persisted["terminal_response"]
    terminal_content = (
        terminal_response["message"]["content"]
        if stream
        else terminal_response["choices"][0]["message"]["content"]
    )
    require(
        persisted["state"] == (
            "stream_complete" if stream else "nonstream_complete"
        )
        and persisted["checkpoint_index"] == (4 if stream else 3)
        and [message["role"] for message in persisted["messages"]]
        == ["system", "user", "assistant", "tool", "assistant"]
        and persisted["messages"][2]["tool_calls"][0]["id"] == "call-001"
        and persisted["messages"][3]["tool_call_id"] == "call-001"
        and terminal_content == '{"ok":true,"state":"captured"}',
        "integrated proxy turn did not preserve the full tool trajectory",
    )


def main() -> int:
    identity = {
        "turn_id": "turn-001",
        "seat_id": "seat-001",
        "event_id": "event-001",
        "correlation_id": "correlation-001",
        "tool_profile": "linkedin-unit1-prepare",
        "proxy_namespace": "taey",
        "process_generation": "generation-001",
        "started_at": 1_800_000_000.0,
    }
    initial_messages = [
        {"role": "system", "content": "private system"},
        {"role": "user", "content": "private task"},
    ]
    sequence_state = {
        "_linkedin_unit1_prepare_sequence": {
            "receipts": [],
            "terminal": None,
        },
        "_tool_profile_state": {"terminal": None},
    }
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-turn-traces"
        root.mkdir(mode=0o700)
        trace = PrivateTurnTrace.start(
            root=str(root),
            required=True,
            enabled=True,
            identity=identity,
            messages=initial_messages,
            sequence_state=sequence_state,
        )
        require(trace is not None, "required private trace did not start")
        target = root / "event-001" / "turn_trace_turn-001.json"
        initial = read_trace(target)
        require(
            initial["schema"] == "taey_private_turn_trace_v1"
            and initial["checkpoint_index"] == 0
            and initial["checkpoint_phase"] == "turn_start"
            and initial["state"] == "in_progress"
            and initial["messages"] == initial_messages,
            "initial private turn checkpoint is incomplete",
        )

        assistant = {
            "role": "assistant",
            "tool_calls": [{
                "id": "call-001",
                "type": "function",
                "function": {
                    "name": "linkedin_unit1_prepare",
                    "arguments": "{\"action\":\"observe\"}",
                },
            }],
        }
        trace.append_message(assistant)
        trace.checkpoint(
            phase="tool_intent",
            state="in_progress",
            tool_rounds=1,
            sequence_state=sequence_state,
        )
        intent = read_trace(target)
        require(
            intent["checkpoint_index"] == 1
            and intent["messages"][-1] == assistant,
            "assistant tool intent was not checkpointed",
        )

        tool_result = {
            "role": "tool",
            "tool_call_id": "call-001",
            "content": "{\"ok\":true}",
        }
        trace.append_message(tool_result)
        trace.checkpoint(
            phase="tool_result",
            state="in_progress",
            tool_rounds=1,
            sequence_state={
                **sequence_state,
                "_tool_profile_state": {"terminal": "done"},
            },
        )
        result = read_trace(target)
        require(
            result["checkpoint_index"] == 2
            and result["messages"][-1] == tool_result
            and result["sequence_state"]["_tool_profile_state"]["terminal"]
            == "done",
            "individual tool result was not checkpointed",
        )

        terminal = {"choices": [{"message": {"content": "done"}}]}
        trace.append_message({"role": "assistant", "content": "done"})
        trace.checkpoint(
            phase="turn_final",
            state="nonstream_complete",
            tool_rounds=1,
            sequence_state=trace.last_sequence_state,
            usage={"prompt_tokens": 10, "completion_tokens": 2},
            terminal_response=terminal,
        )
        final = read_trace(target)
        require(
            final["checkpoint_index"] == 3
            and final["outcome"] == "nonstream_complete"
            and final["terminal_response"] == terminal
            and final["usage"]
            == {"prompt_tokens": 10, "completion_tokens": 2}
            and "ended_at" in final
            and trace.terminal is True
            and not list(target.parent.glob(".*.tmp.*")),
            "terminal private turn checkpoint is incomplete",
        )

        failed = PrivateTurnTrace.start(
            root=str(root),
            required=True,
            enabled=True,
            identity={**identity, "turn_id": "turn-002"},
            messages=initial_messages,
            sequence_state=sequence_state,
        )
        require(failed is not None, "failure fixture did not start")
        failed_target = root / "event-001" / "turn_trace_turn-002.json"
        os.chmod(failed_target, 0o600)
        failed.append_message(assistant)
        try:
            failed.checkpoint(
                phase="tool_intent",
                state="in_progress",
                tool_rounds=1,
                sequence_state=sequence_state,
            )
        except PrivateTurnTraceError:
            pass
        else:
            raise AssertionError("unsafe trace target did not fail loud")
        require(
            failed.failed is True
            and not list(failed_target.parent.glob(".*.tmp.*")),
            "failed checkpoint left an executable trace state or temp file",
        )

        durability_failure = PrivateTurnTrace.start(
            root=str(root),
            required=True,
            enabled=True,
            identity={**identity, "turn_id": "turn-003"},
            messages=initial_messages,
            sequence_state=sequence_state,
        )
        require(
            durability_failure is not None,
            "durability failure fixture did not start",
        )
        durability_target = (
            root / "event-001" / "turn_trace_turn-003.json"
        )
        durable_before = durability_target.read_bytes()
        original_fsync = trace_module.os.fsync

        def fail_file_fsync(_descriptor: int) -> None:
            raise OSError("injected private trace file fsync failure")

        trace_module.os.fsync = fail_file_fsync
        try:
            durability_failure.append_message(assistant)
            try:
                durability_failure.checkpoint(
                    phase="tool_intent",
                    state="in_progress",
                    tool_rounds=1,
                    sequence_state=sequence_state,
                )
            except PrivateTurnTraceError:
                pass
            else:
                raise AssertionError("file fsync failure did not fail loud")
        finally:
            trace_module.os.fsync = original_fsync
        require(
            durability_failure.failed is True
            and durability_target.read_bytes() == durable_before
            and not list(durability_target.parent.glob(".*.tmp.*")),
            "failed file fsync replaced prior evidence or left a temp file",
        )
        integration_root = Path(temporary) / "integration-traces"
        integration_root.mkdir(mode=0o700)
        asyncio.run(validate_proxy_integration(integration_root, stream=False))
        asyncio.run(validate_proxy_integration(integration_root, stream=True))

    require(
        PrivateTurnTrace.start(
            root="",
            required=False,
            enabled=True,
            identity=identity,
            messages=initial_messages,
            sequence_state=sequence_state,
        ) is None,
        "optional unconfigured trace capture did not remain disabled",
    )
    try:
        PrivateTurnTrace.start(
            root="",
            required=True,
            enabled=True,
            identity=identity,
            messages=initial_messages,
            sequence_state=sequence_state,
        )
    except PrivateTurnTraceConfigurationError:
        pass
    else:
        raise AssertionError("required trace capture accepted an empty root")
    try:
        PrivateTurnTrace.start(
            root="relative/private",
            required=True,
            enabled=True,
            identity=identity,
            messages=initial_messages,
            sequence_state=sequence_state,
        )
    except PrivateTurnTraceConfigurationError:
        pass
    else:
        raise AssertionError("private trace capture accepted a relative root")

    module_source = (SERVING_ROOT / "private_turn_trace.py").read_text(
        encoding="utf-8"
    )
    atomic_source = function_source(
        SERVING_ROOT / "private_turn_trace.py",
        "_atomic_checkpoint_write",
    )
    require(
        "/home/mira" not in module_source
        and atomic_source.index("os.write(descriptor, view)")
        < atomic_source.index("os.fchmod(descriptor, 0o400)")
        < atomic_source.index("os.fsync(descriptor)")
        < atomic_source.index("os.close(descriptor)")
        < atomic_source.index("os.replace(")
        < atomic_source.index("os.fsync(parent_fd)", atomic_source.index("os.replace(")),
        "private trace storage lost portability or directory durability",
    )
    loop_source = function_source(
        SERVING_ROOT / "soma_proxy.py",
        "_chat_completions_for_turn",
    )
    intent_positions = [
        match.start()
        for match in re.finditer(
            'phase="tool_intent"',
            loop_source,
        )
    ]
    execute_positions = [
        match.start()
        for match in re.finditer(
            re.escape("await execute_tool_call_async("),
            loop_source,
        )
    ]
    result_positions = [
        match.start()
        for match in re.finditer(
            'phase="tool_result"',
            loop_source,
        )
    ]
    require(
        len(intent_positions) == len(execute_positions) == len(result_positions) == 4
        and all(
            intent < execute < result
            for intent, execute, result in zip(
                intent_positions,
                execute_positions,
                result_positions,
            )
        )
        and loop_source.index("_private_turn_trace_start(")
        < loop_source.index('await _http.post("/v1/chat/completions"'),
        "tool execution is not enclosed by exact intent/result checkpoints",
    )

    print("private turn trace checkpoint machine: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
