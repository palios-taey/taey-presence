#!/usr/bin/env python3
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

try:
    from serving.outbound_request_codec import (
        bind_outbound_request_bytes,
        encode_outbound_request_bytes,
    )
except ImportError:
    from outbound_request_codec import (
        bind_outbound_request_bytes,
        encode_outbound_request_bytes,
    )


SERVING_ROOT = Path(__file__).resolve().parent
SOMA_PROXY = SERVING_ROOT / "soma_proxy.py"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def source_function(
    path: Path, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    require(len(matches) == 1, f"{name} is not one exact function")
    return matches[0]


def exec_source_function(path: Path, name: str, namespace: dict[str, object]) -> None:
    node = source_function(path, name)
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)


def extract_constant(path: Path, name: str) -> object:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for idx, node in enumerate(tree.body):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    helpers = [n for n in tree.body[:idx] if isinstance(n, ast.FunctionDef)]
                    ns = {}
                    exec(
                        compile(
                            ast.Module(body=[*helpers, node], type_ignores=[]),
                            str(path),
                            "exec",
                        ),
                        {
                            "os": __import__("os"),
                            "max": max,
                            "min": min,
                            "int": int,
                            "Optional": __import__("typing").Optional,
                            "TypeError": TypeError,
                            "ValueError": ValueError,
                        },
                        ns,
                    )
                    return ns[name]
    raise AssertionError(f"Constant {name} not found in {path}")


class FakeHTTPException(Exception):
    def __init__(self, status_code: int, detail: object):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self.payload = payload

    def json(self) -> dict:
        return deepcopy(self.payload)


class FakeHTTP:
    def __init__(self, payloads: list[dict]):
        self.payloads = list(payloads)
        self.requests: list[dict] = []

    async def post(
        self,
        path: str,
        *,
        content: bytes,
        headers: dict,
        **kwargs,
    ) -> FakeResponse:
        require(path == "/v1/chat/completions", "unexpected upstream path")
        require(
            "json" not in kwargs,
            "upstream post reconstructed json= instead of exact content bytes",
        )
        require(
            isinstance(content, (bytes, bytearray)),
            "upstream post did not send exact content bytes",
        )
        outbound = bytes(content)
        parsed = json.loads(outbound)
        bind_outbound_request_bytes(parsed, outbound)
        require(
            outbound == encode_outbound_request_bytes(parsed),
            "upstream content bytes drifted from the soma_proxy codec",
        )
        require(bool(headers.get("X-Request-Id")), "upstream lineage header missing")
        self.requests.append(deepcopy(parsed))
        require(bool(self.payloads), "unexpected extra upstream request")
        return FakeResponse(self.payloads.pop(0))


class FakeStreamingResponse:
    def __init__(self, body_iterator, **kwargs):
        self.body_iterator = body_iterator
        self.kwargs = kwargs


class FakeJSONResponse:
    def __init__(self, *, content: dict, status_code: int, headers: dict):
        self.content = content
        self.status_code = status_code
        self.headers = headers


class FakeRequestContext:
    def set(self, value: dict) -> object:
        return object()

    def reset(self, token: object) -> None:
        return None


class FakeRemoteProtocolError(Exception):
    pass


def tool_call(round_num: int) -> dict:
    return {
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call-{round_num}",
                    "function": {
                        "name": "linkedin_unit1_prepare",
                        "arguments": "{}",
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1},
    }


def final_answer(content: str) -> dict:
    return {
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": [],
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1},
    }


async def fake_execute_tool_call_async(
    name: str,
    arguments: dict,
    *,
    tool_call_id: str,
    round_num: int,
) -> str:
    require(name == "linkedin_unit1_prepare", "unexpected tool selected")
    require(arguments == {}, "unexpected tool arguments")
    require(tool_call_id == f"call-{round_num}", "tool lineage drifted")
    return json.dumps({"ok": True, "round": round_num})


