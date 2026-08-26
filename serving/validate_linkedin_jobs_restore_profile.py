#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_PATH = REPO_ROOT / "serving" / "soma_proxy.py"
PROMPT_PATH = REPO_ROOT / "serving" / "TAEY_LINKEDIN_JOBS_RESTORE_SYSTEM.md"
RESULT_KEYS = {
    "ok",
    "platform",
    "display",
    "state",
    "failure_code",
    "target_url_sha256",
    "firefox_pid_sha256",
    "restore_proof_sha256",
    "stable_cycles_observed",
    "receipt_sha256",
    "turn_lineage_sha256",
}


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def load_proxy(environment: dict[str, str]) -> dict:
    prior = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    module = types.ModuleType("linkedin_jobs_restore_profile_validation")
    module.__file__ = str(PROXY_PATH)
    sys.modules[module.__name__] = module
    try:
        source = PROXY_PATH.read_text(encoding="utf-8")
        exec(compile(source, str(PROXY_PATH), "exec"), module.__dict__)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module.__dict__


def lineage_sha256(context: dict) -> str:
    return hashlib.sha256(canonical_bytes({
        "correlation_id": context["correlation_id"],
        "process_generation": context["process_generation"],
        "requester": context["seat_id"],
        "turn_id": context["turn_id"],
    })).hexdigest()


def result(
    *,
    lineage: str = "a" * 64,
    receipt: str = "b" * 64,
    state: str = "restored",
    ok: bool = True,
    failure_code: str | None = None,
    target_url_sha256: str | None = "c" * 64,
    firefox_pid_sha256: str | None = "d" * 64,
    restore_proof_sha256: str | None = "e" * 64,
    stable_cycles_observed: int = 2,
) -> dict:
    return {
        "ok": ok,
        "platform": "linkedin",
        "display": ":18",
        "state": state,
        "failure_code": failure_code,
        "target_url_sha256": target_url_sha256,
        "firefox_pid_sha256": firefox_pid_sha256,
        "restore_proof_sha256": restore_proof_sha256,
        "stable_cycles_observed": stable_cycles_observed,
        "receipt_sha256": receipt,
        "turn_lineage_sha256": lineage,
    }


def prepare_private_root(root: Path, seat: str, correlation: str) -> Path:
    root.mkdir(mode=0o700)
    for section in ("transactions", "claims", "receipts"):
        section_path = root / section
        section_path.mkdir(mode=0o700)
        (section_path / seat).mkdir(mode=0o700)
    transaction = root / "transactions" / seat / f"{correlation}.json"
    transaction.write_bytes(canonical_bytes({
        "operation": "restore_linkedin_jobs_surface",
        "return_url": "https://www.linkedin.com/jobs/search-results/?keywords=validator",
        "schema": "linkedin_jobs_restore_private_input_v1",
    }))
    transaction.chmod(0o400)
    return transaction


def validate_static_boundary(namespace: dict) -> None:
    profile = namespace["_LINKEDIN_JOBS_RESTORE_TOOL_PROFILE"]
    assert profile == "linkedin-jobs-restore"
    assert namespace["_TOOL_PROFILE_ALLOWED"][profile] == frozenset(
        {"restore_linkedin_jobs_surface"}
    )
    tools = namespace["_tools_for_profile"](profile)
    assert len(tools) == 1
    tool = tools[0]["function"]
    assert tool["name"] == "restore_linkedin_jobs_surface"
    assert tool["parameters"] == {
        "type": "object",
        "additionalProperties": False,
        "required": ["display"],
        "properties": {
            "display": {
                "type": "string",
                "pattern": "^:[0-9]{1,3}$",
                "description": "runtime-authorized LinkedIn display supplied by the user",
            },
        },
    }
    spec = namespace["_private_transaction_spec_for_tool"](
        "restore_linkedin_jobs_surface"
    )
    assert spec.profile == profile
    assert spec.runner_name == "run_linkedin_jobs_restore.py"
    assert spec.claim_schema == "linkedin_jobs_restore_claim_v1"
    assert spec.expected_result_keys == frozenset(RESULT_KEYS)
    assert spec.python_env_name == "TAEY_LINKEDIN_JOBS_RESTORE_PYTHON"
    assert spec.private_root_env_name == "TAEY_LINKEDIN_JOBS_RESTORE_PRIVATE_ROOT"
    assert spec.displays_env_name == "TAEY_LINKEDIN_JOBS_RESTORE_DISPLAYS"
    assert spec.timeout_env_name == "TAEY_LINKEDIN_JOBS_RESTORE_TIMEOUT_SECS"
    assert spec.displays == (":18",)
    assert spec.timeout_secs == 1800 and spec.deadline_secs == 1700

    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "Call `restore_linkedin_jobs_surface` exactly once" in prompt
    assert "exact parent-frozen Jobs search-results URL" in prompt
    assert "Never retry" in prompt

    tree = ast.parse(PROXY_PATH.read_text(encoding="utf-8"))
    wrapper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_do_linkedin_jobs_restore"
    )
    wrapper_source = ast.get_source_segment(
        PROXY_PATH.read_text(encoding="utf-8"), wrapper
    )
    assert wrapper_source is not None
    assert "_do_private_transaction" in wrapper_source
    for forbidden in ("UI_DRIVE", "display_lock", "subprocess", "lease"):
        assert forbidden not in wrapper_source

    original = namespace["_do_linkedin_jobs_restore"]
    namespace["_do_linkedin_jobs_restore"] = lambda arguments: json.dumps(arguments)
    token = namespace["_request_context"].set({
        "tool_profile": profile,
        "_tool_profile_state": {"terminal": None},
    })
    try:
        assert json.loads(namespace["execute_tool_call"](
            "restore_linkedin_jobs_surface", {"display": ":18"}
        )) == {"display": ":18"}
        refusal = namespace["execute_tool_call"]("linkedin_jobs", {"display": ":18"})
        assert "not available in profile" in refusal
    finally:
        namespace["_request_context"].reset(token)
        namespace["_do_linkedin_jobs_restore"] = original


