#!/usr/bin/env python3
from __future__ import annotations

import asyncio
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
PROMPT_PATH = (
    REPO_ROOT / "serving" / "TAEY_LINKEDIN_APPLICATION_CLASSIFICATION_SYSTEM.md"
)
RESULT_KEYS = {
    "schema",
    "operation",
    "ok",
    "state",
    "failure_code",
    "records_observed",
    "records_written",
    "transaction_sha256",
    "receipt_sha256",
    "turn_lineage_sha256",
    "terminal",
}
PRIVATE_CLASSIFICATION_CLAIM = {
    "schema": "taey_apply_linkedin_classification_claim_v1",
    "operation": "classify_frozen_linkedin_intake",
    "intake_transaction_ref": "upstream/intake-transaction.json",
    "intake_transaction_sha256": "1" * 64,
    "intake_receipt_ref": "upstream/intake-receipt.json",
    "intake_receipt_sha256": "2" * 64,
    "prewrite_row_sha256": "3" * 64,
    "stable_row_sha256": "4" * 64,
    "policy_input_sha256": "5" * 64,
    "classifier_sha256": "6" * 64,
    "verdict": "PASS",
}
FAKE_CONNECTOR = r'''from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
for name in (
    "private-root", "database", "claim-file", "expected-claim-sha256",
    "receipt-file", "requester", "turn-id", "correlation-id",
    "process-generation",
):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()
expected_pythonpath = str(Path(__file__).resolve().parents[1])
if os.environ.get("PYTHONPATH") != expected_pythonpath:
    raise SystemExit(9)
claim_raw = Path(args.claim_file).read_bytes()
if hashlib.sha256(claim_raw).hexdigest() != args.expected_claim_sha256:
    raise SystemExit(10)
marker = Path(os.environ["TAEY_APPLY_CLASSIFICATION_VALIDATOR_MARKERS"]) / args.correlation_id
descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.close(descriptor)
lineage = hashlib.sha256(json.dumps({
    "correlation_id": args.correlation_id,
    "process_generation": args.process_generation,
    "requester": args.requester,
    "turn_id": args.turn_id,
}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
receipt = json.dumps({
    "schema": "validator_classification_receipt_v1",
    "turn_lineage_sha256": lineage,
}, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
receipt_descriptor = os.open(
    args.receipt_file,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    0o400,
)
os.write(receipt_descriptor, receipt)
os.fsync(receipt_descriptor)
os.close(receipt_descriptor)
result = {
    "schema": "taey_apply_linkedin_classification_result_v1",
    "operation": "classify_frozen_linkedin_intake",
    "ok": True,
    "state": "classified",
    "failure_code": None,
    "records_observed": 1,
    "records_written": 1,
    "transaction_sha256": args.expected_claim_sha256,
    "receipt_sha256": hashlib.sha256(receipt).hexdigest(),
    "turn_lineage_sha256": lineage,
    "terminal": True,
}
print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
'''


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeHttp:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    async def post(self, *_args, **_kwargs) -> FakeResponse:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("one-shot profile requested a second inference round")
        return FakeResponse(self.payload)


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
    module = types.ModuleType("linkedin_application_classification_profile_validation")
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
    for section in (
        "transactions",
        "presence-claims",
        "receipts",
        "decisions",
        "classification-attempts",
        "upstream",
    ):
        parent = root / section
        parent.mkdir(mode=0o700)
    for section in ("transactions", "presence-claims", "receipts"):
        (root / section / seat).mkdir(mode=0o700)


def write_private_classification_claim(root: Path) -> tuple[Path, str]:
    path = root / "decisions" / "classification.json"
    raw = canonical_bytes(PRIVATE_CLASSIFICATION_CLAIM)
    path.write_bytes(raw)
    path.chmod(0o400)
    return path, hashlib.sha256(raw).hexdigest()


