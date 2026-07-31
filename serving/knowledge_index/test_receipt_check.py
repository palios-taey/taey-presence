#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import receipt_check


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
CHECK = HERE / "receipt_check.py"
WRAPPER_CHECK = HERE / "taey-receipt-check"
WRAPPER_RESOLVE = HERE / "taey-index-resolve"
REPO = "palios-taey/taey-presence"
REPO_KEY = "palios-taey__taey-presence"
PINNED_SHA = "1111111111111111111111111111111111111111"
COMPILED_SHA = "0000000000000000000000000000000000000000"
ARTIFACT_SHA = "2222222222222222222222222222222222222222"
LIVE_COMPILED_SHA = "6d7abcd2ec540b1f27d9ae00080fb1de02acd139"
LIVE_PINNED_SHA = "82bfcfb11e437256a4773ea2ea37cf0d8ad100ef"
UNRELATED_SHA = "3333333333333333333333333333333333333333"
SURFACE_ID = "fixture-surface"

RECORDED_TRUE_ANCESTOR_COMPARE = {
    "status": "ahead",
    "ahead_by": 1,
    "behind_by": 0,
    "base_commit": {"sha": LIVE_COMPILED_SHA},
    "merge_base_commit": {"sha": LIVE_COMPILED_SHA},
}
RECORDED_INVERSE_COMPARE = {
    "status": "behind",
    "ahead_by": 0,
    "behind_by": 1,
    "base_commit": {"sha": LIVE_PINNED_SHA},
    "merge_base_commit": {"sha": LIVE_COMPILED_SHA},
}
RECORDED_DIVERGED_COMPARE = {
    "status": "diverged",
    "ahead_by": 1,
    "behind_by": 1,
    "base_commit": {"sha": LIVE_COMPILED_SHA},
    "merge_base_commit": {"sha": UNRELATED_SHA},
}


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def json_bytes(obj) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode()