def validate_result_contract(namespace: dict) -> None:
    validate = namespace["_linkedin_jobs_restore_result_error"]
    assert validate(result(), 0) is None
    for code in (
        "deadline_expired",
        "display_lock_unavailable",
        "lock_release_indeterminate",
        "private_input_invalid",
        "restore_indeterminate",
    ):
        assert validate(result(
            state="technical_failure",
            ok=False,
            failure_code=code,
            target_url_sha256=None,
            firefox_pid_sha256=None,
            restore_proof_sha256="f" * 64,
            stable_cycles_observed=0,
        ), 2) is None
    invalid = (
        (result(ok=False), 0),
        (result(failure_code="unexpected"), 0),
        (result(stable_cycles_observed=1), 0),
        (result(firefox_pid_sha256=None), 0),
        (result(restore_proof_sha256="invalid"), 0),
        (result(
            state="technical_failure",
            ok=False,
            failure_code="restore_indeterminate",
            stable_cycles_observed=0,
        ), 1),
        (result(
            state="technical_failure",
            ok=False,
            failure_code="unexpected",
            stable_cycles_observed=0,
        ), 2),
    )
    for payload, returncode in invalid:
        assert validate(payload, returncode) is not None


def validate_transaction_boundary(namespace: dict, private_root: Path) -> None:
    context = {
        "tool_profile": "linkedin-jobs-restore",
        "seat_id": "restore-validator-seat",
        "turn_id": "restore-validator-turn",
        "event_id": "restore-validator-event",
        "correlation_id": "restore-validator-correlation",
        "process_generation": "1" * 32,
        "_tool_profile_state": {"terminal": None},
    }
    transaction = prepare_private_root(
        private_root,
        context["seat_id"],
        context["correlation_id"],
    )
    transaction_sha256 = hashlib.sha256(transaction.read_bytes()).hexdigest()
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
        calls.append(command)
        receipt_path = Path(command[command.index("--receipt-file") + 1])
        receipt_bytes = b'{"schema":"linkedin_jobs_restore_validator_receipt_v1"}'
        receipt_path.write_bytes(receipt_bytes)
        receipt_path.chmod(0o400)
        payload = result(
            lineage=lineage_sha256(context),
            receipt=hashlib.sha256(receipt_bytes).hexdigest(),
        )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload, separators=(",", ":")),
            stderr="",
        )

    token = namespace["_request_context"].set(context)
    try:
        with patch("subprocess.run", fake_run):
            first = json.loads(namespace["_do_linkedin_jobs_restore"](
                {"display": ":18"}
            ))
            second = json.loads(namespace["_do_linkedin_jobs_restore"](
                {"display": ":18"}
            ))
    finally:
        namespace["_request_context"].reset(token)

    assert first["state"] == "restored" and first["ok"] is True
    assert "receipt_file already exists" in second["error"]
    assert len(calls) == 1
    command = calls[0]
    assert command[0] == sys.executable
    assert command[1].endswith("scripts/run_linkedin_jobs_restore.py")
    expected_flags = {
        "--display": ":18",
        "--private-root": str(private_root),
        "--transaction-file": str(transaction),
        "--expected-transaction-sha256": transaction_sha256,
        "--requester": context["seat_id"],
        "--turn-id": context["turn_id"],
        "--correlation-id": context["correlation_id"],
        "--process-generation": context["process_generation"],
        "--deadline-seconds": "1700",
    }
    for flag, expected in expected_flags.items():
        assert command[command.index(flag) + 1] == expected
    claim_path = (
        private_root
        / "claims"
        / context["seat_id"]
        / f"{context['correlation_id']}.json"
    )
    assert stat.S_IMODE(claim_path.stat().st_mode) == 0o400
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    assert claim["schema"] == "linkedin_jobs_restore_claim_v1"
    assert claim["transaction_sha256"] == transaction_sha256
    assert claim["turn_lineage_sha256"] == lineage_sha256(context)
    assert context["_tool_profile_state"]["terminal"]["tool"] == (
        "restore_linkedin_jobs_surface"
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="linkedin-jobs-restore-presence-") as temp:
        root = Path(temp)
        private_root = root / "private"
        hands_root = root / "hands"
        runner = hands_root / "scripts" / "run_linkedin_jobs_restore.py"
        runner.parent.mkdir(parents=True)
        runner.touch()
        environment = {
            "TAEYS_HANDS_ROOT": str(hands_root),
            "TAEY_LINKEDIN_JOBS_RESTORE_PYTHON": sys.executable,
            "TAEY_LINKEDIN_JOBS_RESTORE_PRIVATE_ROOT": str(private_root),
            "TAEY_LINKEDIN_JOBS_RESTORE_DISPLAYS": ":18",
            "TAEY_LINKEDIN_JOBS_RESTORE_TIMEOUT_SECS": "1800",
        }
        namespace = load_proxy(environment)
        validate_static_boundary(namespace)
        validate_result_contract(namespace)
        validate_transaction_boundary(namespace, private_root)
    print(json.dumps({
        "profile": "linkedin-jobs-restore",
        "runner": "run_linkedin_jobs_restore.py",
        "status": "PASS",
        "tool": "restore_linkedin_jobs_surface",
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
