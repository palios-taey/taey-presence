"""Mechanical contract for record-only consultation terminal receipts."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serving import taey_seat as seat  # noqa: E402


class _Inbox:
    def __init__(self, claims: list[seat.ClaimedMessage], operations: list[str]):
        self.claims = claims
        self.operations = operations

    def claim_available(self) -> list[seat.ClaimedMessage]:
        return list(self.claims)

    def acknowledge(self, claims: list[seat.ClaimedMessage]) -> None:
        self.operations.append(
            "ack:" + ",".join(claim.message_id for claim in claims)
        )

    def release_pointer(self) -> None:
        self.operations.append("release_pointer")


class _Store(seat.EventStore):
    def __init__(self, path: Path, operations: list[str]):
        self.operations = operations
        super().__init__(path, max_turns=10)

    def append(self, event_type: str, **fields: object) -> None:
        super().append(event_type, **fields)
        self.operations.append(f"event:{event_type}")


class _NoProxy:
    def ask(self, *args: object, **kwargs: object) -> seat.ProxyResult:
        del args, kwargs
        raise AssertionError("record-only receipt must not invoke the model")


def _claim(
    *,
    sender: str = "consult-monitor",
    message_type: str = "result",
    body: object,
    message_id: str = "receipt-1",
) -> seat.ClaimedMessage:
    source = seat.QueueSpec(
        name="notifications",
        queue_key="queue",
        processing_key="processing",
        source_side="LEFT",
        processing_side="RIGHT",
        requeue_side="LEFT",
    )
    return seat.ClaimedMessage(
        source=source,
        raw=json.dumps({"from": sender, "type": message_type, "body": body}),
        payload={"from": sender, "type": message_type, "body": body},
        message_id=message_id,
    )


def _success_receipt() -> dict[str, object]:
    return {
        "schema": seat.CONSULT_TERMINAL_RECEIPT_SCHEMA,
        "monitor_id": "monitor-gemini-r2",
        "platform": "gemini",
        "display": ":4",
        "extraction_status": "succeeded",
        "terminal": True,
        "response_file": "/tmp/response.md",
        "bytes": 2746,
        "sha": "a" * 64,
        "request_json": "/tmp/request.json",
        "headers": "/tmp/headers.json",
        "response_json": "/tmp/response.json",
        "event": "event-1",
        "correlation": "correlation-1",
    }


class ConsultTerminalReceiptTests(unittest.TestCase):
    def test_valid_receipt_is_fsynced_before_ack_without_inference(self) -> None:
        operations: list[str] = []
        receipt = _success_receipt()
        claim = _claim(body=json.dumps(receipt, sort_keys=True))
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = Path(raw_tmp) / "main.jsonl"
            store = _Store(path, operations)
            reply = seat._run_turn(
                "[NOTIFY] You have 1 message",
                inbox=_Inbox([claim], operations),
                store=store,
                proxy=_NoProxy(),
            )

            self.assertIn("recorded 1 consultation terminal receipt", reply)
            self.assertEqual(
                operations,
                ["event:turn_attempt", "event:turn_outcome", "ack:receipt-1"],
            )
            events = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(events), 2)
            self.assertTrue(events[0]["skipped_inference"])
            self.assertEqual(events[0]["record_only_receipts"], [receipt])
            self.assertFalse(events[1]["conversation_visible"])
            self.assertEqual(events[1]["kind"], "consult_terminal_receipt")
            self.assertIn("receipt-1", seat.EventStore(path, 10).completed_message_ids)

    def test_only_exact_valid_tuple_is_record_only(self) -> None:
        receipt = _success_receipt()
        cases = (
            _claim(sender="other", body=json.dumps(receipt)),
            _claim(sender=" consult-monitor", body=json.dumps(receipt)),
            _claim(message_type="status", body=json.dumps(receipt)),
            _claim(message_type="RESULT", body=json.dumps(receipt)),
            _claim(body="not-json"),
            _claim(body=json.dumps({**receipt, "terminal": False})),
            _claim(body=json.dumps({**receipt, "sha": "not-a-sha"})),
        )
        for claim in cases:
            with self.subTest(payload=claim.payload):
                actionable, record_only = seat._split_record_only_receipts([claim])
                self.assertEqual(actionable, [claim])
                self.assertEqual(record_only, [])

    def test_failed_receipt_requires_error(self) -> None:
        failed = {
            "schema": seat.CONSULT_TERMINAL_RECEIPT_SCHEMA,
            "monitor_id": "monitor-claude-r1",
            "platform": "claude",
            "display": ":3",
            "extraction_status": "failed",
            "terminal": True,
            "error": "authentication required",
        }
        valid = _claim(body=json.dumps(failed))
        invalid = _claim(body=json.dumps({**failed, "error": ""}))
        self.assertEqual(len(seat._split_record_only_receipts([valid])[1]), 1)
        self.assertEqual(seat._split_record_only_receipts([invalid])[1], [])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
