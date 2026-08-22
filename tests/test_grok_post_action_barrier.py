import os
import unittest
from types import SimpleNamespace
from unittest import mock

from serving import ui_drive


class GrokPostActionBarrierTests(unittest.TestCase):
    def setUp(self):
        self.ref = ui_drive._encode_ref(
            display=":5",
            platform="grok",
            scope="base",
            revision="a" * 64,
            element="retry_button",
        )
        self.args = SimpleNamespace(action="click", ref=self.ref)
        self.deps = SimpleNamespace(platform="grok", display=":5")
        self.lease = SimpleNamespace(
            seat_id="gate-seat",
            turn_id="gate-turn",
            process_generation="b" * 32,
        )
        self.action_result = {
            "performed": True,
            "performed_primitive": "click",
            "element": {
                "element": "retry_button",
                "ref": self.ref,
            },
        }

    def _run_dispatch(self, barrier_receipt):
        transition = object()
        events = []

        def resolved(*_args, **_kwargs):
            events.append("resolve")
            return transition

        def acted(*_args, **_kwargs):
            events.append("action")
            return dict(self.action_result)

        def observed(*_args, **_kwargs):
            events.append("barrier")
            return barrier_receipt

        with mock.patch.object(
            ui_drive,
            "resolve_post_action_transition",
            side_effect=resolved,
        ) as resolve, mock.patch.object(
            ui_drive,
            "_lease_context",
            return_value=self.lease,
        ), mock.patch.object(
            ui_drive,
            "_guard_action",
            return_value={"state": "owned"},
        ), mock.patch.object(
            ui_drive,
            "_element_action",
            side_effect=acted,
        ) as action, mock.patch.object(
            ui_drive,
            "run_resolved_post_action_barrier",
            side_effect=observed,
        ) as barrier, mock.patch.dict(
            os.environ,
            {"AT_SPI_BUS_ADDRESS": "unix:path=/tmp/gate-a11y"},
        ):
            result = ui_drive._dispatch(self.args, self.deps)
        return result, transition, events, resolve, action, barrier

    def test_exact_grok_retry_resolves_before_action_and_returns_pass_receipt(self):
        pass_receipt = {
            "verdict": "PASS",
            "receipt_sha256": "c" * 64,
            "next_mutation_authorized": True,
        }
        result, transition, events, resolve, action, barrier = self._run_dispatch(pass_receipt)

        self.assertEqual(events, ["resolve", "action", "barrier"])
        resolve.assert_called_once_with("grok", "usage_limit_retry")
        action.assert_called_once_with("click", self.args, self.deps)
        self.assertEqual(result["post_action_barrier"], pass_receipt)
        call = barrier.call_args
        self.assertIs(call.args[0], transition)
        lineage = call.kwargs["lineage"]
        self.assertEqual(lineage.seat_id, "gate-seat")
        self.assertEqual(lineage.turn_id, "gate-turn")
        self.assertEqual(lineage.process_generation, "b" * 32)
        self.assertEqual(lineage.display, ":5")
        self.assertEqual(lineage.atspi_bus_address, "unix:path=/tmp/gate-a11y")
        self.assertEqual(lineage.pre_action_revision, "a" * 64)
        self.assertEqual(
            call.kwargs["action_receipt"],
            {
                "action": "click",
                "element": "retry_button",
                "mutation_count": 1,
                "outcome": "applied",
                "ref": self.ref,
                "revision": "a" * 64,
            },
        )

    def test_barrier_halt_raises_with_terminal_receipt(self):
        halt_receipt = {
            "verdict": "HALT",
            "receipt_sha256": "d" * 64,
            "next_mutation_authorized": False,
        }
        with self.assertRaisesRegex(
            ui_drive.UiDriveError,
            "Grok Retry post-action barrier HALT receipt=",
        ) as raised:
            self._run_dispatch(halt_receipt)

        self.assertIn('"next_mutation_authorized":false', str(raised.exception))
        self.assertIn('"receipt_sha256":"' + ("d" * 64) + '"', str(raised.exception))

    def test_other_click_does_not_resolve_or_run_barrier(self):
        ref = ui_drive._encode_ref(
            display=":5",
            platform="grok",
            scope="base",
            revision="e" * 64,
            element="send_button",
        )
        args = SimpleNamespace(action="click", ref=ref)
        action_result = {
            "performed": True,
            "performed_primitive": "click",
            "element": {"element": "send_button", "ref": ref},
        }
        with mock.patch.object(
            ui_drive,
            "resolve_post_action_transition",
        ) as resolve, mock.patch.object(
            ui_drive,
            "_lease_context",
            return_value=self.lease,
        ), mock.patch.object(
            ui_drive,
            "_guard_action",
            return_value={"state": "owned"},
        ), mock.patch.object(
            ui_drive,
            "_element_action",
            return_value=action_result,
        ), mock.patch.object(
            ui_drive,
            "run_resolved_post_action_barrier",
        ) as barrier:
            result = ui_drive._dispatch(args, self.deps)

        self.assertEqual(result["element"]["element"], "send_button")
        resolve.assert_not_called()
        barrier.assert_not_called()


if __name__ == "__main__":
    unittest.main()
