#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import types


REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_PATH = REPO_ROOT / "serving" / "soma_proxy.py"
PROMPT_PATH = REPO_ROOT / "serving" / "TAEY_LINKEDIN_APPLICATION_INTAKE_SYSTEM.md"
RESULT_KEYS = {
    "schema",
    "ok",
    "state",
    "failure_code",
    "records_observed",
    "records_written",
    "job_identity_sha256",
    "row_digest",
    "receipt_sha256",
    "turn_lineage_sha256",
}
TRANSACTION = {
    "schema": "taey_apply_linkedin_intake_private_input_v1",
    "operation": "ingest_linkedin_captured_job",
    "search_receipt_ref": "sources/search-receipt.json",
    "search_artifact_ref": "sources/search-artifact.json",
    "selected_receipt_ref": "sources/selected-receipt.json",
    "selected_artifact_ref": "sources/selected-artifact.json",
    "card_digest": "a" * 64,
}
FAKE_CONNECTOR = r'''from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
for name in (
    "private-root", "database", "transaction-file",
    "expected-transaction-sha256", "receipt-file", "requester", "turn-id",
    "correlation-id", "process-generation",
):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()
expected_pythonpath = str(Path(__file__).resolve().parents[1])
if os.environ.get("PYTHONPATH") != expected_pythonpath:
    raise SystemExit(9)
transaction_raw = Path(args.transaction_file).read_bytes()
if hashlib.sha256(transaction_raw).hexdigest() != args.expected_transaction_sha256:
    raise SystemExit(10)
marker = Path(os.environ["TAEY_APPLY_VALIDATOR_MARKER"])
descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.close(descriptor)
lineage = hashlib.sha256(json.dumps({
    "correlation_id": args.correlation_id,
    "process_generation": args.process_generation,
    "requester": args.requester,
    "turn_id": args.turn_id,
}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
receipt = json.dumps({
    "schema": "validator_receipt_v1",
    "turn_lineage_sha256": lineage,
}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
receipt_descriptor = os.open(
    args.receipt_file,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    0o400,
)
os.write(receipt_descriptor, receipt)
os.fsync(receipt_descriptor)
os.close(receipt_descriptor)
result = {
    "schema": "taey_apply_linkedin_intake_result_v1",
    "ok": True,
    "state": "captured_unclassified",
    "failure_code": None,
    "records_observed": 1,
    "records_written": 1,
    "job_identity_sha256": "b" * 64,
    "row_digest": "c" * 64,
    "receipt_sha256": hashlib.sha256(receipt).hexdigest(),
    "turn_lineage_sha256": lineage,
}
print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
'''


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
    module = types.ModuleType("linkedin_application_intake_profile_validation")
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


def prepare_private_root(root: Path, seat: str) -> None:
    root.mkdir(mode=0o700)
    for section in ("transactions", "claims", "receipts"):
        parent = root / section
        parent.mkdir(mode=0o700)
        (parent / seat).mkdir(mode=0o700)


def write_transaction(root: Path, seat: str, correlation: str) -> Path:
    path = root / "transactions" / seat / f"{correlation}.json"
    path.write_bytes(canonical_bytes(TRANSACTION))
    path.chmod(0o400)
    return path


def context(profile: str, seat: str, correlation: str) -> dict:
    return {
        "tool_profile": profile,
        "seat_id": seat,
        "turn_id": "intake-validator-turn",
        "event_id": "intake-validator-event",
        "correlation_id": correlation,
        "process_generation": "1" * 32,
        "_tool_profile_state": {"terminal": None},
    }