def write_transaction(
    root: Path,
    seat: str,
    correlation: str,
    classification_claim_sha256: str,
    *,
    suffix: bytes = b"",
) -> Path:
    path = root / "transactions" / seat / f"{correlation}.json"
    path.write_bytes(canonical_bytes({
        "schema": "taey_apply_linkedin_classification_private_input_v1",
        "operation": "classify_frozen_linkedin_intake",
        "classification_claim_ref": "decisions/classification.json",
        "classification_claim_sha256": classification_claim_sha256,
    }) + suffix)
    path.chmod(0o400)
    return path


def context(profile: str, seat: str, correlation: str) -> dict:
    return {
        "tool_profile": profile,
        "seat_id": seat,
        "turn_id": "classification-validator-turn",
        "event_id": "classification-validator-event",
        "correlation_id": correlation,
        "process_generation": "1" * 32,
        "_tool_profile_state": {"terminal": None},
    }


def validate_static_boundary(namespace: dict) -> None:
    profile = namespace["_LINKEDIN_APPLICATION_CLASSIFICATION_TOOL_PROFILE"]
    assert profile == "linkedin-application-classification"
    assert namespace["_TOOL_PROFILE_ALLOWED"][profile] == frozenset(
        {"linkedin_application_classification"}
    )
    tools = {
        item["function"]["name"]: item["function"]
        for item in namespace["TOOLS"]
    }
    tool = tools["linkedin_application_classification"]
    assert tool["parameters"] == {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }
    spec = namespace["_private_transaction_spec_for_tool"](
        "linkedin_application_classification"
    )
    assert spec.profile == profile
    assert spec.runner_name == "taey_apply.classification_cli"
    assert spec.claim_schema == "taey_apply_linkedin_classification_presence_claim_v1"
    assert spec.expected_result_keys == frozenset(RESULT_KEYS)
    assert spec.displays == () and spec.displays_env_name == ""
    assert spec.python_env_name == "TAEY_APPLY_CLASSIFICATION_PYTHON"
    assert spec.public_root_env_name == "TAEY_APPLY_CLASSIFICATION_PUBLIC_ROOT"
    assert spec.private_root_env_name == "TAEY_APPLY_CLASSIFICATION_PRIVATE_ROOT"
    assert spec.database_env_name == "TAEY_APPLY_CLASSIFICATION_DB"
    assert spec.timeout_env_name == "TAEY_APPLY_CLASSIFICATION_TIMEOUT_SECS"

    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "Call `linkedin_application_classification` exactly once with `{}`" in prompt
    assert "outside model context" in prompt
    assert "Never retry" in prompt

    source_text = PROXY_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    implementation = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_do_linkedin_application_classification"
    )
    source = ast.get_source_segment(source_text, implementation)
    assert source is not None
    for forbidden in (
        "TAEYS_HANDS_ROOT",
        "UI_DRIVE",
        "display_lock",
        "verdict",
        "policy",
        "score",
        "submit",
    ):
        assert forbidden not in source

    original = namespace["_do_linkedin_application_classification"]
    namespace["_do_linkedin_application_classification"] = (
        lambda arguments: json.dumps(arguments)
    )
    token = namespace["_request_context"].set({
        "tool_profile": profile,
        "_tool_profile_state": {"terminal": None},
    })
    try:
        assert json.loads(namespace["execute_tool_call"](
            "linkedin_application_classification", {}
        )) == {}
        refusal = namespace["execute_tool_call"](
            "linkedin_application_intake", {}
        )
        assert "not available in profile" in refusal
    finally:
        namespace["_request_context"].reset(token)
        namespace["_do_linkedin_application_classification"] = original


def validate_result_contract(namespace: dict) -> None:
    validate = namespace["_linkedin_application_classification_result_error"]
    valid = {
        "schema": "taey_apply_linkedin_classification_result_v1",
        "operation": "classify_frozen_linkedin_intake",
        "ok": True,
        "state": "classified",
        "failure_code": None,
        "records_observed": 1,
        "records_written": 1,
        "transaction_sha256": "a" * 64,
        "receipt_sha256": "b" * 64,
        "turn_lineage_sha256": "c" * 64,
        "terminal": True,
    }
    assert validate(valid, 0) is None
    for payload, returncode in (
        ({**valid, "schema": "wrong"}, 0),
        ({**valid, "operation": "wrong"}, 0),
        ({**valid, "records_observed": True}, 0),
        ({**valid, "records_written": 0}, 0),
        ({**valid, "terminal": False}, 0),
        ({**valid, "receipt_sha256": "not-a-digest"}, 0),
        (valid, 2),
    ):
        assert validate(payload, returncode) is not None


