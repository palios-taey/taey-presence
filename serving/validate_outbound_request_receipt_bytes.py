#!/usr/bin/env python3
"""Required gate: mutating soma_proxy outbound bytes invalidates the receipt.

This is not production evidence. It proves the bind is over the exact bytes
the codec/soma_proxy send path emits, and that a reconstructed equivalent
body cannot keep a receipt valid.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import council_prompt_receipt as producer
from outbound_request_codec import (
    bind_outbound_request_bytes,
    encode_outbound_request_bytes,
)


ROOT = Path(__file__).resolve().parent


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise RuntimeError(detail)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return ""


def prove_soma_proxy_posts_encoded_bytes() -> None:
    tree = ast.parse((ROOT / "soma_proxy.py").read_text(encoding="utf-8"))
    posts = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
        if name in {"_http.post", "_http.stream"}:
            args = [
                ast.literal_eval(arg)
                if isinstance(arg, ast.Constant)
                else None
                for arg in node.args
            ]
            if "/v1/chat/completions" in args or (
                len(args) >= 2 and args[1] == "/v1/chat/completions"
            ):
                posts.append((node.lineno, name, keywords))
        if name == "_post_vllm_chat_completions":
            posts.append((node.lineno, name, keywords))
    require(posts, "soma_proxy has no vLLM chat-completion send sites")
    for lineno, name, keywords in posts:
        if name in {"_http.post", "_http.stream"}:
            require(
                "json" not in keywords,
                f"soma_proxy:{lineno} {name} still serializes via json=",
            )
            require(
                "content" in keywords,
                f"soma_proxy:{lineno} {name} does not send exact content bytes",
            )
    helper_fn = None
    encoder_fn = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_post_vllm_chat_completions":
                helper_fn = node
            if node.name == "encode_vllm_outbound_request_bytes":
                encoder_fn = node
    require(helper_fn is not None, "_post_vllm_chat_completions is missing")
    require(encoder_fn is not None, "encode_vllm_outbound_request_bytes is missing")
    require(
        any(
            isinstance(item, ast.Call)
            and _call_name(item.func) == "encode_vllm_outbound_request_bytes"
            for item in ast.walk(helper_fn)
        ),
        "_post_vllm_chat_completions does not encode outbound bytes",
    )
    encoder_calls = {
        _call_name(item.func)
        for item in ast.walk(encoder_fn)
        if isinstance(item, ast.Call)
    }
    require(
        "encode_outbound_request_bytes" in encoder_calls
        and "bind_outbound_request_bytes" in encoder_calls,
        "soma_proxy outbound encoder is not the receipt codec bind",
    )


def main() -> int:
    manifest = producer.load_manifest(ROOT / "council_seats.json")
    seat = manifest.seats[0]
    static_receipt = producer.prompt_contract_receipt(manifest, seat)
    lineage = {
        "request_contract": producer.DCM_REQUEST_CONTRACT,
        "request_id": "dcm-request-1",
        "council_run_id": "dcm-round-1",
        "round_id": "dcm-round-1",
        "prompt_revision": 1,
        "model_identity_receipt_sha256": "sha256:" + ("a" * 64),
        "prompt_contract_sha256": static_receipt["prompt_contract_sha256"],
        "evidence_registry": ("evidence-1",),
    }
    claim = type(
        "Claim",
        (),
        {
            "source": type("Source", (), {"name": "inbox"})(),
            "message_id": "m1",
            "raw": '{"body":"exact evidence"}',
            "payload": {"attachments": []},
        },
    )()
    request = {
        "model": "ep3",
        "messages": [
            producer.system_message(seat),
            {"role": "user", "content": "exact dynamic wave body"},
        ],
        "chat_template_kwargs": {"enable_thinking": False},
        "max_rounds": producer.COUNCIL_MAX_TOOL_ROUNDS,
        "response_format": producer.response_format(
            seat,
            lineage["prompt_revision"],
            lineage["evidence_registry"],
        ),
    }
    outbound = encode_outbound_request_bytes(request)
    receipt = producer.model_request_receipt(
        manifest=manifest,
        seat=seat,
        lineage=lineage,
        model_request=request,
        outbound_request_bytes=outbound,
        claims=[claim],
    )
    producer.verify_model_request_receipt_outbound(receipt, outbound)

    reconstructed = json.dumps(request).encode("utf-8")
    require(
        reconstructed != outbound,
        "default json.dumps already matched canonical outbound bytes",
    )
    try:
        bind_outbound_request_bytes(request, reconstructed)
    except ValueError as exc:
        require("drifted" in str(exc), "reconstructed equivalent body was not fail-closed")
    else:
        raise RuntimeError("reconstructed equivalent outbound body was accepted")
    try:
        producer.verify_model_request_receipt_outbound(receipt, reconstructed)
    except ValueError:
        pass
    else:
        raise RuntimeError("mutating outbound bytes did not invalidate the receipt")

    pretty = json.dumps(request, indent=2, ensure_ascii=False).encode("utf-8")
    try:
        producer.verify_model_request_receipt_outbound(receipt, pretty)
    except ValueError:
        pass
    else:
        raise RuntimeError("pretty-printed outbound bytes still verified")

    flipped = outbound[:-1] + bytes([outbound[-1] ^ 1])
    try:
        producer.verify_model_request_receipt_outbound(receipt, flipped)
    except ValueError:
        pass
    else:
        raise RuntimeError("single-byte outbound mutation still verified")

    prove_soma_proxy_posts_encoded_bytes()
    print(
        json.dumps(
            {
                "status": "PASS",
                "contract": producer.MODEL_REQUEST_RECEIPT_CONTRACT,
                "outbound_request_sha256": receipt["outbound_request_sha256"],
                "receipt_sha256": receipt["receipt_sha256"],
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
