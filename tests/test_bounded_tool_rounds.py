import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

# Install import-only stubs if dependencies are absent in CI runner
if importlib.util.find_spec("fastapi") is None:
    fastapi_stub = ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = ""):
            self.status_code = status_code
            self.detail = detail
            super().__init__(f"{status_code}: {detail}")

    class _StubApp:
        def __init__(self, *args, **kwargs):
            pass

        def on_event(self, *args, **kwargs):
            return lambda fn: fn

        def get(self, *args, **kwargs):
            return lambda fn: fn

        def post(self, *args, **kwargs):
            return lambda fn: fn

        def middleware(self, *args, **kwargs):
            return lambda fn: fn

        def add_middleware(self, *args, **kwargs):
            pass

    class _StubRequest:
        def __init__(self, *args, **kwargs):
            pass

    fastapi_stub.HTTPException = HTTPException
    fastapi_stub.FastAPI = _StubApp
    fastapi_stub.Request = _StubRequest

    fastapi_responses = ModuleType("fastapi.responses")

    class _StubResponse:
        def __init__(self, content=None, *args, **kwargs):
            self.content = content
            self.body = content if isinstance(content, (bytes, str)) else json.dumps(content) if content is not None else ""
            self.status_code = kwargs.get("status_code", 200)

    fastapi_responses.StreamingResponse = _StubResponse
    fastapi_responses.JSONResponse = _StubResponse

    sys.modules["fastapi"] = fastapi_stub
    sys.modules["fastapi.responses"] = fastapi_responses
else:
    from fastapi import HTTPException

if importlib.util.find_spec("httpx") is None:
    httpx_stub = ModuleType("httpx")

    class _StubAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

    class _StubClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    httpx_stub.AsyncClient = _StubAsyncClient
    httpx_stub.Client = _StubClient
    httpx_stub.Response = object
    httpx_stub.Request = object
    httpx_stub.TimeoutException = type("TimeoutException", (Exception,), {})
    httpx_stub.RequestError = type("RequestError", (Exception,), {})
    httpx_stub.RemoteProtocolError = type("RemoteProtocolError", (Exception,), {})
    sys.modules["httpx"] = httpx_stub

if importlib.util.find_spec("redis") is None:
    redis_stub = ModuleType("redis")

    class _StubRedis:
        def __init__(self, *args, **kwargs):
            pass

        @classmethod
        def from_url(cls, *args, **kwargs):
            return cls()

    redis_stub.Redis = _StubRedis
    sys.modules["redis"] = redis_stub

if importlib.util.find_spec("starlette") is None or importlib.util.find_spec("starlette.background") is None:
    starlette_stub = ModuleType("starlette")
    starlette_bg = ModuleType("starlette.background")

    class _StubBackgroundTask:
        def __init__(self, *args, **kwargs):
            pass

    starlette_bg.BackgroundTask = _StubBackgroundTask
    sys.modules["starlette"] = starlette_stub
    sys.modules["starlette.background"] = starlette_bg

if importlib.util.find_spec("uvicorn") is None:
    sys.modules["uvicorn"] = ModuleType("uvicorn")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "serving") not in sys.path:
    sys.path.insert(0, str(ROOT / "serving"))

from serving import soma_proxy


