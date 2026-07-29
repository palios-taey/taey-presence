import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "serving" / "taey_seat.py"
SPEC = importlib.util.spec_from_file_location("taey_seat_under_test", MODULE_PATH)
taey_seat = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = taey_seat
SPEC.loader.exec_module(taey_seat)


class FakeInbox:
    def __init__(self, claims):
        self.claims = claims
        self.acknowledged = []
        self.requeued = []
        self.pointer_released = False

    def claim_available(self):
        return list(self.claims)

    def acknowledge(self, claims):
        self.acknowledged.extend(claims)

    def requeue(self, claims):
        self.requeued.extend(claims)

    def release_pointer(self):
        self.pointer_released = True


class FakeStore:
    def __init__(self):
        self.events = []
        self.completed_message_ids = set()
        self.remembered = []

    def append(self, event_type, **fields):
        self.events.append((event_type, fields))

    def messages_for(self, prompt):
        return [{"role": "user", "content": prompt}]

    def remember_outcome(self, prompt, reply, message_ids):
        self.remembered.append((prompt, reply, message_ids))
        self.completed_message_ids.update(message_ids)


class FakeProxy:
    def __init__(self):
        self.calls = []

    def ask(self, prompt, *, event_id, correlation_id, messages):
        self.calls.append(
            {
                "prompt": prompt,
                "event_id": event_id,
                "correlation_id": correlation_id,
                "messages": messages,
            }
        )
        return taey_seat.ProxyResult(
            reply="handled command",
            turn_id="turn-1",
            event_id=event_id,
            correlation_id=correlation_id,
        )


def make_claim(message_id, message_type, body):
    payload = {
        "msg_id": message_id,
        "from": "conductor-codex",
        "type": message_type,
        "priority": "normal",
        "timestamp": 1785354583.984341,
        "body": body,
    }
    return taey_seat.ClaimedMessage(
        source=taey_seat.QUEUES[0],
        raw=json.dumps(payload),
        payload=payload,
        message_id=message_id,
    )


class TaeySeatNonActionableMessageTests(unittest.TestCase):
    def test_peer_idle_pointer_is_acknowledged_without_proxy_call(self):
        claim = make_claim(
            "peer-idle-conductor-codex-no-task-1785354583",
            "peer_idle",
            "conductor-codex stopped - no current task recorded",
        )
        inbox = FakeInbox([claim])
        store = FakeStore()
        proxy = FakeProxy()

        reply = taey_seat._run_turn(
            "[NOTIFY] You have 1 messages",
            inbox=inbox,
            store=store,
            proxy=proxy,
        )

        self.assertEqual(proxy.calls, [])
        self.assertEqual([claim.message_id], [c.message_id for c in inbox.acknowledged])
        self.assertEqual([], inbox.requeued)
        self.assertIn("non-actionable", reply)
        self.assertIn("peer_idle", reply)
        self.assertEqual(["turn_attempt", "turn_outcome"], [e[0] for e in store.events])
        outcome = store.events[-1][1]
        self.assertTrue(outcome["skipped_inference"])
        self.assertFalse(outcome["conversation_visible"])
        self.assertEqual([claim.message_id], outcome["message_ids"])

    def test_peer_idle_is_stripped_from_mixed_actionable_batch(self):
        idle = make_claim(
            "peer-idle-conductor-codex-no-task-1785354583",
            "peer_idle",
            "conductor-codex stopped - no current task recorded",
        )
        command = make_claim(
            "real-command-1",
            "command",
            "Read the release receipt and report the model.",
        )
        inbox = FakeInbox([idle, command])
        store = FakeStore()
        proxy = FakeProxy()

        reply = taey_seat._run_turn(
            "[NOTIFY] You have 2 messages",
            inbox=inbox,
            store=store,
            proxy=proxy,
        )

        self.assertEqual("handled command", reply)
        self.assertEqual([idle.message_id, command.message_id], [c.message_id for c in inbox.acknowledged])
        self.assertEqual(1, len(proxy.calls))
        self.assertNotIn("peer_idle", proxy.calls[0]["prompt"])
        self.assertNotIn(idle.message_id, proxy.calls[0]["prompt"])
        self.assertIn("command", proxy.calls[0]["prompt"])
        self.assertIn(command.message_id, proxy.calls[0]["prompt"])
        self.assertEqual([command.message_id], store.remembered[-1][2])

    def test_non_conversation_outcome_marks_message_done_without_context_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            records = [
                {
                    "schema_version": 1,
                    "event_type": "turn_outcome",
                    "recorded_at": 1,
                    "session": "taey",
                    "ok": True,
                    "conversation_visible": False,
                    "message_ids": ["idle-1"],
                    "prompt": "peer idle",
                    "reply": "ack",
                },
                {
                    "schema_version": 1,
                    "event_type": "turn_outcome",
                    "recorded_at": 2,
                    "session": "taey",
                    "ok": True,
                    "message_ids": ["command-1"],
                    "prompt": "real prompt",
                    "reply": "real reply",
                },
            ]
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            store = taey_seat.EventStore(path, max_turns=10)

            self.assertEqual({"idle-1", "command-1"}, store.completed_message_ids)
            messages = store.messages_for("new prompt")
            contents = [message["content"] for message in messages]
            self.assertNotIn("peer idle", contents)
            self.assertIn("real prompt", contents)
            self.assertIn("real reply", contents)


if __name__ == "__main__":
    unittest.main()
