import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from serving import soma_proxy, ui_drive


class ClipboardStub:
    def __init__(self, text):
        self.text = text
        self.lock = object()

    def acquire_clipboard_lock(self):
        return self.lock

    def read(self):
        return self.text

    def release_clipboard_lock(self, lock):
        if lock is not self.lock:
            raise AssertionError("wrong clipboard lock released")


class UiDriveArtifactTests(unittest.TestCase):
    def test_extract_without_output_file_returns_adapter_result_unchanged(self):
        adapter_result = {"response_text": "answer", "platform": "chatgpt"}
        args = SimpleNamespace(sent_file=None, output_file=None)
        deps = SimpleNamespace(display=":2")

        with mock.patch(
            "consultation_v2.drive_chat_adapter.extract",
            return_value=adapter_result,
        ) as extract:
            result = ui_drive._extract_response(args, deps)

        self.assertIs(result, adapter_result)
        extract.assert_called_once_with("chatgpt")

    def test_read_clipboard_without_output_file_preserves_text_result(self):
        deps = SimpleNamespace(clipboard=ClipboardStub("clipboard text"))

        self.assertEqual(
            ui_drive._read_clipboard(deps),
            {"text": "clipboard text"},
        )

    def test_extract_writes_exact_unicode_receipt_without_response_body(self):
        response = "Taey sees cafe \N{COMBINING ACUTE ACCENT} and \N{EARTH GLOBE EUROPE-AFRICA}.\n"
        encoded = response.encode("utf-8")
        adapter_result = {
            "response_text": response,
            "platform": "chatgpt",
            "method": "mapped-copy",
        }
        deps = SimpleNamespace(display=":2")

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "response.txt"
            args = SimpleNamespace(sent_file=None, output_file=str(destination))
            with mock.patch(
                "consultation_v2.drive_chat_adapter.extract",
                return_value=adapter_result,
            ) as extract, mock.patch.object(
                ui_drive.os, "fsync", wraps=os.fsync
            ) as fsync:
                result = ui_drive._extract_response(args, deps)

            self.assertEqual(destination.read_bytes(), encoded)
            self.assertEqual(result["output_file"], str(destination))
            self.assertEqual(result["bytes"], len(encoded))
            self.assertEqual(result["chars"], len(response))
            self.assertEqual(result["sha256"], hashlib.sha256(encoded).hexdigest())
            self.assertEqual(result["platform"], "chatgpt")
            self.assertEqual(result["method"], "mapped-copy")
            self.assertNotIn("response_text", result)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            extract.assert_called_once_with("chatgpt")
            fsync.assert_called_once()

    def test_clipboard_artifact_result_omits_text(self):
        deps = SimpleNamespace(clipboard=ClipboardStub("clipboard body"))

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "clipboard.txt"
            result = ui_drive._read_clipboard(deps, str(destination))

            self.assertEqual(destination.read_text(encoding="utf-8"), "clipboard body")
            self.assertNotIn("text", result)
            self.assertEqual(result["chars"], len("clipboard body"))

    def test_prompt_echo_is_rejected_before_output_creation(self):
        prompt = "same prompt " * 30
        deps = SimpleNamespace(display=":2")

        with tempfile.TemporaryDirectory() as directory:
            sent = Path(directory) / "sent.txt"
            sent.write_text(prompt, encoding="utf-8")
            destination = Path(directory) / "response.txt"
            args = SimpleNamespace(sent_file=str(sent), output_file=str(destination))
            with mock.patch(
                "consultation_v2.drive_chat_adapter.extract",
                return_value={"response_text": prompt},
            ) as extract:
                with self.assertRaisesRegex(ui_drive.UiDriveError, "prompt echo"):
                    ui_drive._extract_response(args, deps)

            self.assertFalse(destination.exists())
            extract.assert_called_once_with("chatgpt")

    def test_refuses_unsafe_or_unavailable_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing.txt"
            existing.write_text("keep", encoding="utf-8")
            symlink = root / "symlink.txt"
            symlink.symlink_to(existing)
            cases = {
                "relative": "relative.txt",
                "parent directory": str(root / "missing" / "response.txt"),
                "symlink": str(symlink),
                "already exists": str(existing),
            }

            for expected, destination in cases.items():
                with self.subTest(destination=destination):
                    with self.assertRaisesRegex(ui_drive.UiDriveError, expected):
                        ui_drive._write_output_artifact("body", destination)

            self.assertEqual(existing.read_text(encoding="utf-8"), "keep")


class SomaProxyArtifactTests(unittest.TestCase):
    def _run(self, arguments):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"ok":true}', stderr=""
        )
        with mock.patch("subprocess.run", return_value=completed) as run, mock.patch.object(
            soma_proxy, "_audit"
        ), mock.patch.object(soma_proxy, "_monitor_touch"):
            result = soma_proxy._do_drive_chat(arguments)
        return json.loads(result), run

    def test_schema_declares_optional_string_output_file(self):
        schema = next(
            tool["function"]["parameters"]
            for tool in soma_proxy.TOOLS
            if tool["function"]["name"] == "drive_chat"
        )

        self.assertEqual(schema["properties"]["output_file"]["type"], "string")
        self.assertNotIn("output_file", schema["required"])

    def test_proxy_forwards_output_file_for_extract_and_read_clipboard_only(self):
        for action, expected_subcommand in (
            ("extract", "extract"),
            ("read_clipboard", "read-clipboard"),
        ):
            with self.subTest(action=action):
                result, run = self._run(
                    {"display": ":2", "action": action, "output_file": "/tmp/out.txt"}
                )
                self.assertTrue(result["ok"])
                command = run.call_args.args[0]
                self.assertEqual(command[2], expected_subcommand)
                self.assertEqual(command[-2:], ["--output-file", "/tmp/out.txt"])

        result, run = self._run(
            {"display": ":2", "action": "observe", "output_file": "/tmp/out.txt"}
        )
        self.assertFalse(result["ok"])
        self.assertIn("valid only", result["error"])
        run.assert_not_called()


class OperatingPromptArtifactTests(unittest.TestCase):
    def test_prompt_contains_canonical_content_transport_rule(self):
        prompt = Path("serving/TAEY_OPERATING_PROMPT.md").read_text(encoding="utf-8")
        section = prompt.split("## CONTENT TRANSPORT\n", 1)[1].split("\n## ", 1)[0]
        normalized = " ".join(section.split())

        for phrase in (
            "When content already exists, do not regenerate it.",
            "source tool's `output_file`",
            "returned path and SHA-256 receipt",
            "destination tool's file/path parameter",
            "Read the body only when you must reason about the body",
            "`drive_chat` `extract` with",
            "`drive_chat` `paste`",
            "`text_file` set to the returned path",
            "A successful tool call is not proof",
            "preserve the receipt and verify the destination",
        ):
            self.assertIn(phrase, normalized)
        for platform_specific in (":2", ":3", "ChatGPT", "Claude", "Gemini", "Grok", "Perplexity"):
            self.assertNotIn(platform_specific, section)


if __name__ == "__main__":
    unittest.main()