def validate_static_boundary(namespace: dict) -> None:
    profile = namespace["_LINKEDIN_APPLICATION_INTAKE_TOOL_PROFILE"]
    assert profile == "linkedin-application-intake"
    assert namespace["_TOOL_PROFILE_ALLOWED"][profile] == frozenset(
        {"linkedin_application_intake"}
    )
    tools = {
        item["function"]["name"]: item["function"]
        for item in namespace["TOOLS"]
    }
    tool = tools["linkedin_application_intake"]
    assert tool["parameters"] == {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }
    spec = namespace["_private_transaction_spec_for_tool"](
        "linkedin_application_intake"
    )
    assert spec.profile == profile
    assert spec.runner_name == "taey_apply.cli"
    assert spec.claim_schema == "taey_apply_linkedin_intake_claim_v1"
    assert spec.expected_result_keys == frozenset(RESULT_KEYS)
    assert spec.displays == () and spec.displays_env_name == ""
    assert spec.python_env_name == "TAEY_APPLY_PYTHON"
    assert spec.public_root_env_name == "TAEY_APPLY_PUBLIC_ROOT"
    assert spec.private_root_env_name == "TAEY_APPLY_PRIVATE_ROOT"
    assert spec.database_env_name == "TAEY_APPLY_DB"
    assert spec.timeout_env_name == "TAEY_APPLY_TIMEOUT_SECS"

    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "Call `linkedin_application_intake` exactly once with `{}`" in prompt
    assert "display, paths, and job data are not your input" in prompt
    assert "Never retry" in prompt

    tree = ast.parse(PROXY_PATH.read_text(encoding="utf-8"))
    implementation = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_do_linkedin_application_intake"
    )
    source = ast.get_source_segment(PROXY_PATH.read_text(encoding="utf-8"), implementation)
    assert source is not None
    for forbidden in (
        "TAEYS_HANDS_ROOT",
        "UI_DRIVE",
        "display_lock",
        "score",
        "submit",
    ):
        assert forbidden not in source

    original = namespace["_do_linkedin_application_intake"]
    namespace["_do_linkedin_application_intake"] = lambda arguments: json.dumps(arguments)
    token = namespace["_request_context"].set({
        "tool_profile": profile,
        "_tool_profile_state": {"terminal": None},
    })
    try:
        assert json.loads(namespace["execute_tool_call"](
            "linkedin_application_intake", {}
        )) == {}
        refusal = namespace["execute_tool_call"]("linkedin_jobs", {"display": ":18"})
        assert "not available in profile" in refusal
    finally:
        namespace["_request_context"].reset(token)
        namespace["_do_linkedin_application_intake"] = original


def validate_result_contract(namespace: dict) -> None:
    validate = namespace["_linkedin_application_intake_result_error"]
    valid = {
        "schema": "taey_apply_linkedin_intake_result_v1",
        "ok": True,
        "state": "captured_unclassified",
        "failure_code": None,
        "records_observed": 1,
        "records_written": 1,
        "job_identity_sha256": "a" * 64,
        "row_digest": "b" * 64,
        "receipt_sha256": "c" * 64,
        "turn_lineage_sha256": "d" * 64,
    }
    assert validate(valid, 0) is None
    assert validate({**valid, "state": "already_present", "records_written": 0}, 0) is None
    for payload, returncode in (
        ({**valid, "schema": "wrong"}, 0),
        ({**valid, "records_observed": True}, 0),
        ({**valid, "records_written": 0}, 0),
        ({**valid, "receipt_sha256": "not-a-digest"}, 0),
        (valid, 2),
    ):
        assert validate(payload, returncode) is not None


