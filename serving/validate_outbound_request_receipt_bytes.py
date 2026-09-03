#!/usr/bin/env python3
"""Required gate: mutating soma_proxy outbound bytes invalidates the receipt.

This is not production evidence. It proves the bind is over the exact bytes
the codec/soma_proxy send path emits, and that a reconstructed equivalent
body cannot keep a receipt valid.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest import mock

if "redis" not in sys.modules and importlib.util.find_spec("redis") is None:
    redis_stub = ModuleType("redis")
    redis_stub.Redis = mock.MagicMock
    sys.modules["redis"] = redis_stub

if __package__:
    from . import council_prompt_receipt as producer
    from .outbound_request_codec import (
        bind_outbound_request_bytes,
        encode_outbound_request_bytes,
    )
else:
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
        "max_tokens": producer.COUNCIL_MAX_COMPLETION_TOKENS,
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
    prove_canonical_codec_golden_bytes()
    prove_proxy_client_ask_binds_outbound_bytes()
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


def prove_canonical_codec_golden_bytes() -> None:
    require(
        producer.MODEL_REQUEST_RECEIPT_CONTRACT
        == "taey-council-model-request-producer-receipt/v2",
        f"expected receipt contract v2, got {producer.MODEL_REQUEST_RECEIPT_CONTRACT}",
    )
    test_payload = {
        "z_key": 123,
        "a_key": "simple",
        "nested": {"beta": [3, 2, 1], "alpha": True},
        "unicode_text": "日本語 / ñoño / §¶",
        "empty_list": [],
    }
    # Exact golden bytes with sorted keys, compact separators, UTF-8 (no ascii escapes)
    expected_golden = (
        b'{"a_key":"simple","empty_list":[],"nested":{"alpha":true,"beta":[3,2,1]},'
        b'"unicode_text":"\xe6\x97\xa5\xe6\x9c\xac\xe8\xaa\x9e / \xc3\xb1o\xc3\xb1o / \xc2\xa7\xc2\xb6","z_key":123}'
    )
    actual_bytes = encode_outbound_request_bytes(test_payload)
    require(
        actual_bytes == expected_golden,
        f"encode_outbound_request_bytes drifted from golden bytes:\nexpected: {expected_golden!r}\nactual:   {actual_bytes!r}",
    )


def prove_proxy_client_ask_binds_outbound_bytes() -> None:
    import taey_seat

    # Hermetic safety barrier: urlopen MUST NEVER be called during test execution.
    # If the outbound bind is bypassed (e.g. during mutation testing), urlopen fails fast in 0ms.
    client = taey_seat.ProxyClient()
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user query"},
    ]
    mismatched_request = {
        "model": taey_seat.MODEL,
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "DIFFERENT query"},
        ],
        "chat_template_kwargs": {"enable_thinking": False},
    }
    mismatched_bytes = encode_outbound_request_bytes(mismatched_request)

    def proxy_response(tool_profile: str):
        response = mock.MagicMock()
        response.read.return_value = json.dumps({
            "choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")
        response.headers = {
            "X-Taey-Turn-Id": "turn-1",
            "X-Taey-Event-Id": "evt-1",
            "X-Taey-Correlation-Id": "corr-1",
            "X-Taey-Tool-Profile": tool_profile,
        }
        response.__enter__.return_value = response
        return response

    bounded_request = client.model_request_body(
        messages,
        max_rounds=producer.COUNCIL_MAX_TOOL_ROUNDS,
        max_tokens=producer.COUNCIL_MAX_COMPLETION_TOKENS,
    )
    bounded_bytes = encode_outbound_request_bytes(bounded_request)
    with mock.patch(
        "urllib.request.urlopen",
        return_value=proxy_response(producer.COUNCIL_TOOL_PROFILE),
    ) as urlopen:
        client.ask(
            prompt="user query",
            event_id="evt-1",
            correlation_id="corr-1",
            messages=messages,
            max_rounds=producer.COUNCIL_MAX_TOOL_ROUNDS,
            max_tokens=producer.COUNCIL_MAX_COMPLETION_TOKENS,
            tool_profile=producer.COUNCIL_TOOL_PROFILE,
            outbound_request_bytes=bounded_bytes,
        )
        sent = urlopen.call_args.args[0]
        headers = {key.lower(): value for key, value in sent.header_items()}
        require(
            headers.get("x-taey-tool-profile") == producer.COUNCIL_TOOL_PROFILE,
            "ProxyClient did not send the receipted council tool profile",
        )

    with mock.patch(
        "urllib.request.urlopen",
        return_value=proxy_response("full"),
    ):
        try:
            client.ask(
                prompt="user query",
                event_id="evt-1",
                correlation_id="corr-1",
                messages=messages,
                max_rounds=producer.COUNCIL_MAX_TOOL_ROUNDS,
                max_tokens=producer.COUNCIL_MAX_COMPLETION_TOKENS,
                tool_profile=producer.COUNCIL_TOOL_PROFILE,
                outbound_request_bytes=bounded_bytes,
            )
        except taey_seat.SeatFailure as exc:
            require(
                "tool-profile mismatch" in str(exc),
                f"unexpected tool-profile failure: {exc}",
            )
        else:
            raise RuntimeError("ProxyClient accepted a mismatched tool-profile echo")

    with (
        mock.patch(
            "urllib.request.urlopen",
            side_effect=AssertionError(
                "urlopen reached: outbound request bind bypassed"
            ),
        ),
        mock.patch.object(
            taey_seat,
            "PROXY_URL",
            "http://127.0.0.1:9999/hermetic-test-only",
        ),
    ):
        # Driving ask() with mismatched bytes MUST raise SeatFailure
        try:
            client.ask(
                prompt="user query",
                event_id="evt-1",
                correlation_id="corr-1",
                messages=messages,
                outbound_request_bytes=mismatched_bytes,
            )
        except taey_seat.SeatFailure as exc:
            require("drifted" in str(exc), f"unexpected SeatFailure message: {exc}")
        else:
            raise RuntimeError(
                "ProxyClient.ask() accepted mismatched outbound_request_bytes without SeatFailure"
            )

        # Non-bytes outbound_request_bytes MUST raise SeatFailure
        try:
            client.ask(
                prompt="user query",
                event_id="evt-1",
                correlation_id="corr-1",
                messages=messages,
                outbound_request_bytes="not-bytes",  # type: ignore
            )
        except taey_seat.SeatFailure:
            pass
        else:
            raise RuntimeError(
                "ProxyClient.ask() accepted non-bytes outbound_request_bytes without SeatFailure"
            )


if __name__ == "__main__":
    raise SystemExit(main())
