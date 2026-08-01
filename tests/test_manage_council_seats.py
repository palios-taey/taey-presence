import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "serving" / "manage_council_seats.py"
SPEC = importlib.util.spec_from_file_location("manage_council_seats_under_test", MODULE_PATH)
manager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manager
SPEC.loader.exec_module(manager)


def make_seat(index=1):
    role = manager.CANONICAL_ROLES[f"taey-council-{index}"]
    return manager.SeatConfig(
        seat_id=f"taey-council-{index}",
        role_id=role,
        conversation_id=f"council-{role}",
        shared_prompt=Path("serving/council_prompts/shared.md"),
        role_prompt=Path(f"serving/council_prompts/{role}.md"),
    )


class FakeRedis:
    def __init__(self, active_turns=None, registrations=None):
        self.active_turns = active_turns or {}
        self.registrations = registrations or {}

    def zcard(self, key):
        seat_id = key.split(":")[1]
        return self.active_turns.get(seat_id, 0)

    def get(self, key):
        parts = key.split(":")
        seat_id = parts[1]
        field = parts[2]
        if field == "seat_registration":
            return self.registrations.get(seat_id)
        if field == "idle":
            return "1"
        if field == "turns_open":
            return "0"
        return None

    def llen(self, key):
        return 0


class ManageCouncilSeatsTests(unittest.TestCase):
    def test_main_error_handler_reports_original_exception_without_nameerror(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_manifest = Path(tmp) / "missing.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "validate",
                    "--manifest",
                    str(missing_manifest),
                ],
                cwd=MODULE_PATH.parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("[taey-council] FATAL: CouncilConfigError:", result.stderr)
        self.assertIn(str(missing_manifest), result.stderr)
        self.assertNotIn("NameError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_launch_refuses_existing_canonical_tmux_session_before_systemd_start(self):
        seat = make_seat()
        with (
            patch.object(manager, "_systemctl_binary", return_value="systemctl"),
            patch.object(manager.shutil, "which", return_value="tmux"),
            patch.object(manager, "_tmux_session_exists", return_value=True),
            patch.object(manager, "_redis_client") as redis_client,
            patch.object(manager.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(manager.CouncilConfigError, "canonical sessions already exist"):
                manager.launch([seat])

        redis_client.assert_not_called()
        run.assert_not_called()

    def test_launch_refuses_unit_bound_to_different_checkout_before_start(self):
        seat = make_seat()
        other_root = Path("/tmp/other-taey-presence").resolve()
        show_output = (
            "LoadState=loaded\n"
            f"WorkingDirectory={other_root}\n"
            f"EnvironmentFiles={other_root}/serving/run/council-seat-1.env "
            "(ignore_errors=no)\n"
        )
        with (
            patch.object(manager, "_systemctl_binary", return_value="systemctl"),
            patch.object(manager.shutil, "which", return_value=None),
            patch.object(manager, "_redis_client") as redis_client,
            patch.object(
                manager.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["systemctl"],
                    0,
                    stdout=show_output,
                    stderr="",
                ),
            ) as run,
        ):
            with self.assertRaisesRegex(
                manager.CouncilConfigError,
                "installed for WorkingDirectory",
            ):
                manager.launch([seat])

        redis_client.assert_not_called()
        run.assert_called_once()
        self.assertEqual(
            [
                "systemctl",
                "--user",
                "show",
                "taey-council-seat@1.service",
                "--property=LoadState",
                "--property=WorkingDirectory",
                "--property=EnvironmentFiles",
                "--no-page",
            ],
            run.call_args.args[0],
        )

    def test_launch_refuses_authoritative_active_turns_before_systemd_start(self):
        seat = make_seat()
        with (
            patch.object(manager, "_systemctl_binary", return_value="systemctl"),
            patch.object(manager.shutil, "which", return_value=None),
            patch.object(manager, "_tmux_session_exists", return_value=False),
            patch.object(manager, "_preflight_systemd_unit"),
            patch.object(manager, "_redis_client", return_value=FakeRedis(active_turns={seat.seat_id: 1})),
            patch.object(manager.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(manager.CouncilConfigError, "authoritative active turns"):
                manager.launch([seat])

        run.assert_not_called()

    def test_launch_starts_systemd_unit_and_waits_for_at_rest(self):
        seat = make_seat()
        previous_registration = '{"process_generation":"old"}'
        with (
            patch.object(manager, "_systemctl_binary", return_value="systemctl"),
            patch.object(manager.shutil, "which", return_value=None),
            patch.object(manager, "_tmux_session_exists", return_value=False),
            patch.object(manager, "_preflight_systemd_unit"),
            patch.object(
                manager,
                "_redis_client",
                return_value=FakeRedis(registrations={seat.seat_id: previous_registration}),
            ),
            patch.object(manager, "_prepare_sessions_root"),
            patch.object(manager, "_write_seat_environment_file", return_value=Path("serving/run/council-seat-1.env")),
            patch.object(manager, "_wait_for_at_rest") as wait_for_at_rest,
            patch.object(manager.subprocess, "run") as run,
            patch("builtins.print"),
        ):
            manager.launch([seat])

        run.assert_called_once_with(
            ["systemctl", "--user", "start", "taey-council-seat@1.service"],
            check=True,
        )
        wait_for_at_rest.assert_called_once()
        self.assertEqual(seat, wait_for_at_rest.call_args.args[2])
        self.assertEqual(previous_registration, wait_for_at_rest.call_args.args[3])

    def test_preflight_accepts_unit_bound_to_current_checkout(self):
        seat = make_seat()
        expected_env = manager._seat_env_file(seat).resolve()
        show_output = (
            "LoadState=loaded\n"
            f"WorkingDirectory={manager.REPO_ROOT}\n"
            f"EnvironmentFiles={expected_env} (ignore_errors=no)\n"
        )
        with patch.object(
            manager.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["systemctl"],
                0,
                stdout=show_output,
                stderr="",
            ),
        ):
            manager._preflight_systemd_unit("systemctl", seat)

    def test_status_reports_systemd_and_tmux_transport_state(self):
        seat = make_seat()
        with (
            patch.object(manager, "_systemctl_binary", return_value="systemctl"),
            patch.object(manager.shutil, "which", return_value="tmux"),
            patch.object(manager, "_tmux_session_exists", return_value=True),
            patch.object(manager, "_redis_client", return_value=FakeRedis()),
            patch.object(
                manager,
                "_unit_state",
                return_value={
                    "unit": "taey-council-seat@1.service",
                    "transport": "systemd",
                    "answered": True,
                    "active": "active",
                    "sub": "running",
                    "main_pid": "123",
                    "exec_main_status": "0",
                },
            ),
        ):
            rows = manager.status([seat])

        self.assertEqual("taey-council-seat@1.service", rows[0]["systemd"]["unit"])
        self.assertTrue(rows[0]["tmux"])
        self.assertEqual("systemd", rows[0]["transports"]["systemd"]["transport"])
        self.assertEqual("tmux", rows[0]["transports"]["tmux"]["transport"])
        self.assertTrue(rows[0]["transports"]["tmux"]["answered"])
        self.assertTrue(rows[0]["transports"]["tmux"]["exists"])
        self.assertTrue(rows[0]["environment_file"].endswith("serving/run/council-seat-1.env"))

    def test_generated_environment_file_uses_numeric_instance_and_seat_identity(self):
        seat = make_seat(3)
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp) / "run"
            sessions_root = Path(tmp) / "sessions"
            sessions_root.mkdir()
            with patch.object(manager, "RUN_ROOT", run_root):
                env_file = manager._write_seat_environment_file(seat, sessions_root)
            contents = env_file.read_text(encoding="utf-8")
            mode = env_file.stat().st_mode & 0o777

        self.assertEqual("council-seat-3.env", env_file.name)
        self.assertIn('TAEY_SESSION_NAME="taey-council-3"\n', contents)
        self.assertIn('TAEY_COUNCIL_ROLE_ID="systems-dependencies"\n', contents)
        self.assertEqual(0o600, mode)

    def test_systemd_environment_values_escape_shell_sensitive_characters(self):
        self.assertEqual('"a\\\\b\\"c\\$d\\`e"', manager._systemd_env_value('a\\b"c$d`e'))
        with self.assertRaisesRegex(manager.CouncilConfigError, "single-line"):
            manager._systemd_env_value("line1\nline2")

    def test_wait_for_at_rest_requires_active_unit_and_fresh_identity(self):
        seat = make_seat()
        fresh_registration = (
            '{"seat_id":"taey-council-1","role_id":"context-memory",'
            '"process_generation":"new"}'
        )
        redis_client = FakeRedis(registrations={seat.seat_id: fresh_registration})
        with patch.object(
            manager.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            manager._wait_for_at_rest(redis_client, "systemctl", seat, None)

        run.assert_called_with(
            [
                "systemctl",
                "--user",
                "is-active",
                "--quiet",
                "taey-council-seat@1.service",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def test_unit_template_matches_supervision_contract(self):
        unit_path = (
            Path(__file__).resolve().parents[1]
            / "serving"
            / "systemd"
            / "taey-council-seat@.service"
        )
        contents = unit_path.read_text(encoding="utf-8")

        self.assertIn("EnvironmentFile=@TAEY_ROOT@/serving/run/council-seat-%i.env", contents)
        self.assertIn("WorkingDirectory=@TAEY_ROOT@", contents)
        self.assertIn("Environment=HOME=@TAEY_HOME@", contents)
        self.assertIn("Restart=on-failure", contents)
        self.assertIn("StandardOutput=journal", contents)
        self.assertNotIn("Restart=always", contents)
        self.assertNotIn("CONTEXT WARNING", contents)
        self.assertNotIn("conversation", contents.lower())


if __name__ == "__main__":
    unittest.main()
