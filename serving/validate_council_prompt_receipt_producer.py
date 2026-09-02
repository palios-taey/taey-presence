#!/usr/bin/env python3
"""Exercise the council receipt producer; this is not an authority verifier."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import council_prompt_receipt as producer


ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "council_seats.json"


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise RuntimeError(detail)


def install_import_only_redis_stub() -> None:
    if importlib.util.find_spec("redis") is not None:
        return
    redis_stub = ModuleType("redis")
    redis_stub.Redis = object
    sys.modules["redis"] = redis_stub


def install_import_only_dcm_stub() -> None:
    if importlib.util.find_spec("taey_adapter") is not None:
        return
    sys.modules["taey_adapter"] = ModuleType("taey_adapter")


def main() -> int:
    manifest = producer.load_manifest(MANIFEST_PATH)
    require(len(manifest.seats) == 7, "production manifest seat count changed")
    source_receipt = producer.static_prompt_sources(manifest)
    require(len(source_receipt) == len(manifest.seats) + 1, "source set is incomplete")
    require(
        [source["seat_id"] for source in source_receipt[:-1]]
        == [seat.seat_id for seat in manifest.seats],
        "role prompt sources are not in manifest order",
    )
    require(
        source_receipt[-1]["source_kind"] == "shared",
        "shared prompt is not the final static source",
    )

    seat = manifest.seats[0]
    shared = seat.shared_prompt.read_text(encoding="utf-8").strip()
    role = seat.role_prompt.read_text(encoding="utf-8").strip()
    legacy_contract = (
        f"Immutable runtime identity: seat_id={seat.seat_id}; "
        f"role_id={seat.role_id}; "
        f"response_contract={producer.RESPONSE_CONTRACT}; "
        f"role_contract_revision={producer.ROLE_CONTRACT_REVISION}.\n\n"
        f"{shared}\n\n{role}"
    )
    require(
        producer.seat_role_contract(seat) == legacy_contract,
        "legacy role contract rendering changed",
    )
    legacy_system_message = {
        "role": "system",
        "content": (
            "[COUNCIL ROLE CONTRACT]\n"
            f"{legacy_contract}\n"
            "[/COUNCIL ROLE CONTRACT]"
        ),
    }
    require(
        producer.system_message(seat) == legacy_system_message,
        "legacy system wrapper rendering changed",
    )

    static_receipt = producer.prompt_contract_receipt(manifest, seat)
    require(
        static_receipt["producer_state"] == "self_asserted_unverified",
        "producer artifact overstates its authority",
    )
    require(
        static_receipt["prompt_contract_sha256"]
        == producer.canonical_sha256(static_receipt["prompt_contract"]),
        "static prompt contract digest is not canonical",
    )

    with tempfile.TemporaryDirectory() as manifest_temporary:
        manifest_root = Path(manifest_temporary)
        shutil.copy2(MANIFEST_PATH, manifest_root / MANIFEST_PATH.name)
        shutil.copytree(ROOT / "council_prompts", manifest_root / "council_prompts")
        copied_manifest_path = manifest_root / MANIFEST_PATH.name
        copied = producer.load_manifest(copied_manifest_path)
        copied_baseline = producer.prompt_contract_receipt(
            copied,
            copied.seats[0],
        )["prompt_contract_sha256"]
        source_paths = [seat.role_prompt for seat in copied.seats]
        source_paths.append(copied.seats[0].shared_prompt)
        for source_path in source_paths:
            original = source_path.read_text(encoding="utf-8")
            source_path.write_text(original + "\nsource tamper\n", encoding="utf-8")
            changed = producer.load_manifest(copied_manifest_path)
            changed_digest = producer.prompt_contract_receipt(
                changed,
                changed.seats[0],
            )["prompt_contract_sha256"]
            require(
                changed_digest != copied_baseline,
                f"static source tamper was not bound: {source_path.name}",
            )
            source_path.write_text(original, encoding="utf-8")

        copied_document = json.loads(copied_manifest_path.read_text(encoding="utf-8"))
        copied_document["seats"] = copied_document["seats"][:-1]
        copied_manifest_path.write_text(
            json.dumps(copied_document, indent=2) + "\n",
            encoding="utf-8",
        )
        reduced = producer.load_manifest(copied_manifest_path)
        require(
            len(reduced.seats) == 6,
            "manifest-derived seat set was replaced by a hard-coded seat table",
        )
        require(
            producer.prompt_contract_receipt(reduced, reduced.seats[0])[
                "prompt_contract_sha256"
            ]
            != copied_baseline,
            "manifest seat removal was not bound",
        )

    lineage = {
        "request_contract": producer.DCM_REQUEST_CONTRACT,
        "request_id": "dcm-request-1",
        "council_run_id": "dcm-round-1",
        "round_id": "dcm-round-1",
        "prompt_revision": 1,
        "prompt_contract_sha256": static_receipt["prompt_contract_sha256"],
        "model_identity_receipt_sha256": "sha256:" + ("a" * 64),
        "evidence_registry": ["fleet_message:m1"],
    }
    claim = SimpleNamespace(
        source=SimpleNamespace(name="inbox"),
        message_id="m1",
        raw='{"body":"exact evidence"}',
        payload={"attachments": []},
    )
    response_format = producer.response_format(
        seat,
        lineage["prompt_revision"],
        lineage["evidence_registry"],
    )
    request = {
        "model": "ep3",
        "messages": [
            producer.system_message(seat),
            {"role": "user", "content": "exact dynamic wave body"},
        ],
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": response_format,
    }
    receipt = producer.model_request_receipt(
        manifest=manifest,
        seat=seat,
        lineage=lineage,
        model_request=request,
        claims=[claim],
    )
    repeated = producer.model_request_receipt(
        manifest=manifest,
        seat=seat,
        lineage=lineage,
        model_request=request,
        claims=[claim],
    )
    require(receipt == repeated, "producer output is not deterministic")
    unsigned = dict(receipt)
    receipt_sha256 = unsigned.pop("receipt_sha256")
    require(
        receipt_sha256 == producer.canonical_sha256(unsigned),
        "model request receipt digest is not canonical",
    )
    require(
        receipt["model_request_sha256"]
        == producer.canonical_sha256(receipt["model_request"]),
        "model request digest does not bind the exact request",
    )
    require(
        receipt["attachments"] == {"state": "none", "items": []},
        "no-attachment state is not explicit",
    )

    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        temporary_path.chmod(0o700)
        os.environ.update(
            {
                "TAEY_SESSION_NAME": seat.seat_id,
                "TAEY_CONVERSATION_ID": seat.conversation_id,
                "TAEY_COUNCIL_ROLE_ID": seat.role_id,
                "TAEY_COUNCIL_MANIFEST_PATH": str(MANIFEST_PATH),
                "TAEY_COUNCIL_SHARED_PROMPT_PATH": str(seat.shared_prompt),
                "TAEY_COUNCIL_ROLE_PROMPT_PATH": str(seat.role_prompt),
                "TAEY_EXECUTIVE_EVENT_LOG": str(
                    temporary_path / f"{seat.seat_id}.jsonl"
                ),
                "TAEY_MODEL": "ep3",
            }
        )
        install_import_only_redis_stub()
        install_import_only_dcm_stub()
        import taey_council_seat as runtime

        runtime_manifest, runtime_seat, runtime_contract = (
            runtime._validate_seat_contract()
        )
        store = runtime.CouncilEventStore(
            runtime.executive.EVENT_LOG,
            runtime.executive.MAX_TURNS,
            runtime_manifest,
            runtime_seat,
            runtime_contract,
        )
        legacy_contract_version, legacy_digest = runtime._request_contract([], store)
        require(legacy_contract_version is None, "legacy request gained a contract opt-in")
        require(
            legacy_digest
            == producer.text_sha256(legacy_contract).removeprefix("sha256:"),
            "legacy prompt digest changed",
        )
        require(
            store.messages_for("legacy prompt")
            == [legacy_system_message, {"role": "user", "content": "legacy prompt"}],
            "legacy message list changed",
        )

        runtime_claim = SimpleNamespace(
            source=SimpleNamespace(name="inbox"),
            message_id="m1",
            raw='{"body":"exact evidence"}',
            payload={
                "type": "council_request",
                "body": "exact evidence",
                "delivery_id": "m1",
                "request_id": "dcm-request-1",
                "council_run_id": "dcm-round-1",
                "round_id": "dcm-round-1",
                "dcm_session_id": "dcm-round-1",
                "wave_id": "wave-dcm-1",
                "round": 1,
                "phase": "independent",
                "prompt_id": "prompt-1",
                "prompt_revision": 1,
                "prompt_sha256": "sha256:" + ("b" * 64),
                "seat_id": seat.seat_id,
                "role": seat.role_id,
                "request_revision": 1,
                "parent_contribution_ids": [],
                "parent_frontier_sha256": "sha256:" + ("c" * 64),
                "process_generation_expected": runtime.PROCESS_GENERATION,
                "expected_process_generation": runtime.PROCESS_GENERATION,
                "model_endpoint": runtime.executive.PROXY_URL,
                "requested_alias": runtime.executive.MODEL,
                "model_manifest_sha256": "sha256:" + ("d" * 64),
                "model_content_sha256": "sha256:" + ("e" * 64),
                "serving_container_digest": "sha256:" + ("f" * 64),
                "request_contract": producer.DCM_REQUEST_CONTRACT,
                "prompt_contract_sha256": store.dcm_v2_prompt_contract_sha256,
                "model_identity_receipt_sha256": "sha256:" + ("a" * 64),
                "attachments": [],
            },
        )
        runtime_request_contract, runtime_prompt_digest = runtime._request_contract(
            [runtime_claim],
            store,
        )
        runtime_lineage = runtime._response_lineage(
            [runtime_claim],
            "dcm-request-1",
            "dcm-round-1",
            runtime_prompt_digest,
            runtime_request_contract,
        )
        runtime_lineage["evidence_registry"] = store.evidence_registry(
            [runtime_claim],
            "dcm-request-1",
            runtime_prompt_digest,
        )
        runtime_prompt = runtime._prompt_for(
            "[NOTIFY] You have 1 messages",
            [runtime_claim],
            runtime_lineage,
        )
        runtime_messages = store.messages_for(runtime_prompt)
        runtime_format = runtime._contribution_response_format(
            runtime_lineage,
            runtime_seat,
        )
        runtime_request = runtime.executive.ProxyClient.model_request_body(
            runtime_messages,
            runtime_format,
        )
        runtime_receipt = producer.model_request_receipt(
            manifest=runtime_manifest,
            seat=runtime_seat,
            lineage=runtime_lineage,
            model_request=runtime_request,
            claims=[runtime_claim],
        )
        require(
            runtime_receipt["model_request"] == runtime_request,
            "runtime receipt does not contain the exact ProxyClient request",
        )
        require(
            runtime_receipt["prompt_contract_sha256"]
            == store.dcm_v2_prompt_contract_sha256,
            "runtime receipt does not bind its produced prompt contract",
        )

    changed_request = copy.deepcopy(request)
    changed_request["messages"][1]["content"] += " changed"
    changed_receipt = producer.model_request_receipt(
        manifest=manifest,
        seat=seat,
        lineage=lineage,
        model_request=changed_request,
        claims=[claim],
    )
    require(
        changed_receipt["receipt_sha256"] != receipt["receipt_sha256"],
        "dynamic message tamper did not change the receipt",
    )

    changed_claim = SimpleNamespace(
        source=claim.source,
        message_id=claim.message_id,
        raw='{"body":"changed evidence"}',
        payload=claim.payload,
    )
    changed_evidence_receipt = producer.model_request_receipt(
        manifest=manifest,
        seat=seat,
        lineage=lineage,
        model_request=request,
        claims=[changed_claim],
    )
    require(
        changed_evidence_receipt["receipt_sha256"] != receipt["receipt_sha256"],
        "evidence tamper did not change the receipt",
    )

    bad_request = copy.deepcopy(request)
    bad_request["messages"][0]["content"] += " changed"
    try:
        producer.model_request_receipt(
            manifest=manifest,
            seat=seat,
            lineage=lineage,
            model_request=bad_request,
            claims=[claim],
        )
    except ValueError as exc:
        require("exact rendered council messages" in str(exc), "wrong tamper rejection")
    else:
        raise RuntimeError("system-message tamper was accepted")

    attachment_claim = SimpleNamespace(
        source=claim.source,
        message_id=claim.message_id,
        raw=claim.raw,
        payload={"attachments": [{"sha256": "sha256:" + ("b" * 64)}]},
    )
    try:
        producer.model_request_receipt(
            manifest=manifest,
            seat=seat,
            lineage=lineage,
            model_request=request,
            claims=[attachment_claim],
        )
    except ValueError as exc:
        require("do not support attachments" in str(exc), "wrong attachment rejection")
    else:
        raise RuntimeError("unsupported attachment was accepted")

    print(
        json.dumps(
            {
                "status": "PASS",
                "contract": producer.MODEL_REQUEST_RECEIPT_CONTRACT,
                "seat_count": len(manifest.seats),
                "static_source_count": len(source_receipt),
                "prompt_contract_sha256": static_receipt[
                    "prompt_contract_sha256"
                ],
                "receipt_sha256": receipt["receipt_sha256"],
                "legacy_rendering_unchanged": True,
                "producer_authority": "self_asserted_unverified",
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
