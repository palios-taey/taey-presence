import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from serving import soma_proxy


class BoundedToolRoundTests(unittest.TestCase):
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
        turn = soma_proxy.TurnContext(
            turn_id="turn-bounded",
            seat_id="taey-council-1",
            event_id="event-bounded",
            correlation_id="round-bounded",
            tool_profile=soma_proxy._FULL_TOOL_PROFILE,
            proxy_namespace="worker",
            process_generation="generation",
            started_at=1.0,
        )
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


if __name__ == "__main__":
    unittest.main()