async def parser_case(
    namespace: dict,
    *,
    stream: bool,
    raw_arguments: object,
) -> tuple[tuple, list[tuple[str, dict]], dict]:
    profile = namespace["_LINKEDIN_APPLICATION_CLASSIFICATION_TOOL_PROFILE"]
    tool = "linkedin_application_classification"
    payload = {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-classification",
                    "type": "function",
                    "function": {"name": tool, "arguments": raw_arguments},
                }],
            },
        }],
        "usage": {"completion_tokens": 1, "prompt_tokens": 1},
    }
    namespace["_http"] = FakeHttp(payload)
    namespace["publish_metrics"] = lambda *_args, **_kwargs: None
    calls: list[tuple[str, dict]] = []

    async def fake_execute(name: str, arguments: dict, **_kwargs) -> str:
        calls.append((name, arguments))
        return json.dumps({"ok": True, "state": "validator_terminal"})

    namespace["execute_tool_call_async"] = fake_execute
    namespace["_one_shot_system_prompts"] = {
        profile: PROMPT_PATH.read_text(encoding="utf-8")
    }
    turn = namespace["TurnContext"](
        turn_id="classification-parser-turn",
        seat_id="classification-parser-seat",
        event_id="classification-parser-event",
        correlation_id="classification-parser-correlation",
        tool_profile=profile,
        proxy_namespace="classification-parser-proxy",
        process_generation="1" * 32,
        started_at=0.0,
    )
    request_context = context(
        profile,
        "classification-parser-seat",
        "classification-parser-correlation",
    )
    request_context["turn_id"] = turn.turn_id
    request_context["event_id"] = turn.event_id
    token = namespace["_request_context"].set(request_context)
    try:
        try:
            response = await namespace["_chat_completions_for_turn"](
                {
                    "stream": stream,
                    "messages": [{"role": "user", "content": "Run once."}],
                },
                turn,
                False,
            )
        except namespace["HTTPException"] as exc:
            outcome = ("error", exc.status_code, exc.detail)
        else:
            if stream:
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
                outcome = ("stream", "".join(chunks))
            else:
                outcome = (
                    "nonstream",
                    response.status_code,
                    response.body.decode("utf-8"),
                )
    finally:
        namespace["_request_context"].reset(token)
    return outcome, calls, request_context["_tool_profile_state"]


def validate_parser_boundary(namespace: dict, private_root: Path) -> None:
    claim_root = private_root / "presence-claims" / "classification-validator-seat"
    claims_before = set(claim_root.iterdir())
    for stream in (True, False):
        valid_outcome, valid_calls, valid_state = asyncio.run(parser_case(
            namespace,
            stream=stream,
            raw_arguments="{}",
        ))
        assert valid_outcome[0] == ("stream" if stream else "nonstream")
        assert valid_calls == [("linkedin_application_classification", {})]
        assert valid_state["terminal"] is None
        for raw_arguments in (
            "{",
            "",
            "[]",
            '{"duplicate":1,"duplicate":2}',
            '{"not_finite":NaN}',
            None,
        ):
            outcome, calls, state = asyncio.run(parser_case(
                namespace,
                stream=stream,
                raw_arguments=raw_arguments,
            ))
            assert outcome == (
                "error",
                502,
                {
                    "error": (
                        "linkedin_application_classification_tool_arguments_invalid"
                    ),
                    "turn_id": "classification-parser-turn",
                },
            )
            assert calls == []
            assert state["terminal"] == {
                "tool": "linkedin_application_classification",
                "reason": "malformed tool arguments refused before execution",
            }
    assert set(claim_root.iterdir()) == claims_before


