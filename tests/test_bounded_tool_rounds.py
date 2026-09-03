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
            if hasattr(content, "__aiter__"):
                self.body_iterator = content
                self.body = b""
            elif isinstance(content, (bytes, str)):
                self.body = content
            elif content is not None:
                self.body = json.dumps(content)
            else:
                self.body = ""
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

    def _inexhaustible_mock_http(
        self,
        tool_payload: dict,
        final_payload: dict,
        max_calls: int = 20,
    ) -> mock.AsyncMock:
        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count > max_calls:
                raise AssertionError(
                    f"Runaway loop detected: call_count={call_count} exceeded limit {max_calls}"
                )
            req_body = kwargs.get("json", {})
            if req_body.get("tool_choice") == "none" or "tools" not in req_body:
                return SimpleNamespace(status_code=200, json=lambda: final_payload)
            return SimpleNamespace(status_code=200, json=lambda: tool_payload)

        http = mock.AsyncMock()
        http.post.side_effect = mock_post
        return http

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
        http = self._inexhaustible_mock_http(tool_payload, final_payload, max_calls=10)
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

        # Load-bearing assertions: exactly 2 tool calls executed + 1 final answer
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
        http = mock.AsyncMock()
        for invalid_value in invalid_types:
            with self.subTest(invalid_type=type(invalid_value), value=invalid_value):
                body = {
                    "model": "ep3",
                    "messages": [{"role": "user", "content": "test"}],
                    "max_rounds": invalid_value,
                }
                with mock.patch.object(soma_proxy, "_http", http):
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
        self.assertEqual(http.post.await_count, 0)

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
        http = mock.AsyncMock()
        for invalid_value in invalid_ranges:
            with self.subTest(value=invalid_value):
                body = {
                    "model": "ep3",
                    "messages": [{"role": "user", "content": "test"}],
                    "max_rounds": invalid_value,
                }
                with mock.patch.object(soma_proxy, "_http", http):
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
        self.assertEqual(http.post.await_count, 0)

    def test_valid_max_rounds_boundaries_accepted_without_tools(self):
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

    def test_omitted_max_rounds_terminates_at_default_proxy_ceiling(self):
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
                            "arguments": '{"query":"omitted"}',
                        },
                    }],
                },
            }],
            "usage": {"completion_tokens": 1},
        }
        final_payload = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"status":"ceiling_reached"}'},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
        ceiling = 3
        http = self._inexhaustible_mock_http(tool_payload, final_payload, max_calls=10)
        turn = self._make_turn()
        body = {
            "model": "ep3",
            "messages": [{"role": "user", "content": "omitted max_rounds"}],
            "tools": [{
                "type": "function",
                "function": {"name": "search_isma"},
            }],
        }
        # max_rounds is omitted from body

        with mock.patch.object(
            soma_proxy,
            "DEFAULT_MAX_TOOL_ROUNDS",
            ceiling,
        ), mock.patch.object(
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

        # Invariant: omission terminates at exactly ceiling rounds
        self.assertEqual(http.post.await_count, ceiling + 1)
        self.assertEqual(execute.await_count, ceiling)
        final_body = http.post.await_args_list[-1].kwargs["json"]
        self.assertNotIn("tools", final_body)
        self.assertEqual(final_body["tool_choice"], "none")
        self.assertEqual(json.loads(response.body), final_payload)

    def test_stream_request_terminates_at_default_proxy_ceiling(self):
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
                            "arguments": '{"query":"stream"}',
                        },
                    }],
                },
            }],
            "usage": {"completion_tokens": 1},
        }
        final_payload = {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "stream-final-content",
                    "reasoning": "stream-final-thinking",
                },
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
        ceiling = 2
        http = self._inexhaustible_mock_http(tool_payload, final_payload, max_calls=10)
        turn = self._make_turn()
        body = {
            "model": "ep3",
            "stream": True,
            "messages": [{"role": "user", "content": "stream tool turns"}],
            "tools": [{
                "type": "function",
                "function": {"name": "search_isma"},
            }],
        }

        with mock.patch.object(
            soma_proxy,
            "DEFAULT_MAX_TOOL_ROUNDS",
            ceiling,
        ), mock.patch.object(
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

            async def consume(resp):
                chunks = []
                async for chunk in resp.body_iterator:
                    chunks.append(chunk)
                return chunks

            chunks = asyncio.run(consume(response))

        # Invariant: stream path tool loop terminates at ceiling rounds
        self.assertEqual(http.post.await_count, ceiling + 1)
        self.assertEqual(execute.await_count, ceiling)
        final_body = http.post.await_args_list[-1].kwargs["json"]
        self.assertNotIn("tools", final_body)
        self.assertEqual(final_body["tool_choice"], "none")
        self.assertGreater(len(chunks), 0)

    def test_caller_cannot_raise_max_rounds_above_proxy_ceiling(self):
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
                            "arguments": '{"query":"bypass"}',
                        },
                    }],
                },
            }],
            "usage": {"completion_tokens": 1},
        }
        final_payload = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"status":"clamped"}'},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
        ceiling = 2
        http = self._inexhaustible_mock_http(tool_payload, final_payload, max_calls=10)
        turn = self._make_turn()
        body = {
            "model": "ep3",
            "messages": [{"role": "user", "content": "caller tries higher bound"}],
            "tools": [{
                "type": "function",
                "function": {"name": "search_isma"},
            }],
            "max_rounds": 10,
        }

        with mock.patch.object(
            soma_proxy,
            "DEFAULT_MAX_TOOL_ROUNDS",
            ceiling,
        ), mock.patch.object(
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

        # Invariant: caller request for 10 is clamped to ceiling 2
        self.assertEqual(http.post.await_count, ceiling + 1)
        self.assertEqual(execute.await_count, ceiling)
        final_body = http.post.await_args_list[-1].kwargs["json"]
        self.assertNotIn("tools", final_body)
        self.assertEqual(final_body["tool_choice"], "none")
        self.assertEqual(json.loads(response.body), final_payload)

    def test_default_ceiling_constant_value_and_headroom(self):
        self.assertEqual(soma_proxy.DEFAULT_MAX_TOOL_ROUNDS, 16)
        self.assertGreater(soma_proxy.DEFAULT_MAX_TOOL_ROUNDS, 2)  # Higher than council=2
        self.assertLess(soma_proxy.DEFAULT_MAX_TOOL_ROUNDS, 28)    # Lower than 28-round hang


if __name__ == "__main__":
    unittest.main()
