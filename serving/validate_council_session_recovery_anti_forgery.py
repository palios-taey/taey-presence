#!/usr/bin/env python3
"""Mechanical contract: anti-forgery validator for council session recovery outcomes."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

# Install import-only stubs if dependencies are absent in CI runner
if importlib.util.find_spec("redis") is None:
    redis_stub = ModuleType("redis")
    redis_stub.Redis = object
    sys.modules["redis"] = redis_stub

if importlib.util.find_spec("taey_adapter") is None:
    sys.modules["taey_adapter"] = ModuleType("taey_adapter")

ROOT = Path(__file__).resolve().parent.parent
SERVING = str(Path(__file__).resolve().parent)
while SERVING in sys.path:
    sys.path.remove(SERVING)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import serving.council_prompt_receipt as producer
from dashboard import native_council


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise RuntimeError(detail)


def canonical_sha256(value: object) -> str:
    return producer.canonical_sha256(value)


def main() -> int:
    session_id = "dcm-session-test"
    wave_id = "wave-test-1"
    session_failure_record = {
        "contract": "dcm-session-failure/v1",
        "session_id": session_id,
        "failure_kind": "seat_inference_side_effect_uncertain",
        "failure_detail_sha256": "sha256:" + "d" * 64,
        "wave_id": wave_id,
    }
    session_failure_sha256 = canonical_sha256(session_failure_record)
    session_failure_record["terminal_failure_sha256"] = session_failure_sha256

    def make_req_identity(seat_id: str, role: str, req_id: str) -> dict[str, object]:
        return {
            "session_id": session_id,
            "wave_id": wave_id,
            "round": 1,
            "phase": "independent",
            "prompt_id": "prompt-1",
            "prompt_revision": 1,
            "prompt_sha256": "sha256:" + "p" * 64,
            "seat_id": seat_id,
            "role": role,
            "request_revision": 1,
            "parent_frontier_sha256": "sha256:" + "f" * 64,
            "process_generation_expected": "gen-1",
            "model_endpoint": "http://127.0.0.1:8000",
            "requested_alias": "taey",
            "model_manifest_sha256": "sha256:" + "m" * 64,
            "model_content_sha256": "sha256:" + "c" * 64,
            "serving_container_digest": "sha256:" + "s" * 64,
            "request_contract": "taey-native-dcm-request/v2",
            "prompt_contract_sha256": "sha256:" + "r" * 64,
            "model_identity_receipt_sha256": "sha256:" + "i" * 64,
        }

    claim_obs = {
        "process_generation_observed": "gen-1",
        "model_endpoint": "http://127.0.0.1:8000",
        "served_alias": "taey",
        "model_manifest_sha256": "sha256:" + "m" * 64,
        "model_content_sha256": "sha256:" + "c" * 64,
        "serving_container_digest": "sha256:" + "s" * 64,
        "prompt_contract_sha256": "sha256:" + "r" * 64,
        "model_identity_receipt_sha256": "sha256:" + "i" * 64,
    }

    slot_claimed = {
        "seat_id": "taey-council-1",
        "role": "context-memory",
        "request_revision": 1,
        "request_id": "req-1",
        "state": "claimed",
        "request_identity": make_req_identity("taey-council-1", "context-memory", "req-1"),
        "claim_observation": claim_obs,
    }

    slot_contributed = {
        "seat_id": "taey-council-2",
        "role": "evidence-reality",
        "request_revision": 1,
        "request_id": "req-2",
        "state": "contributed",
        "request_identity": make_req_identity("taey-council-2", "evidence-reality", "req-2"),
        "claim_observation": claim_obs,
        "contrib_id": "contrib-2",
    }

    slot_pending = {
        "seat_id": "taey-council-3",
        "role": "systems-dependencies",
        "request_revision": 1,
        "request_id": "req-3",
        "state": "pending",
        "request_identity": make_req_identity("taey-council-3", "systems-dependencies", "req-3"),
        "claim_observation": None,
    }

    outcome_record_failed = {
        "outcome_record_sha256": "sha256:" + "o" * 64,
        "failure_stage": "model_identity_unproven",
        "failure_detail_sha256": "sha256:" + "e" * 64,
        "inference_performed": False,
    }
    slot_failed = {
        "seat_id": "taey-council-4",
        "role": "adversarial-failure",
        "request_revision": 1,
        "request_id": "req-4",
        "state": "failed",
        "terminal_outcome": "model_identity_unproven",
        "request_identity": make_req_identity("taey-council-4", "adversarial-failure", "req-4"),
        "claim_observation": claim_obs,
        "outcome_record": outcome_record_failed,
        "inference_performed": False,
        "failure_stage": "model_identity_unproven",
        "failure_detail_sha256": "sha256:" + "e" * 64,
    }

    outcome_record_failed_inferred = {
        "outcome_record_sha256": "sha256:" + "f" * 64,
        "failure_stage": "inference_timeout",
        "failure_detail_sha256": "sha256:" + "t" * 64,
        "inference_performed": True,
    }
    slot_failed_inferred = {
        "seat_id": "taey-council-5",
        "role": "scope-intent",
        "request_revision": 1,
        "request_id": "req-5",
        "state": "failed",
        "terminal_outcome": "inference_timeout",
        "request_identity": make_req_identity("taey-council-5", "scope-intent", "req-5"),
        "claim_observation": claim_obs,
        "outcome_record": outcome_record_failed_inferred,
        "inference_performed": True,
        "failure_stage": "inference_timeout",
        "failure_detail_sha256": "sha256:" + "t" * 64,
    }

    contrib_obj = {
        "contrib_id": "contrib-2",
        "contribution_receipt_sha256": "sha256:" + "z" * 64,
        "structured_content": {"summary": "valid contribution content"},
    }

    wave_fixture = {
        "session_id": session_id,
        "wave_id": wave_id,
        "round": 1,
        "phase": "independent",
        "prompt_id": "prompt-1",
        "prompt_revision": 1,
        "prompt_sha256": "sha256:" + "p" * 64,
        "parent_frontier_sha256": "sha256:" + "f" * 64,
        "status": "closed",
        "session_status": "failed",
        "close_outcome": "session_failed",
        "session_failure": session_failure_record,
        "session_failure_sha256": session_failure_sha256,
        "graph_uri": "bolt://127.0.0.1:7687",
        "graph_database": "neo4j",
        "slots": [slot_claimed, slot_contributed, slot_pending, slot_failed, slot_failed_inferred],
        "contributions": [contrib_obj],
    }

    seats = native_council.COUNCIL_SEATS
    transport = native_council.NativeCouncilTransport(
        redis_client=MagicMock(),
        sessions_dir=Path("/tmp"),
        seats=seats,
    )
    adapter_mock = MagicMock()
    adapter_mock.mesh.read_wave = MagicMock(return_value=wave_fixture)
    adapter_mock.mesh.DCM_NEO4J_URI = "bolt://127.0.0.1:7687"
    adapter_mock.mesh.DCM_NEO4J_DATABASE = "neo4j"
    transport._dcm_adapter = lambda: adapter_mock

    def make_receipt(
        seat_id: str,
        role: str,
        req_id: str,
        del_id: str,
        term_outcome: str,
        graph_rcpt_sha: str,
        inf_perf: object,
        inf_state: str | None = None,
        observed_gen: str | None = "gen-1",
        served_al: str | None = "taey",
        failure_stage: str | None = None,
        failure_detail_sha: str | None = None,
        **extra: object,
    ) -> dict[str, object]:
        rcpt: dict[str, object] = {
            "contract": "taey-native-dcm-receipt/v2",
            "receipt_kind": "transport",
            "session_id": session_id,
            "correlation_id": session_id,
            "wave_id": wave_id,
            "round": 1,
            "phase": "independent",
            "prompt": {"prompt_id": "prompt-1", "revision": 1, "sha256": "sha256:" + "p" * 64},
            "seat_id": seat_id,
            "role": role,
            "request_revision": 1,
            "request_id": req_id,
            "emitter": {"component": "taey-council-seat", "process_generation": "gen-1"},
            "graph": {"uri": "bolt://127.0.0.1:7687", "database": "neo4j"},
            "frontier": {"parent_contribution_ids": [], "parent_frontier_sha256": "sha256:" + "f" * 64, "claimed_peers": [], "peers_present": []},
            "execution": {
                "model_endpoint": "http://127.0.0.1:8000",
                "process_generation_expected": "gen-1",
                "process_generation_observed": observed_gen,
                "requested_alias": "taey",
                "served_alias": served_al,
                "model_manifest_sha256": "sha256:" + "m" * 64,
                "model_content_sha256": "sha256:" + "c" * 64,
                "serving_container_digest": "sha256:" + "s" * 64,
            },
            "stage": "terminal_acknowledged",
            "delivery_id": del_id,
            "acknowledgement_id": canonical_sha256({
                "delivery_id": del_id,
                "graph_receipt_sha256": graph_rcpt_sha,
                "request_id": req_id,
                "terminal_outcome": term_outcome,
            }),
            "claim_outcome": "duplicate_dispatch",
            "terminal_outcome": term_outcome,
            "inference_performed": inf_perf,
            "original_request_id": req_id,
            "request_contract": "taey-native-dcm-request/v2",
            "prompt_contract_sha256": "sha256:" + "r" * 64,
            "model_identity_receipt_sha256": "sha256:" + "i" * 64,
        }
        if inf_state is not None:
            rcpt["inference_state"] = inf_state
        if term_outcome == "session_failed":
            rcpt["session_failure_sha256"] = session_failure_sha256
            rcpt["failure_stage"] = "session_failed"
            rcpt["failure_detail_sha256"] = session_failure_record["failure_detail_sha256"]
        elif failure_stage is not None:
            rcpt["failure_stage"] = failure_stage
            rcpt["failure_detail_sha256"] = failure_detail_sha
        rcpt.update(extra)
        rcpt["receipt_sha256"] = canonical_sha256(rcpt)
        return rcpt

    seat_1 = next(s for s in seats if s.seat_id == "taey-council-1")
    seat_2 = next(s for s in seats if s.seat_id == "taey-council-2")
    seat_3 = next(s for s in seats if s.seat_id == "taey-council-3")
    seat_4 = next(s for s in seats if s.seat_id == "taey-council-4")
    seat_5 = next(s for s in seats if s.seat_id == "taey-council-5")

    req_1 = {**slot_claimed["request_identity"], "dcm_session_id": session_id, "delivery_id": "del-1", "request_id": "req-1", "expected_process_generation": "gen-1", "parent_contribution_ids": []}
    req_2 = {**slot_contributed["request_identity"], "dcm_session_id": session_id, "delivery_id": "del-2", "request_id": "req-2", "expected_process_generation": "gen-1", "parent_contribution_ids": []}
    req_3 = {**slot_pending["request_identity"], "dcm_session_id": session_id, "delivery_id": "del-3", "request_id": "req-3", "expected_process_generation": "gen-1", "parent_contribution_ids": []}
    req_4 = {**slot_failed["request_identity"], "dcm_session_id": session_id, "delivery_id": "del-4", "request_id": "req-4", "expected_process_generation": "gen-1", "parent_contribution_ids": []}
    req_5 = {**slot_failed_inferred["request_identity"], "dcm_session_id": session_id, "delivery_id": "del-5", "request_id": "req-5", "expected_process_generation": "gen-1", "parent_contribution_ids": []}

    # 1. Contributed slot rejection of forged session_failed
    forged_contributed = make_receipt("taey-council-2", "evidence-reality", "req-2", "del-2", "session_failed", session_failure_sha256, None, "side_effect_uncertain")
    transport.redis.lrange.return_value = [json.dumps(forged_contributed).encode("utf-8")]
    try:
        transport._matching_outcome(seat_2, request=req_2)
        require(False, "Failed to reject forged session_failed against contributed slot")
    except native_council.CouncilTransportFailure:
        print("PASS: Forged session_failed against contributed slot rejected")

    # 2. Failed terminal slot rejection of forged session_failed
    forged_failed = make_receipt("taey-council-4", "adversarial-failure", "req-4", "del-4", "session_failed", session_failure_sha256, None, "side_effect_uncertain")
    transport.redis.lrange.return_value = [json.dumps(forged_failed).encode("utf-8")]
    try:
        transport._matching_outcome(seat_4, request=req_4)
        require(False, "Failed to reject forged session_failed against failed slot")
    except native_council.CouncilTransportFailure:
        print("PASS: Forged session_failed against failed slot rejected")

    # 3. Claimed slot bad booleans (True or False instead of None + side_effect_uncertain)
    for bad_bool in (True, False):
        forged_claimed_bool = make_receipt("taey-council-1", "context-memory", "req-1", "del-1", "session_failed", session_failure_sha256, bad_bool, "side_effect_uncertain" if bad_bool else "not_started")
        transport.redis.lrange.return_value = [json.dumps(forged_claimed_bool).encode("utf-8")]
        try:
            transport._matching_outcome(seat_1, request=req_1)
            require(False, f"Failed to reject forged inference_performed={bad_bool} on claimed slot")
        except native_council.CouncilTransportFailure:
            print(f"PASS: Forged inference_performed={bad_bool} on claimed slot rejected")

    # 4. Pending slot bad booleans/states (True or side_effect_uncertain instead of False + not_started)
    forged_pending_true = make_receipt("taey-council-3", "systems-dependencies", "req-3", "del-3", "session_failed", session_failure_sha256, True, "not_started", observed_gen=None, served_al=None)
    transport.redis.lrange.return_value = [json.dumps(forged_pending_true).encode("utf-8")]
    try:
        transport._matching_outcome(seat_3, request=req_3)
        require(False, "Failed to reject forged inference_performed=True on pending slot")
    except native_council.CouncilTransportFailure:
        print("PASS: Forged inference_performed=True on pending slot rejected")

    forged_pending_uncertain = make_receipt("taey-council-3", "systems-dependencies", "req-3", "del-3", "session_failed", session_failure_sha256, False, "side_effect_uncertain", observed_gen=None, served_al=None)
    transport.redis.lrange.return_value = [json.dumps(forged_pending_uncertain).encode("utf-8")]
    try:
        transport._matching_outcome(seat_3, request=req_3)
        require(False, "Failed to reject forged inference_state=side_effect_uncertain on pending slot")
    except native_council.CouncilTransportFailure:
        print("PASS: Forged inference_state=side_effect_uncertain on pending slot rejected")

    # 5. Valid claimed slot session_failed
    valid_claimed = make_receipt("taey-council-1", "context-memory", "req-1", "del-1", "session_failed", session_failure_sha256, None, "side_effect_uncertain")
    transport.redis.lrange.return_value = [json.dumps(valid_claimed).encode("utf-8")]
    out_claimed = transport._matching_outcome(seat_1, request=req_1)
    require(
        out_claimed["ok"] is False
        and out_claimed["kind"] == "dcm_session_failed"
        and out_claimed["inference_state"] == "side_effect_uncertain",
        "Valid claimed session_failed outcome did not match expected structure",
    )
    print("PASS: Valid claimed session_failed accepted with side_effect_uncertain")

    # 6. Valid pending slot session_failed
    valid_pending = make_receipt("taey-council-3", "systems-dependencies", "req-3", "del-3", "session_failed", session_failure_sha256, False, "not_started", observed_gen=None, served_al=None)
    transport.redis.lrange.return_value = [json.dumps(valid_pending).encode("utf-8")]
    out_pending = transport._matching_outcome(seat_3, request=req_3)
    require(
        out_pending["ok"] is False
        and out_pending["kind"] == "dcm_session_failed"
        and out_pending["inference_state"] == "not_started",
        "Valid pending session_failed outcome did not match expected structure",
    )
    print("PASS: Valid pending session_failed accepted with not_started")

    # 7. Valid contributed slot contribution
    valid_contrib = make_receipt(
        "taey-council-2",
        "evidence-reality",
        "req-2",
        "del-2",
        "contributed",
        contrib_obj["contribution_receipt_sha256"],
        True,
        contrib_id="contrib-2",
        contribution_receipt_sha256=contrib_obj["contribution_receipt_sha256"],
    )
    transport.redis.lrange.return_value = [json.dumps(valid_contrib).encode("utf-8")]
    out_contrib = transport._matching_outcome(seat_2, request=req_2)
    require(
        out_contrib["ok"] is True
        and out_contrib["kind"] == "council_contribution"
        and out_contrib["inference_state"] == "completed"
        and out_contrib["contribution"] == contrib_obj["structured_content"],
        "Valid contribution outcome did not match expected structure",
    )
    print("PASS: Valid contribution accepted with completed")

    # 8. Injected inference_state on contributed receipt cannot override completed
    injected_contrib = make_receipt(
        "taey-council-2",
        "evidence-reality",
        "req-2",
        "del-2",
        "contributed",
        contrib_obj["contribution_receipt_sha256"],
        True,
        inf_state="side_effect_uncertain",
        contrib_id="contrib-2",
        contribution_receipt_sha256=contrib_obj["contribution_receipt_sha256"],
    )
    transport.redis.lrange.return_value = [json.dumps(injected_contrib).encode("utf-8")]
    out_injected = transport._matching_outcome(seat_2, request=req_2)
    require(
        out_injected["ok"] is True and out_injected["inference_state"] == "completed",
        "Injected inference_state bypassed local completed derivation",
    )
    print("PASS: Injected inference_state on contributed receipt cannot override local completed")

    # 9. Valid ordinary graph outcome_record failure (not_started)
    valid_failed_not_started = make_receipt(
        "taey-council-4",
        "adversarial-failure",
        "req-4",
        "del-4",
        "model_identity_unproven",
        outcome_record_failed["outcome_record_sha256"],
        False,
        failure_stage="model_identity_unproven",
        failure_detail_sha=outcome_record_failed["failure_detail_sha256"],
    )
    transport.redis.lrange.return_value = [json.dumps(valid_failed_not_started).encode("utf-8")]
    out_failed_ns = transport._matching_outcome(seat_4, request=req_4)
    require(
        out_failed_ns["ok"] is False
        and out_failed_ns["kind"] == "dcm_model_identity_unproven"
        and out_failed_ns["inference_state"] == "not_started",
        "Valid ordinary failure (not_started) outcome did not match expected structure",
    )
    print("PASS: Valid ordinary outcome_record failure accepted with not_started")

    # 10. Injected inference_state on ordinary failure cannot override local not_started derivation
    injected_failed_ns = make_receipt(
        "taey-council-4",
        "adversarial-failure",
        "req-4",
        "del-4",
        "model_identity_unproven",
        outcome_record_failed["outcome_record_sha256"],
        False,
        inf_state="completed",
        failure_stage="model_identity_unproven",
        failure_detail_sha=outcome_record_failed["failure_detail_sha256"],
    )
    transport.redis.lrange.return_value = [json.dumps(injected_failed_ns).encode("utf-8")]
    out_injected_ns = transport._matching_outcome(seat_4, request=req_4)
    require(
        out_injected_ns["ok"] is False and out_injected_ns["inference_state"] == "not_started",
        "Injected inference_state bypassed local not_started derivation",
    )
    print("PASS: Injected inference_state on ordinary failure (not_started) cannot override local derivation")

    # 11. Valid ordinary graph outcome_record failure with inference_performed=True (failed)
    valid_failed_inferred = make_receipt(
        "taey-council-5",
        "scope-intent",
        "req-5",
        "del-5",
        "inference_timeout",
        outcome_record_failed_inferred["outcome_record_sha256"],
        True,
        failure_stage="inference_timeout",
        failure_detail_sha=outcome_record_failed_inferred["failure_detail_sha256"],
    )
    transport.redis.lrange.return_value = [json.dumps(valid_failed_inferred).encode("utf-8")]
    out_failed_inf = transport._matching_outcome(seat_5, request=req_5)
    require(
        out_failed_inf["ok"] is False
        and out_failed_inf["kind"] == "dcm_inference_timeout"
        and out_failed_inf["inference_state"] == "failed",
        "Valid ordinary failure (failed) outcome did not match expected structure",
    )
    print("PASS: Valid ordinary outcome_record failure with inference_performed=True accepted with failed")

    # 12. Injected inference_state on ordinary failure (failed) cannot override local failed derivation
    injected_failed_inf = make_receipt(
        "taey-council-5",
        "scope-intent",
        "req-5",
        "del-5",
        "inference_timeout",
        outcome_record_failed_inferred["outcome_record_sha256"],
        True,
        inf_state="not_started",
        failure_stage="inference_timeout",
        failure_detail_sha=outcome_record_failed_inferred["failure_detail_sha256"],
    )
    transport.redis.lrange.return_value = [json.dumps(injected_failed_inf).encode("utf-8")]
    out_injected_inf = transport._matching_outcome(seat_5, request=req_5)
    require(
        out_injected_inf["ok"] is False and out_injected_inf["inference_state"] == "failed",
        "Injected inference_state bypassed local failed derivation",
    )
    print("PASS: Injected inference_state on ordinary failure (failed) cannot override local derivation")

    # 13. Mismatched / stale DCM wave missing session_failure is rejected (fails closed)
    stale_wave_missing_failure = dict(wave_fixture)
    stale_wave_missing_failure["session_failure"] = None
    adapter_mock.mesh.read_wave = MagicMock(return_value=stale_wave_missing_failure)
    transport.redis.lrange.return_value = [json.dumps(valid_claimed).encode("utf-8")]
    try:
        transport._matching_outcome(seat_1, request=req_1)
        require(False, "Stale DCM missing session_failure was not rejected")
    except native_council.CouncilTransportFailure:
        print("PASS: Stale DCM missing session_failure rejected (fails closed)")

    # 14. Mismatched / stale DCM wave with mismatched session_failure_sha256 is rejected
    stale_wave_mismatched_sha = dict(wave_fixture)
    stale_wave_mismatched_sha["session_failure_sha256"] = "sha256:" + "0" * 64
    adapter_mock.mesh.read_wave = MagicMock(return_value=stale_wave_mismatched_sha)
    transport.redis.lrange.return_value = [json.dumps(valid_claimed).encode("utf-8")]
    try:
        transport._matching_outcome(seat_1, request=req_1)
        require(False, "Stale DCM with mismatched failure digest was not rejected")
    except native_council.CouncilTransportFailure:
        print("PASS: Stale DCM with mismatched failure digest rejected (fails closed)")

    # 15. Mismatched / stale DCM wave with close_outcome != session_failed is rejected
    stale_wave_wrong_outcome = dict(wave_fixture)
    stale_wave_wrong_outcome["close_outcome"] = "incomplete_round"
    adapter_mock.mesh.read_wave = MagicMock(return_value=stale_wave_wrong_outcome)
    transport.redis.lrange.return_value = [json.dumps(valid_claimed).encode("utf-8")]
    try:
        transport._matching_outcome(seat_1, request=req_1)
        require(False, "Stale DCM with wrong close_outcome was not rejected")
    except native_council.CouncilTransportFailure:
        print("PASS: Stale DCM with wrong close_outcome rejected (fails closed)")

    # Reset wave mock
    adapter_mock.mesh.read_wave = MagicMock(return_value=wave_fixture)

    print("\n=== COUNCIL RECOVERY ANTI-FORGERY & MISMATCH VALIDATION: PASS ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