def validate_one_shot(
    namespace: dict,
    private_root: Path,
    database: Path,
    classification_claim_sha256: str,
) -> None:
    profile = namespace["_LINKEDIN_APPLICATION_CLASSIFICATION_TOOL_PROFILE"]
    seat = "classification-validator-seat"
    correlation = "classification-validator-correlation"
    transaction_path = write_transaction(
        private_root,
        seat,
        correlation,
        classification_claim_sha256,
    )
    request_context = context(profile, seat, correlation)
    token = namespace["_request_context"].set(request_context)
    try:
        result = json.loads(
            namespace["_do_linkedin_application_classification"]({})
        )
    finally:
        namespace["_request_context"].reset(token)
    assert set(result) == RESULT_KEYS, result
    assert result["state"] == "classified"
    serialized_result = canonical_bytes(result).decode("utf-8")
    for private_value in (
        "PASS",
        "decisions/classification.json",
        str(private_root),
        str(database),
    ):
        assert private_value not in serialized_result
    assert request_context["_tool_profile_state"]["terminal"] == {
        "tool": "linkedin_application_classification",
        "reason": (
            "the one frozen LinkedIn Application Classification invocation has "
            "been spent"
        ),
    }

    transaction_sha256 = hashlib.sha256(transaction_path.read_bytes()).hexdigest()
    lineage = hashlib.sha256(canonical_bytes({
        "correlation_id": correlation,
        "process_generation": "1" * 32,
        "requester": seat,
        "turn_id": "classification-validator-turn",
    })).hexdigest()
    presence_claim_path = (
        private_root / "presence-claims" / seat / f"{correlation}.json"
    )
    assert stat.S_IMODE(presence_claim_path.stat().st_mode) == 0o400
    assert json.loads(presence_claim_path.read_text(encoding="utf-8")) == {
        "classification_claim_sha256": classification_claim_sha256,
        "correlation_id_sha256": hashlib.sha256(correlation.encode()).hexdigest(),
        "event_id_sha256": hashlib.sha256(
            b"classification-validator-event"
        ).hexdigest(),
        "schema": "taey_apply_linkedin_classification_presence_claim_v1",
        "seat_id": seat,
        "transaction_sha256": transaction_sha256,
        "turn_lineage_sha256": lineage,
    }
    receipt_path = private_root / "receipts" / seat / f"{correlation}.json"
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o400
    assert hashlib.sha256(receipt_path.read_bytes()).hexdigest() == result["receipt_sha256"]
    assert result["transaction_sha256"] == classification_claim_sha256

    token = namespace["_request_context"].set(context(profile, seat, correlation))
    try:
        duplicate = json.loads(
            namespace["_do_linkedin_application_classification"]({})
        )
    finally:
        namespace["_request_context"].reset(token)
    assert duplicate["ok"] is False and "spent" in duplicate["error"]

    invalid_correlation = "classification-validator-invalid-args"
    write_transaction(
        private_root,
        seat,
        invalid_correlation,
        classification_claim_sha256,
    )
    token = namespace["_request_context"].set(
        context(profile, seat, invalid_correlation)
    )
    try:
        invalid = json.loads(
            namespace["_do_linkedin_application_classification"](
                {"unexpected": True}
            )
        )
    finally:
        namespace["_request_context"].reset(token)
    assert invalid["ok"] is False and "empty JSON object" in invalid["error"]
    assert not (
        private_root / "presence-claims" / seat / f"{invalid_correlation}.json"
    ).exists()

    noncanonical_correlation = "classification-validator-noncanonical"
    write_transaction(
        private_root,
        seat,
        noncanonical_correlation,
        classification_claim_sha256,
        suffix=b"\n",
    )
    token = namespace["_request_context"].set(
        context(profile, seat, noncanonical_correlation)
    )
    try:
        noncanonical = json.loads(
            namespace["_do_linkedin_application_classification"]({})
        )
    finally:
        namespace["_request_context"].reset(token)
    assert noncanonical["ok"] is False
    assert "canonical classification contract" in noncanonical["error"]
    assert not (
        private_root
        / "presence-claims"
        / seat
        / f"{noncanonical_correlation}.json"
    ).exists()

    unsafe_database_correlation = "classification-validator-unsafe-database"
    write_transaction(
        private_root,
        seat,
        unsafe_database_correlation,
        classification_claim_sha256,
    )
    database.chmod(0o644)
    token = namespace["_request_context"].set(
        context(profile, seat, unsafe_database_correlation)
    )
    try:
        unsafe_database = json.loads(
            namespace["_do_linkedin_application_classification"]({})
        )
    finally:
        namespace["_request_context"].reset(token)
        database.chmod(0o600)
    assert unsafe_database["ok"] is False
    assert "0600 regular file" in unsafe_database["error"]
    assert not (
        private_root
        / "presence-claims"
        / seat
        / f"{unsafe_database_correlation}.json"
    ).exists()