def canonical_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class ReceiptFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.remote = root / "remote"
        self.index_path = root / "index.json"
        self.live_index_path = root / "live-index.json"
        self.receipt_path = "serving/receipts/fixture.liveness.json"
        self.manifest_path = "serving/manifests/fixture.artifacts.json"
        self.gates_manifest_ref = "serving/manifests/fixture-gates.json"
        self.liveness = {
            "probe_cmd": "printf '{\"ok\":true}\\n'",
            "expect": {"lang": "jq", "predicate": ".ok == true"},
        }
        artifact_raw = b"fixture artifact\n"
        self.manifest_raw = canonical_bytes({
            "surface_id": SURFACE_ID,
            "artifacts": [{"path": "artifact.txt", "sha256": sha256(artifact_raw)}],
        })
        self.manifest_sha = sha256(self.manifest_raw)
        self.gates_manifest = {
            "manifest_version": 1,
            "required_contexts": ["fixture-ci"],
            "trusted_actors": {"apps": ["github-actions"], "logins": ["trusted-bot"]},
        }
        self.compiled_at_commit = PINNED_SHA
        self.index = {
            "index_id": "taey-knowledge-index",
            "version": 1,
            "generated_at_commit": PINNED_SHA,
            "live_url": "https://example.invalid/fixture-index.json",
            "code_host_allowlist": ["github.com"],
            "sections_present": ["presence"],
            "sections_pending": [],
            "sections": {
                "presence": {
                    "capabilities": [self.capability("")],
                    "processes": [],
                }
            },
            "source_manifest": {"sections": [], "compiled_body_sha256": "fixture"},
        }
        self.write_reachable([ARTIFACT_SHA])
        self.write_gates(statuses=[
            {"context": "fixture-ci", "state": "success", "creator": {"login": "trusted-bot"}}
        ])
        self.write_remote(PINNED_SHA, self.manifest_path, self.manifest_raw)
        self.write_remote(ARTIFACT_SHA, self.gates_manifest_ref, json_bytes(self.gates_manifest))
        self.rewrite_receipt_and_index()

    def capability(self, receipt_sha: str) -> dict:
        return {
            "id": SURFACE_ID,
            "kind": "serve",
            "repo": {
                "name": REPO,
                "public_url": "https://github.com/palios-taey/taey-presence",
                "pinned_sha": PINNED_SHA,
            },
            "entry_doc": "docs/entry.md",
            "artifact_paths": ["artifact.txt"],
            "artifact_commit_sha": ARTIFACT_SHA,
            "artifact_manifest": {"path": self.manifest_path, "sha256": self.manifest_sha},
            "bootstrap": {"cmd": "true", "requires": []},
            "liveness": copy.deepcopy(self.liveness),
            "endpoints": [{"name": "fixture", "env": "FIXTURE_URL", "health": "/health"}],
            "hardware_tier": "fixture",
            "receipts": {
                "liveness": self.receipt_path,
                "usage": "serving/receipts/fixture.usage.json",
                "liveness_sha256": receipt_sha,
            },
            "status": "production",
        }

    def receipt(self) -> dict:
        return {
            "receipt_version": 2,
            "surface_id": SURFACE_ID,
            "repo": REPO,
            "artifact_commit_sha": ARTIFACT_SHA,
            "artifact_manifest_sha256": self.manifest_sha,
            "gates_manifest_ref": self.gates_manifest_ref,
            "liveness": copy.deepcopy(self.liveness),
            "index_entry_ref": SURFACE_ID,
            "compiled_at_commit": self.compiled_at_commit,
        }

    def write_remote(self, ref: str, rel_path: str, raw: bytes) -> None:
        path = self.remote / "contents" / REPO_KEY / ref / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def remove_remote(self, ref: str, rel_path: str) -> None:
        path = self.remote / "contents" / REPO_KEY / ref / rel_path
        if path.exists():
            path.unlink()

    def write_gates(self, statuses=None, check_runs=None) -> None:
        statuses = [] if statuses is None else statuses
        check_runs = {"check_runs": [] if check_runs is None else check_runs}
        status_path = self.remote / "statuses" / REPO_KEY / f"{ARTIFACT_SHA}.json"
        check_path = self.remote / "check_runs" / REPO_KEY / f"{ARTIFACT_SHA}.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        check_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(statuses))
        check_path.write_text(json.dumps(check_runs))

    def write_reachable(self, shas: list[str]) -> None:
        path = self.remote / "reachable" / f"{REPO_KEY}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(shas))

    def write_ancestors(self, ancestors_by_descendant: dict[str, list[str]]) -> None:
        path = self.remote / "ancestors" / f"{REPO_KEY}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ancestors_by_descendant))

    def rewrite_receipt_and_index(self) -> None:
        receipt_raw = json_bytes(self.receipt())
        receipt_sha = sha256(receipt_raw)
        self.write_remote(PINNED_SHA, self.receipt_path, receipt_raw)
        self.index["sections"]["presence"]["capabilities"][0] = self.capability(receipt_sha)
        raw = json_bytes(self.index)
        self.index_path.write_bytes(raw)
        self.live_index_path.write_bytes(raw)

    def rewrite_index_only(self) -> None:
        raw = json_bytes(self.index)
        self.index_path.write_bytes(raw)
        self.live_index_path.write_bytes(raw)

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["TAEY_RECEIPT_INDEX_PATH"] = str(self.index_path)
        env["TAEY_RECEIPT_LIVE_INDEX_PATH"] = str(self.live_index_path)
        env["TAEY_RECEIPT_FIXTURE_ROOT"] = str(self.remote)
        return env