class BoundedToolRoundTests(unittest.TestCase):
    def _make_turn(self) -> soma_proxy.TurnContext:
        return soma_proxy.TurnContext(
            turn_id="turn-bounded",
            seat_id="taey-council-1",
            event_id="event-bounded",
            correlation_id="round-bounded",
            tool_profile=soma_proxy._FULL_TOOL_PROFILE,
            proxy_namespace="worker",
            process_generation="generation",
            started_at=1.0,
        )

    def test_nonstream_request_forces_final_answer_at_round_limit(self):
        tool_payload = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-1",
                        "function": {
                            "name": "search_isma",
                            "arguments": '{"query":"bounded"}',
                        },
                    }],
                },
            }],
            "usage": {"completion_tokens": 1},
        }
        final_payload = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"status":"ok"}'},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
        responses = [
            SimpleNamespace(status_code=200, json=lambda: tool_payload),
            SimpleNamespace(status_code=200, json=lambda: tool_payload),
            SimpleNamespace(status_code=200, json=lambda: final_payload),
        ]
        http = mock.AsyncMock()
        http.post.side_effect = responses
        turn = self._make_turn()
        body = {
            "model": "ep3",
            "messages": [{"role": "user", "content": "bounded contribution"}],
            "tools": [{
                "type": "function",
                "function": {"name": "search_isma"},
            }],
            "response_format": {"type": "json_schema"},
            "max_rounds": 2,
        }

        with mock.patch.object(
            soma_proxy,
            "inject_preamble",
            side_effect=lambda value: value,
        ), mock.patch.object(
            soma_proxy,
            "_http",
            http,
        ), mock.patch.object(
            soma_proxy,
            "execute_tool_call_async",
            new=mock.AsyncMock(return_value="evidence"),
        ) as execute:
            response = asyncio.run(
                soma_proxy._chat_completions_for_turn(
                    body,
                    turn,
                    liveness_registered=False,
                )
            )

        self.assertEqual(http.post.await_count, 3)
        self.assertEqual(execute.await_count, 2)
        first_body = http.post.await_args_list[0].kwargs["json"]
        final_body = http.post.await_args_list[2].kwargs["json"]
        self.assertNotIn("max_rounds", first_body)
        self.assertIn("tools", first_body)
        self.assertNotIn("tools", final_body)
        self.assertEqual(final_body["tool_choice"], "none")
        self.assertEqual(final_body["response_format"], {"type": "json_schema"})
        self.assertEqual(json.loads(response.body), final_payload)

    def test_invalid_max_rounds_type_raises_http_422(self):
        turn = self._make_turn()
        invalid_types = [
            True,
            False,
            "2",
            "invalid",
            1.5,
            2.0,
            [2],
            {"rounds": 2},
        ]
        for invalid_value in invalid_types:
            with self.subTest(invalid_type=type(invalid_value), value=invalid_value):
                body = {
                    "model": "ep3",
                    "messages": [{"role": "user", "content": "test"}],
                    "max_rounds": invalid_value,
                }
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(
                        soma_proxy._chat_completions_for_turn(
                            body,
                            turn,
                            liveness_registered=False,
                        )
                    )
                self.assertEqual(ctx.exception.status_code, 422)
                self.assertEqual(
                    ctx.exception.detail,
                    "max_rounds must be an integer from 1 through 32",
                )

    def test_invalid_max_rounds_range_raises_http_422(self):
        turn = self._make_turn()
        invalid_ranges = [
            0,
            -1,
            -10,
            33,
            34,
            100,
        ]
        for invalid_value in invalid_ranges:
            with self.subTest(value=invalid_value):
                body = {
                    "model": "ep3",
                    "messages": [{"role": "user", "content": "test"}],
                    "max_rounds": invalid_value,
                }
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(
                        soma_proxy._chat_completions_for_turn(
                            body,
                            turn,
                            liveness_registered=False,
                        )
                    )
                self.assertEqual(ctx.exception.status_code, 422)
                self.assertEqual(
                    ctx.exception.detail,
                    "max_rounds must be an integer from 1 through 32",
                )

    def test_valid_max_rounds_boundaries_accepted(self):
        final_payload = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "done"},
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }
        for valid_value in (1, 32, None):
            with self.subTest(max_rounds=valid_value):
                http = mock.AsyncMock()
                http.post.return_value = SimpleNamespace(status_code=200, json=lambda: final_payload)
                turn = self._make_turn()
                body = {
                    "model": "ep3",
                    "messages": [{"role": "user", "content": "test"}],
                }
                if valid_value is not None:
                    body["max_rounds"] = valid_value

                with mock.patch.object(
                    soma_proxy,
                    "inject_preamble",
                    side_effect=lambda value: value,
                ), mock.patch.object(
                    soma_proxy,
                    "_http",
                    http,
                ):
                    response = asyncio.run(
                        soma_proxy._chat_completions_for_turn(
                            body,
                            turn,
                            liveness_registered=False,
                        )
                    )
                self.assertEqual(http.post.await_count, 1)
                sent_body = http.post.await_args.kwargs["json"]
                self.assertNotIn("max_rounds", sent_body)
                self.assertEqual(json.loads(response.body), final_payload)

    def test_proxy_ceiling_default_is_production_finite_bound(self):
        self.assertEqual(soma_proxy.PROXY_MAX_TOOL_ROUNDS, 32)
        self.assertEqual(soma_proxy.load_proxy_max_tool_rounds("32"), 32)
        self.assertEqual(soma_proxy.load_proxy_max_tool_rounds("1"), 1)
        self.assertEqual(soma_proxy.load_proxy_max_tool_rounds(None), 32)
        for invalid in ("", "0", "33", "16x", "2.0"):
            with self.subTest(raw=invalid):
                with self.assertRaises(ValueError):
                    soma_proxy.load_proxy_max_tool_rounds(invalid)

    def test_omitted_max_rounds_terminates_at_proxy_ceiling(self):
        tool_payload = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-1",
                        "function": {
                            "name": "search_isma",
                            "arguments": '{"query":"bounded"}',
                        },
                    }],
                },
            }],
            "usage": {"completion_tokens": 1},
        }
        final_payload = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"status":"ok"}'},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
        ceiling = 3
        responses = [
            SimpleNamespace(status_code=200, json=lambda: tool_payload)
            for _ in range(ceiling)
        ] + [SimpleNamespace(status_code=200, json=lambda: final_payload)]
        http = mock.AsyncMock()
        http.post.side_effect = responses
        body = {
            "model": "ep3",
            "messages": [{"role": "user", "content": "omitted ceiling"}],
            "tools": [{
                "type": "function",
                "function": {"name": "search_isma"},
            }],
            "response_format": {"type": "json_schema"},
        }
        with mock.patch.object(
            soma_proxy, "PROXY_MAX_TOOL_ROUNDS", ceiling
        ), mock.patch.object(
            soma_proxy, "inject_preamble", side_effect=lambda value: value
        ), mock.patch.object(
            soma_proxy, "_http", http
        ), mock.patch.object(
            soma_proxy,
            "execute_tool_call_async",
            new=mock.AsyncMock(return_value="evidence"),
        ) as execute:
            response = asyncio.run(
                soma_proxy._chat_completions_for_turn(
                    body,
                    self._make_turn(),
                    liveness_registered=False,
                )
            )
        self.assertEqual(http.post.await_count, ceiling + 1)
        self.assertEqual(execute.await_count, ceiling)
        final_body = http.post.await_args_list[-1].kwargs["json"]
        self.assertNotIn("tools", final_body)
        self.assertEqual(final_body["tool_choice"], "none")
        self.assertEqual(json.loads(response.body), final_payload)

    def test_stream_omitted_max_rounds_terminates_at_proxy_ceiling(self):
        tool_payload = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-1",
                        "function": {
                            "name": "search_isma",
                            "arguments": '{"query":"bounded"}',
                        },
                    }],
                },
            }],
            "usage": {"completion_tokens": 1},
        }
        final_payload = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "stream-final"},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
        ceiling = 3
        responses = [
            SimpleNamespace(status_code=200, json=lambda: tool_payload)
            for _ in range(ceiling)
        ] + [SimpleNamespace(status_code=200, json=lambda: final_payload)]
        http = mock.AsyncMock()
        http.post.side_effect = responses
        body = {
            "model": "ep3",
            "stream": True,
            "messages": [{"role": "user", "content": "omitted stream ceiling"}],
            "tools": [{
                "type": "function",
                "function": {"name": "search_isma"},
            }],
        }
        async def _run():
            response = await soma_proxy._chat_completions_for_turn(
                body,
                self._make_turn(),
                liveness_registered=False,
            )
            async for _chunk in response.body_iterator:
                pass
            return response

        with mock.patch.object(
            soma_proxy, "PROXY_MAX_TOOL_ROUNDS", ceiling
        ), mock.patch.object(
            soma_proxy, "inject_preamble", side_effect=lambda value: value
        ), mock.patch.object(
            soma_proxy, "_http", http
        ), mock.patch.object(
            soma_proxy,
            "execute_tool_call_async",
            new=mock.AsyncMock(return_value="evidence"),
        ) as execute:
            asyncio.run(_run())
        self.assertEqual(http.post.await_count, ceiling + 1)
        self.assertEqual(execute.await_count, ceiling)
        last_probe = http.post.await_args_list[-1].kwargs["json"]
        self.assertNotIn("tools", last_probe)
        self.assertEqual(last_probe.get("tool_choice"), "none")

    def test_caller_cannot_raise_above_proxy_ceiling(self):
        turn = self._make_turn()
        body = {
            "model": "ep3",
            "messages": [{"role": "user", "content": "raise"}],
            "max_rounds": 16,
        }
        with mock.patch.object(soma_proxy, "PROXY_MAX_TOOL_ROUNDS", 8):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    soma_proxy._chat_completions_for_turn(
                        body,
                        turn,
                        liveness_registered=False,
                    )
                )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail,
            "max_rounds must be an integer from 1 through 8",
        )


if __name__ == "__main__":
    unittest.main()
