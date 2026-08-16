import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from serving import soma_proxy


class EmptyTerminalCompletionTests(unittest.TestCase):
    def test_receipt_contains_shape_and_hashes_but_not_body(self):
        payload = {
            "id": "completion-1",
            "choices": [{
                "finish_reason": "length",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": None,
                    "tool_calls": [],
                },
            }],
            "usage": {
                "prompt_tokens": 242000,
                "completion_tokens": 6800,
                "total_tokens": 248800,
            },
        }

        receipt = soma_proxy._invalid_completion_receipt(
            payload,
            upstream_status=200,
        )

        self.assertEqual(receipt["error"], "upstream_terminal_completion_empty")
        self.assertEqual(receipt["finish_reason"], "length")
        self.assertEqual(receipt["tool_call_count"], 0)
        self.assertEqual(receipt["content"]["chars"], 0)
        self.assertEqual(receipt["usage"]["completion_tokens"], 6800)
        serialized = soma_proxy.json.dumps(receipt)
        self.assertNotIn("completion-1", serialized)
        self.assertNotIn("242000 characters", serialized)

    def test_empty_probe_fails_before_a_second_upstream_generation(self):
        payload = {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning": None,
                    "tool_calls": [],
                },
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        response = SimpleNamespace(
            status_code=200,
            json=lambda: payload,
        )
        http = mock.AsyncMock()
        http.post.return_value = response
        turn = soma_proxy.TurnContext(
            turn_id="turn-empty",
            seat_id="taey-main",
            event_id="event-empty",
            correlation_id="correlation-empty",
            process_generation="generation",
            started_at=1.0,
        )
        body = {
            "stream": True,
            "messages": [{"role": "user", "content": "real production work"}],
            "tools": [{"type": "function", "function": {"name": "run_command"}}],
        }

        with mock.patch.object(soma_proxy, "_http", http), mock.patch.object(
            soma_proxy, "inject_preamble", side_effect=lambda value: value
        ), mock.patch.object(soma_proxy, "_audit") as audit:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    soma_proxy._chat_completions_for_turn(
                        body,
                        turn,
                        liveness_registered=False,
                    )
                )

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(http.post.await_count, 1)
        event, receipt = audit.call_args.args
        self.assertEqual(event, "upstream_invalid_completion")
        self.assertEqual(receipt["finish_reason"], "stop")
        self.assertEqual(receipt["usage"]["completion_tokens"], 50)


if __name__ == "__main__":
    unittest.main()