class ReceiptCheckerTests(unittest.TestCase):
    def mock_compare_fetch(self, responses: dict[str, dict], default_branch: str = LIVE_PINNED_SHA):
        def fake_fetch(url: str) -> dict:
            if url == f"https://api.github.com/repos/{REPO}":
                return {"default_branch": default_branch}
            if url not in responses:
                raise AssertionError(f"unexpected URL: {url}")
            return copy.deepcopy(responses[url])

        return mock.patch.object(receipt_check, "fetch_json_url", side_effect=fake_fetch)

    def run_check(self, fixture: ReceiptFixture, expected_rc: int = 3):
        proc = subprocess.run(
            [sys.executable, str(CHECK), "check", SURFACE_ID],
            cwd=REPO_ROOT,
            env=fixture.env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, expected_rc, proc.stderr + proc.stdout)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, proc.stdout)
        return json.loads(lines[0])

    def run_wrapper_check(self, fixture: ReceiptFixture, surface_id: str = SURFACE_ID):
        proc = subprocess.run(
            [str(WRAPPER_CHECK), surface_id],
            cwd=REPO_ROOT,
            env=fixture.env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(len(proc.stdout.strip().splitlines()), 1, proc.stdout)
        return proc.returncode, json.loads(proc.stdout)

    def test_null_liveness_sha_refuses_r2_fail_closed(self) -> None:
        # ASSERTED AGAINST A FIXTURE, NOT THE LIVE INDEX.
        #
        # This test used to point TAEY_RECEIPT_INDEX_PATH at the real index.json and rely
        # on liveness_sha256 being null there. That was true only until rollout step 4
        # compiled the receipts — which was step 4's entire purpose — so the test failed
        # the moment the thing it was waiting for happened, and it failed as though the
        # receipts were wrong rather than as though the fixture had gone stale.
        #
        # The behaviour under test is real and worth keeping: a null liveness_sha256 must
        # fail CLOSED. So it is now asserted against a fixture this test controls, like
        # every other case in this battery.
        with tempfile.TemporaryDirectory() as td:
            fixture = ReceiptFixture(Path(td))
            fixture.index["sections"]["presence"]["capabilities"][0]["receipts"][
                "liveness_sha256"
            ] = None
            fixture.rewrite_index_only()
            proc = subprocess.run(
                [sys.executable, str(CHECK), "check", SURFACE_ID],
                cwd=REPO_ROOT,
                env=fixture.env(),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 3, proc.stderr + proc.stdout)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["verdict"], "REFUSE")
            self.assertEqual(payload["reason"], "binding-mismatch")
            self.assertEqual(payload["receipt_sha256"], "")

    def test_commit_ancestor_or_equal_api_accepts_github_ahead_status(self) -> None:
        responses = {
            f"https://api.github.com/repos/{REPO}/compare/{LIVE_COMPILED_SHA}...{LIVE_PINNED_SHA}":
                RECORDED_TRUE_ANCESTOR_COMPARE,
            f"https://api.github.com/repos/{REPO}/compare/{LIVE_PINNED_SHA}...{LIVE_COMPILED_SHA}":
                RECORDED_INVERSE_COMPARE,
            f"https://api.github.com/repos/{REPO}/compare/{LIVE_COMPILED_SHA}...{UNRELATED_SHA}":
                RECORDED_DIVERGED_COMPARE,
        }
        with (
            mock.patch.dict(os.environ, {receipt_check.FIXTURE_ROOT_ENV: ""}),
            self.mock_compare_fetch(responses),
        ):
            self.assertTrue(receipt_check.commit_ancestor_or_equal(REPO, LIVE_COMPILED_SHA, LIVE_PINNED_SHA))
            self.assertFalse(receipt_check.commit_ancestor_or_equal(REPO, LIVE_PINNED_SHA, LIVE_COMPILED_SHA))
            self.assertFalse(receipt_check.commit_ancestor_or_equal(REPO, LIVE_COMPILED_SHA, UNRELATED_SHA))

    def test_artifact_reachable_api_accepts_github_ahead_status(self) -> None:
        responses = {
            f"https://api.github.com/repos/{REPO}/compare/{LIVE_COMPILED_SHA}...{LIVE_PINNED_SHA}":
                RECORDED_TRUE_ANCESTOR_COMPARE,
            f"https://api.github.com/repos/{REPO}/compare/{LIVE_PINNED_SHA}...{LIVE_COMPILED_SHA}":
                RECORDED_INVERSE_COMPARE,
            f"https://api.github.com/repos/{REPO}/compare/{UNRELATED_SHA}...{LIVE_PINNED_SHA}":
                RECORDED_DIVERGED_COMPARE,
        }
        with (
            mock.patch.dict(os.environ, {receipt_check.FIXTURE_ROOT_ENV: ""}),
            self.mock_compare_fetch(responses),
        ):
            self.assertTrue(receipt_check.artifact_reachable_from_default(REPO, LIVE_COMPILED_SHA))
            self.assertFalse(receipt_check.artifact_reachable_from_default(REPO, UNRELATED_SHA))
        with (
            mock.patch.dict(os.environ, {receipt_check.FIXTURE_ROOT_ENV: ""}),
            self.mock_compare_fetch(responses, default_branch=LIVE_COMPILED_SHA),
        ):
            self.assertFalse(receipt_check.artifact_reachable_from_default(REPO, LIVE_PINNED_SHA))

    def test_full_accept_fixture_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = ReceiptFixture(Path(td))
            payload = self.run_check(fixture, expected_rc=0)
            expected_sha = (
                fixture.index["sections"]["presence"]["capabilities"][0]["receipts"]["liveness_sha256"]
            )
            self.assertEqual(payload["verdict"], "ACCEPT")
            self.assertEqual(payload["reason"], "accepted")
            self.assertEqual(payload["receipt_sha256"], expected_sha)

    def test_full_accept_fixture_allows_non_equal_compiler_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = ReceiptFixture(Path(td))
            fixture.compiled_at_commit = COMPILED_SHA
            fixture.write_ancestors({PINNED_SHA: [COMPILED_SHA]})
            fixture.rewrite_receipt_and_index()
            payload = self.run_check(fixture, expected_rc=0)
            self.assertEqual(payload["verdict"], "ACCEPT")
            self.assertEqual(payload["reason"], "accepted")

    def test_non_ancestor_compiler_commit_refuses_binding_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = ReceiptFixture(Path(td))
            fixture.compiled_at_commit = COMPILED_SHA
            fixture.rewrite_receipt_and_index()
            payload = self.run_check(fixture)
            self.assertEqual(payload["reason"], "binding-mismatch")

    def test_no_receipt_refuses_no_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = ReceiptFixture(Path(td))
            fixture.remove_remote(PINNED_SHA, fixture.receipt_path)
            payload = self.run_check(fixture)
            self.assertEqual(payload["reason"], "no-receipt")

    def test_stale_receipt_sha_refuses_binding_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = ReceiptFixture(Path(td))
            fixture.index["sections"]["presence"]["capabilities"][0]["receipts"]["liveness_sha256"] = "0" * 64
            fixture.rewrite_index_only()
            payload = self.run_check(fixture)
            self.assertEqual(payload["reason"], "binding-mismatch")
            self.assertNotEqual(payload["receipt_sha256"], "")

    def test_red_gate_refuses_gate_not_green(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = ReceiptFixture(Path(td))
            fixture.write_gates(statuses=[
                {"context": "fixture-ci", "state": "failure", "creator": {"login": "trusted-bot"}}
            ])
            payload = self.run_check(fixture)
            self.assertEqual(payload["reason"], "gate-not-green")

    def test_untrusted_actor_refuses_untrusted_actor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = ReceiptFixture(Path(td))
            fixture.write_gates(statuses=[
                {"context": "fixture-ci", "state": "success", "creator": {"login": "intruder"}}
            ])
            payload = self.run_check(fixture)
            self.assertEqual(payload["reason"], "untrusted-actor")

    def test_wrong_shape_liveness_refuses_not_live(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = ReceiptFixture(Path(td))
            fixture.liveness = {
                "probe_cmd": "printf '{\"ok\":false}\\n'",
                "expect": {"lang": "jq", "predicate": ".ok == true"},
            }
            fixture.rewrite_receipt_and_index()
            payload = self.run_check(fixture)
            self.assertEqual(payload["reason"], "not-live")

    def test_index_stale_refuses_index_stale(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = ReceiptFixture(Path(td))
            fixture.live_index_path.write_bytes(json_bytes({"stale": True}))
            payload = self.run_check(fixture)
            self.assertEqual(payload["reason"], "index-stale")
            self.assertEqual(payload["receipt_sha256"], "")

    def test_wrapper_is_runnable_and_outputs_single_json_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = ReceiptFixture(Path(td))
            fixture.remove_remote(PINNED_SHA, fixture.receipt_path)
            rc, payload = self.run_wrapper_check(fixture)
            self.assertEqual(rc, 3)
            self.assertEqual(set(payload), {"verdict", "surface_id", "reason", "checked_at", "receipt_sha256"})
            self.assertEqual(payload["reason"], "no-receipt")

    def test_resolve_exact_normalized_membership(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = ReceiptFixture(Path(td))
            proc = subprocess.run(
                [str(WRAPPER_RESOLVE), "./docs/entry.md"],
                cwd=REPO_ROOT,
                env=fixture.env(),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(json.loads(proc.stdout), {"surface_id": SURFACE_ID})

            miss = subprocess.run(
                [str(WRAPPER_RESOLVE), "docs/missing.md"],
                cwd=REPO_ROOT,
                env=fixture.env(),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(miss.returncode, 3, miss.stderr + miss.stdout)
            self.assertEqual(json.loads(miss.stdout)["reason"], "not-in-index")


if __name__ == "__main__":
    unittest.main(verbosity=2)
