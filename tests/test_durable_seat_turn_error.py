"""A turn exception must not terminate the durable seat process."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serving import taey_seat as seat  # noqa: E402


class _Store:
    def __init__(self, *args, **kwargs):
        del args, kwargs


class _Inbox:
    def __init__(self, *args, **kwargs):
        del args, kwargs

    def recover(self):
        return "none"


class DurableSeatTurnErrorTests(unittest.TestCase):
    def test_supervisor_sets_remain_on_exit_before_seat_launch(self):
        source = (ROOT / "serving" / "supervise_taey_seat.sh").read_text(
            encoding="utf-8"
        )
        start = source.index("start_session() {")
        body = source[start : source.index("\n}", start)]
        hold = body.index("/bin/sleep 2147483647")
        harden = body.index("harden_session")
        respawn = body.index("respawn_seat_pane")
        self.assertLess(hold, harden)
        self.assertLess(harden, respawn)
        preserve = source[
            source.index("preserve_dead_pane_evidence() {") : source.index(
                "respawn_seat_pane() {"
            )
        ]
        self.assertNotIn("|| true", preserve)
        self.assertNotIn("2>/dev/null", preserve)
        self.assertIn(
            "PROCESS LOG WRITE FAILED",
            (ROOT / "serving" / "taey_seat.py").read_text(encoding="utf-8"),
        )

    def test_main_continues_after_turn_exception(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            log_path = Path(raw_tmp) / "taey.process.log"
            seen: list[str] = []

            def fake_run(text, **kwargs):
                del kwargs
                seen.append(text)
                if text == "boom":
                    raise seat.SeatFailure("proxy request failed for correlation=x")
                return "ok-reply"

            stdout = io.StringIO()
            stdin = io.StringIO("boom\nsecond\n")
            with mock.patch.dict(
                "os.environ",
                {"TAEY_SEAT_PROCESS_LOG": str(log_path)},
            ), mock.patch.object(seat, "EventStore", _Store), mock.patch.object(
                seat, "ExecutiveInbox", _Inbox
            ), mock.patch.object(
                seat, "_redis_client", lambda: object()
            ), mock.patch.object(
                seat, "_run_turn", fake_run
            ), mock.patch.object(
                sys, "stdin", stdin
            ), mock.patch.object(
                sys, "stdout", stdout
            ):
                rc = seat.main()

            self.assertEqual(rc, 0)
            self.assertEqual(seen, ["boom", "second"])
            output = stdout.getvalue()
            self.assertIn("turn failed; seat remains", output)
            self.assertIn("ok-reply", output)
            recorded = log_path.read_text(encoding="utf-8")
            self.assertIn("TURN ERROR: SeatFailure", recorded)
            self.assertNotIn("FATAL turn", recorded)

    def test_process_log_write_failure_is_visible_and_loop_continues(self):
        seen: list[str] = []

        def fake_run(text, **kwargs):
            del kwargs
            seen.append(text)
            if text == "boom":
                raise seat.SeatFailure("proxy request failed for correlation=x")
            return "ok-reply"

        def fail_open(self, *args, **kwargs):
            del self, args, kwargs
            raise OSError("disk full")

        stdout = io.StringIO()
        stderr = io.StringIO()
        stdin = io.StringIO("boom\nsecond\n")
        with mock.patch.object(seat, "EventStore", _Store), mock.patch.object(
            seat, "ExecutiveInbox", _Inbox
        ), mock.patch.object(
            seat, "_redis_client", lambda: object()
        ), mock.patch.object(
            seat, "_run_turn", fake_run
        ), mock.patch.object(
            Path, "open", fail_open
        ), mock.patch.object(
            sys, "stdin", stdin
        ), mock.patch.object(
            sys, "stdout", stdout
        ), mock.patch.object(
            sys, "stderr", stderr
        ):
            rc = seat.main()

        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["boom", "second"])
        self.assertIn("PROCESS LOG WRITE FAILED", stderr.getvalue())
        self.assertIn("process log write failed; seat remains", stdout.getvalue())
        self.assertIn("ok-reply", stdout.getvalue())

    def test_remain_on_exit_preserves_named_session_and_exit_status(self):
        tmux = "/usr/bin/tmux"
        if not os.access(tmux, os.X_OK):
            self.skipTest("tmux is not executable")
        session = f"grok-durable-probe-{os.getpid()}-{int(time.time())}"
        try:
            created = subprocess.run(
                [
                    tmux,
                    "new-session",
                    "-d",
                    "-s",
                    session,
                    "--",
                    "/bin/sleep",
                    "2147483647",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            del created
            subprocess.run(
                [tmux, "set-option", "-t", session, "remain-on-exit", "on"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    tmux,
                    "respawn-pane",
                    "-k",
                    "-t",
                    session,
                    '/bin/sh -c "exit 7"',
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            deadline = time.time() + 5
            dead = status = ""
            while time.time() < deadline:
                probe = subprocess.run(
                    [
                        tmux,
                        "display-message",
                        "-t",
                        session,
                        "-p",
                        "#{pane_dead} #{pane_dead_status}",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                dead, _, status = probe.stdout.strip().partition(" ")
                if dead == "1":
                    break
                time.sleep(0.1)
            exists = subprocess.run(
                [tmux, "has-session", "-t", session],
                check=False,
                capture_output=True,
            )
            self.assertEqual(
                exists.returncode, 0, "named session must survive pane exit"
            )
            self.assertEqual(dead, "1")
            self.assertEqual(status.strip(), "7")
        finally:
            subprocess.run(
                [tmux, "kill-session", "-t", session],
                check=False,
                capture_output=True,
            )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