def handler_namespace(fake_http: FakeHTTP) -> dict[str, object]:
    namespace: dict[str, object] = {
        "TurnContext": SimpleNamespace,
        "_private_transaction_spec_for_profile": lambda profile: None,
        "_MANUAL_CHAT_UI_TOOL_PROFILE": "manual-chat-ui",
        "_MANUAL_CHAT_UI_SEND_TOOL_PROFILE": "manual-chat-ui-send",
        "_REVENUE_UI_TOOL_PROFILE": "revenue-ui",
        "_LINKEDIN_UNIT1_TOOL_PROFILE": "linkedin-unit1",
        "_LINKEDIN_UNIT1_PREPARE_TOOL_PROFILE": "linkedin-unit1-prepare",
        "_GREENHOUSE_ATS_UI_TOOL_PROFILE": "greenhouse-ats-ui",
        "_CONSULT_CHAT_TOOL_PROFILE": "consult-chat",
        "_FULL_TOOL_PROFILE": "full",
        "_manual_chat_ui_system_prompt": "manual",
        "_manual_chat_ui_send_system_prompt": "send",
        "_revenue_ui_system_prompt": "revenue",
        "_linkedin_unit1_system_prompt": "unit1",
        "_linkedin_unit1_prepare_system_prompt": "prepare",
        "_greenhouse_ats_ui_system_prompt": "greenhouse",
        "_consult_chat_system_prompt": "consult",
        "_one_shot_system_prompts": {},
        "inject_preamble": lambda body: body,
        "TOOLS": [],
        "_tools_for_profile": lambda profile: [{
            "type": "function",
            "function": {"name": f"{profile}-tool"},
        }],
        "time": SimpleNamespace(time=lambda: 1.0),
        "httpx": SimpleNamespace(RemoteProtocolError=FakeRemoteProtocolError),
        "log": SimpleNamespace(
            warning=lambda *args: None,
            error=lambda *args: None,
            info=lambda *args: None,
        ),
        "HTTPException": FakeHTTPException,
        "_http": fake_http,
        "_upstream_headers": lambda turn: {"X-Request-Id": turn.turn_id},
        "_invalid_completion_receipt": lambda *args, **kwargs: {},
        "_audit": lambda *args, **kwargs: None,
        "execute_tool_call_async": fake_execute_tool_call_async,
        "_tool_arguments_or_terminal": lambda raw, **kwargs: json.loads(raw),
        "json": json,
        "StreamingResponse": FakeStreamingResponse,
        "BackgroundTask": lambda *args, **kwargs: None,
        "_request_context": FakeRequestContext(),
        "_turn_payload": lambda turn: {},
        "_end_turn": lambda *args, **kwargs: None,
        "publish_metrics": lambda *args, **kwargs: None,
        "_turn_headers": lambda turn: {},
        "JSONResponse": FakeJSONResponse,
        "MAX_CONTEXT_TOKENS": 262144,
        "DEFAULT_MAX_TOOL_ROUNDS": extract_constant(SOMA_PROXY, "DEFAULT_MAX_TOOL_ROUNDS"),
        "encode_outbound_request_bytes": encode_outbound_request_bytes,
        "bind_outbound_request_bytes": bind_outbound_request_bytes,
    }
    exec_source_function(SOMA_PROXY, "encode_vllm_outbound_request_bytes", namespace)
    exec_source_function(SOMA_PROXY, "_post_vllm_chat_completions", namespace)
    exec_source_function(SOMA_PROXY, "_chat_completions_for_turn", namespace)
    require(
        callable(namespace.get("encode_vllm_outbound_request_bytes")),
        "soma_proxy outbound encoder was not loaded",
    )
    require(
        callable(namespace.get("_post_vllm_chat_completions")),
        "soma_proxy outbound post helper was not loaded",
    )
    return namespace


def turn(profile: str) -> SimpleNamespace:
    return SimpleNamespace(
        turn_id="turn-1",
        seat_id="seat-1",
        event_id="event-1",
        correlation_id="correlation-1",
        tool_profile=profile,
        proxy_namespace="proxy-1",
        process_generation="a" * 32,
        started_at=1.0,
    )


async def run_prepare(stream: bool) -> list[dict]:
    payloads = [tool_call(1), tool_call(2), tool_call(3), final_answer("probe")]
    if not stream:
        payloads.append(final_answer("schema-final"))
    fake_http = FakeHTTP(payloads)
    namespace = handler_namespace(fake_http)
    body = {
        "stream": stream,
        "messages": [{"role": "user", "content": "prepare"}],
        "chat_template_kwargs": {
            "enable_thinking": True,
            "unrelated_template_option": "preserve-me",
        },
    }
    if not stream:
        body["response_format"] = {"type": "json_object"}
    response = await namespace["_chat_completions_for_turn"](
        body,
        turn("linkedin-unit1-prepare"),
        False,
    )
    if stream:
        async for _chunk in response.body_iterator:
            pass
    require(not fake_http.payloads, "not every expected upstream response was consumed")
    require(len(fake_http.requests) == (4 if stream else 5), "round count drifted")
    return fake_http.requests


async def run_unchanged_profile(profile: str) -> dict:
    fake_http = FakeHTTP([final_answer(profile)])
    namespace = handler_namespace(fake_http)
    body = {
        "stream": False,
        "messages": [{"role": "user", "content": profile}],
        "chat_template_kwargs": {
            "enable_thinking": True,
            "unrelated_template_option": "preserve-me",
        },
    }
    if profile == "full":
        body["tools"] = []
    await namespace["_chat_completions_for_turn"](body, turn(profile), False)
    require(len(fake_http.requests) == 1, f"{profile} request count drifted")
    return fake_http.requests[0]


async def validate() -> None:
    expected = {
        "enable_thinking": False,
        "unrelated_template_option": "preserve-me",
    }
    for stream in (True, False):
        requests = await run_prepare(stream)
        require(
            all(request.get("chat_template_kwargs") == expected for request in requests),
            f"prepare stream={stream} did not force thinking off on every request",
        )

    for profile in ("full", "revenue-ui"):
        request = await run_unchanged_profile(profile)
        require(
            request.get("chat_template_kwargs")
            == {
                "enable_thinking": True,
                "unrelated_template_option": "preserve-me",
            },
            f"{profile} inference policy changed",
        )

    fake_http = FakeHTTP([])
    namespace = handler_namespace(fake_http)
    try:
        await namespace["_chat_completions_for_turn"](
            {
                "stream": False,
                "messages": [{"role": "user", "content": "prepare"}],
                "chat_template_kwargs": "invalid",
            },
            turn("linkedin-unit1-prepare"),
            False,
        )
    except FakeHTTPException as exc:
        require(exc.status_code == 400, "invalid kwargs did not fail as bad request")
        require(
            exc.detail == "chat_template_kwargs must be an object",
            "invalid kwargs refusal changed",
        )
    else:
        raise AssertionError("invalid prepare chat_template_kwargs was accepted")
    require(not fake_http.requests, "invalid kwargs reached the upstream model")


def main() -> int:
    asyncio.run(validate())
    print("linkedin prepare thinking policy: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
