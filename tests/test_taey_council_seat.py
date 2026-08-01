import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVING = ROOT / "serving"
if str(SERVING) not in sys.path:
    sys.path.insert(0, str(SERVING))

os.environ["TAEY_SESSION_NAME"] = "taey-council-1"
os.environ["TAEY_COUNCIL_ROLE_ID"] = "context-memory"
os.environ["TAEY_CONVERSATION_ID"] = "council-context-memory"
os.environ["TAEY_EXECUTIVE_EVENT_LOG"] = "/tmp/taey-council-1.jsonl"

MODULE_PATH = SERVING / "taey_council_seat.py"
SPEC = importlib.util.spec_from_file_location(
    "taey_council_seat_under_test",
    MODULE_PATH,
)
taey_council_seat = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = taey_council_seat
SPEC.loader.exec_module(taey_council_seat)


class SequencedInbox:
    def __init__(self, batches):
        self.batches = [list(batch) for batch in batches]
        self.acknowledged = []
        self.requeued = []
        self.pointer_released = False

    def claim_available(self):
        if not self.batches:
            return []
        return self.batches.pop(0)

    def acknowledge(self, claims):
        self.acknowledged.extend(claims)

    def requeue(self, claims):
        self.requeued.extend(claims)

    def release_pointer(self):
        self.pointer_released = True


class FakeStore:
    def __init__(self):
        self.prompt_contract_sha256 = "contract-sha"
        self.completed_message_ids = set()
        self.events = []
        self.remembered = []

    def append(self, event_type, **fields):
        self.events.append((event_type, fields))

    def messages_for(self, prompt):
        return [{"role": "user", "content": prompt}]

    def evidence_registry(self, claims, _event_id):
        refs = ["role_contract:contract-sha"]
        refs.extend(f"fleet_message:{claim.message_id}" for claim in claims)
        return refs

    def remember_outcome(self, prompt, reply, message_ids):
        self.remembered.append((prompt, reply, message_ids))
        self.completed_message_ids.update(message_ids)


class FakeProxy:
    def __init__(self):
        self.calls = []

    def ask(self, prompt, *, event_id, correlation_id, messages, response_format):
        self.calls.append(
            {
                "prompt": prompt,
                "event_id": event_id,
                "correlation_id": correlation_id,
                "messages": messages,
                "response_format": response_format,
            }
        )
        reply = {
            "schema_version": 1,
            "seat_id": taey_council_seat.executive.SESSION,
            "role_id": taey_council_seat.ROLE_ID,
            "status": "complete",
            "prompt_revision": 1,
            "observations": ["observed inbox work"],
            "inferences": ["stdin was not needed"],
            "unknowns": ["none"],
            "evidence_refs": ["role_contract:contract-sha"],
            "concerns": [],
            "questions": [],
            "recommendation": "continue",
            "confidence": 0.9,
        }
        return taey_council_seat.executive.ProxyResult(
            reply=json.dumps(reply),
            turn_id="turn-1",
            event_id=event_id,
            correlation_id=correlation_id,
        )


def make_claim(message_id="message-1"):
    payload = {
        "msg_id": message_id,
        "from": "conductor",
        "type": "command",
        "priority": "normal",
        "timestamp": 1785564452.1390047,
        "body": "Please review the current dispatch.",
    }
    return taey_council_seat.executive.ClaimedMessage(
        source=taey_council_seat.executive.QUEUES[0],
        raw=json.dumps(payload),
        payload=payload,
        message_id=message_id,
    )


class TaeyCouncilSeatInboxLoopTests(unittest.TestCase):
    def test_inbox_loop_idles_then_serves_claim_without_tmux_input(self):
        claim = make_claim()
        inbox = SequencedInbox([[], [claim]])
        store = FakeStore()
        proxy = FakeProxy()
        sleeps = []

        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = taey_council_seat._serve_inbox_loop(
                inbox=inbox,
                store=store,
                proxy=proxy,
                poll_seconds=0.01,
                idle_sleep=sleeps.append,
                max_turns=1,
            )

        self.assertEqual(0, result)
        self.assertEqual([0.01], sleeps)
        self.assertEqual([claim.message_id], [c.message_id for c in inbox.acknowledged])
        self.assertEqual([], inbox.requeued)
        self.assertEqual(1, len(proxy.calls))
        self.assertIn("[FLEET MESSAGE", proxy.calls[0]["prompt"])
        self.assertNotIn("[TMUX OPERATOR INPUT]", proxy.calls[0]["prompt"])
        self.assertIn('"status":"complete"', output.getvalue())

    def test_main_uses_inbox_loop_when_stdin_is_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_log = Path(tmp) / "taey-council-1.jsonl"
            fake_store = mock.Mock()
            fake_store.prompt_contract_sha256 = "contract-sha"
            fake_client = object()
            fake_inbox = mock.Mock()
            fake_inbox.recover.return_value = {"recovered": 0}
            fake_proxy = object()
            with mock.patch.object(
                taey_council_seat,
                "_validate_seat_contract",
                return_value="contract",
            ), mock.patch.object(
                taey_council_seat,
                "CouncilEventStore",
                return_value=fake_store,
            ) as store_cls, mock.patch.object(
                taey_council_seat.executive,
                "_redis_client",
                return_value=fake_client,
            ), mock.patch.object(
                taey_council_seat.executive,
                "ReliableInbox",
                return_value=fake_inbox,
            ), mock.patch.object(
                taey_council_seat,
                "_register_at_rest_liveness",
                return_value=0,
            ), mock.patch.object(
                taey_council_seat.executive,
                "ProxyClient",
                return_value=fake_proxy,
            ), mock.patch.object(
                taey_council_seat,
                "_serve_inbox_loop",
                return_value=0,
            ) as serve_loop, mock.patch.object(
                taey_council_seat.executive,
                "EVENT_LOG",
                event_log,
            ), mock.patch.object(
                taey_council_seat.sys,
                "stdin",
                io.StringIO(""),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = taey_council_seat.main()

        self.assertEqual(0, result)
        store_cls.assert_called_once_with(
            event_log,
            taey_council_seat.executive.MAX_TURNS,
            "contract",
        )
        serve_loop.assert_called_once_with(
            inbox=fake_inbox,
            store=fake_store,
            proxy=fake_proxy,
            poll_seconds=taey_council_seat.DEFAULT_IDLE_POLL_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