def main() -> int:
    with tempfile.TemporaryDirectory(
        prefix="linkedin-application-classification-"
    ) as raw_temp:
        root = Path(raw_temp)
        public_root = root / "taey-apply"
        package_root = public_root / "src" / "taey_apply"
        package_root.mkdir(parents=True)
        (package_root / "__init__.py").write_text("", encoding="utf-8")
        (package_root / "classification_cli.py").write_text(
            FAKE_CONNECTOR,
            encoding="utf-8",
        )
        shadow_marker = root / "root-shadow-executed"
        shadow_root = public_root / "taey_apply"
        shadow_root.mkdir()
        (shadow_root / "__init__.py").write_text("", encoding="utf-8")
        (shadow_root / "classification_cli.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(shadow_marker)!r}).touch()\n",
            encoding="utf-8",
        )
        private_root = root / "private-classification"
        seat = "classification-validator-seat"
        prepare_private_root(private_root, seat)
        _, classification_claim_sha256 = write_private_classification_claim(
            private_root
        )
        database = root / "jobs.db"
        database.touch(mode=0o600)
        database.chmod(0o600)
        marker_root = root / "connector-markers"
        marker_root.mkdir(mode=0o700)
        environment = {
            "TAEY_APPLY_CLASSIFICATION_PYTHON": sys.executable,
            "TAEY_APPLY_CLASSIFICATION_PUBLIC_ROOT": str(public_root),
            "TAEY_APPLY_CLASSIFICATION_PRIVATE_ROOT": str(private_root),
            "TAEY_APPLY_CLASSIFICATION_DB": str(database),
            "TAEY_APPLY_CLASSIFICATION_TIMEOUT_SECS": "30",
            "TAEY_APPLY_CLASSIFICATION_VALIDATOR_MARKERS": str(marker_root),
        }
        namespace = load_proxy(environment)
        validate_static_boundary(namespace)
        validate_result_contract(namespace)
        validate_parser_boundary(namespace, private_root)
        prior_marker = os.environ.get(
            "TAEY_APPLY_CLASSIFICATION_VALIDATOR_MARKERS"
        )
        os.environ["TAEY_APPLY_CLASSIFICATION_VALIDATOR_MARKERS"] = str(marker_root)
        try:
            validate_one_shot(
                namespace,
                private_root,
                database,
                classification_claim_sha256,
            )
        finally:
            if prior_marker is None:
                os.environ.pop(
                    "TAEY_APPLY_CLASSIFICATION_VALIDATOR_MARKERS",
                    None,
                )
            else:
                os.environ[
                    "TAEY_APPLY_CLASSIFICATION_VALIDATOR_MARKERS"
                ] = prior_marker
        assert sorted(path.name for path in marker_root.iterdir()) == [
            "classification-validator-correlation"
        ]
        assert not shadow_marker.exists()

    print(json.dumps({
        "claim_mode": "0400",
        "profile": "linkedin-application-classification",
        "receipt_mode": "0400",
        "result_keys": sorted(RESULT_KEYS),
        "root_shadow_isolated": True,
        "status": "PASS",
        "strict_argument_cases": 12,
        "tool_arguments": {},
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