def validate_one_shot(namespace: dict, private_root: Path, database: Path) -> None:
    profile = namespace["_LINKEDIN_APPLICATION_INTAKE_TOOL_PROFILE"]
    seat = "intake-validator-seat"
    correlation = "intake-validator-correlation"
    transaction_path = write_transaction(private_root, seat, correlation)
    request_context = context(profile, seat, correlation)
    token = namespace["_request_context"].set(request_context)
    try:
        result = json.loads(namespace["_do_linkedin_application_intake"]({}))
    finally:
        namespace["_request_context"].reset(token)
    assert set(result) == RESULT_KEYS, result
    assert result["state"] == "captured_unclassified"
    assert "display" not in result and "path" not in result
    assert request_context["_tool_profile_state"]["terminal"] == {
        "tool": "linkedin_application_intake",
        "reason": "the one frozen LinkedIn Application Intake invocation has been spent",
    }

    transaction_sha256 = hashlib.sha256(transaction_path.read_bytes()).hexdigest()
    lineage = hashlib.sha256(canonical_bytes({
        "correlation_id": correlation,
        "process_generation": "1" * 32,
        "requester": seat,
        "turn_id": "intake-validator-turn",
    })).hexdigest()
    claim_path = private_root / "claims" / seat / f"{correlation}.json"
    assert stat.S_IMODE(claim_path.stat().st_mode) == 0o400
    assert json.loads(claim_path.read_text(encoding="utf-8")) == {
        "correlation_id_sha256": hashlib.sha256(correlation.encode()).hexdigest(),
        "event_id_sha256": hashlib.sha256(b"intake-validator-event").hexdigest(),
        "schema": "taey_apply_linkedin_intake_claim_v1",
        "seat_id": seat,
        "transaction_sha256": transaction_sha256,
        "turn_lineage_sha256": lineage,
    }
    receipt_path = private_root / "receipts" / seat / f"{correlation}.json"
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o400
    assert hashlib.sha256(receipt_path.read_bytes()).hexdigest() == result["receipt_sha256"]

    token = namespace["_request_context"].set(context(profile, seat, correlation))
    try:
        duplicate = json.loads(namespace["_do_linkedin_application_intake"]({}))
    finally:
        namespace["_request_context"].reset(token)
    assert duplicate["ok"] is False and "spent" in duplicate["error"]

    invalid_correlation = "intake-validator-invalid-args"
    write_transaction(private_root, seat, invalid_correlation)
    token = namespace["_request_context"].set(
        context(profile, seat, invalid_correlation)
    )
    try:
        invalid = json.loads(namespace["_do_linkedin_application_intake"](
            {"display": ":18"}
        ))
    finally:
        namespace["_request_context"].reset(token)
    assert invalid["ok"] is False and "empty JSON object" in invalid["error"]
    assert not (private_root / "claims" / seat / f"{invalid_correlation}.json").exists()

    noncanonical_correlation = "intake-validator-noncanonical"
    noncanonical_path = (
        private_root
        / "transactions"
        / seat
        / f"{noncanonical_correlation}.json"
    )
    noncanonical_path.write_bytes(canonical_bytes(TRANSACTION) + b"\n")
    noncanonical_path.chmod(0o400)
    token = namespace["_request_context"].set(
        context(profile, seat, noncanonical_correlation)
    )
    try:
        noncanonical = json.loads(namespace["_do_linkedin_application_intake"]({}))
    finally:
        namespace["_request_context"].reset(token)
    assert noncanonical["ok"] is False and "canonical intake contract" in noncanonical["error"]
    assert not (
        private_root / "claims" / seat / f"{noncanonical_correlation}.json"
    ).exists()

    unsafe_database_correlation = "intake-validator-unsafe-database"
    write_transaction(private_root, seat, unsafe_database_correlation)
    database.chmod(0o644)
    token = namespace["_request_context"].set(
        context(profile, seat, unsafe_database_correlation)
    )
    try:
        unsafe_database = json.loads(
            namespace["_do_linkedin_application_intake"]({})
        )
    finally:
        namespace["_request_context"].reset(token)
        database.chmod(0o600)
    assert unsafe_database["ok"] is False and "0600 regular file" in unsafe_database["error"]
    assert not (
        private_root / "claims" / seat / f"{unsafe_database_correlation}.json"
    ).exists()
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="linkedin-application-intake-") as raw_temp:
        root = Path(raw_temp)
        public_root = root / "taey-apply"
        package_root = public_root / "src" / "taey_apply"
        package_root.mkdir(parents=True)
        (package_root / "__init__.py").write_text("", encoding="utf-8")
        (package_root / "cli.py").write_text(FAKE_CONNECTOR, encoding="utf-8")
        private_root = root / "private"
        seat = "intake-validator-seat"
        prepare_private_root(private_root, seat)
        database = root / "jobs.db"
        database.touch(mode=0o600)
        database.chmod(0o600)
        marker = root / "connector-invoked-once"
        environment = {
            "TAEY_APPLY_PYTHON": sys.executable,
            "TAEY_APPLY_PUBLIC_ROOT": str(public_root),
            "TAEY_APPLY_PRIVATE_ROOT": str(private_root),
            "TAEY_APPLY_DB": str(database),
            "TAEY_APPLY_TIMEOUT_SECS": "30",
            "TAEY_APPLY_VALIDATOR_MARKER": str(marker),
        }
        namespace = load_proxy(environment)
        validate_static_boundary(namespace)
        validate_result_contract(namespace)
        prior_marker = os.environ.get("TAEY_APPLY_VALIDATOR_MARKER")
        os.environ["TAEY_APPLY_VALIDATOR_MARKER"] = str(marker)
        try:
            validate_one_shot(namespace, private_root, database)
        finally:
            if prior_marker is None:
                os.environ.pop("TAEY_APPLY_VALIDATOR_MARKER", None)
            else:
                os.environ["TAEY_APPLY_VALIDATOR_MARKER"] = prior_marker
        assert marker.is_file()

    print(json.dumps({
        "claim_mode": "0400",
        "profile": "linkedin-application-intake",
        "receipt_mode": "0400",
        "result_keys": sorted(RESULT_KEYS),
        "status": "PASS",
        "tool_arguments": {},
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
