"""Canonicalization vs byte-digest boundary for soma_proxy model requests.

Canonicalization is one JSON encoding: sorted keys, compact separators,
UTF-8, no NaN. The byte digest is sha256 of those exact bytes.

A reconstructed equivalent object is not a receipt. Bind fails closed when
the supplied outbound bytes are not identical to that encoding.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def encode_outbound_request_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def outbound_request_sha256(outbound_request_bytes: bytes) -> str:
    if not isinstance(outbound_request_bytes, (bytes, bytearray)):
        raise TypeError("outbound request digest requires raw bytes")
    return "sha256:" + hashlib.sha256(bytes(outbound_request_bytes)).hexdigest()


def bind_outbound_request_bytes(
    model_request: Any,
    outbound_request_bytes: bytes,
) -> bytes:
    if not isinstance(outbound_request_bytes, (bytes, bytearray)):
        raise ValueError("outbound request bytes must be raw bytes")
    outbound = bytes(outbound_request_bytes)
    try:
        reconstructed = json.loads(outbound)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("outbound request bytes are not JSON") from exc
    encoded = encode_outbound_request_bytes(model_request)
    if outbound != encoded:
        raise ValueError(
            "outbound request bytes drifted from the canonical model request"
        )
    if reconstructed != model_request:
        raise ValueError(
            "outbound request bytes reconstructed a different model request"
        )
    if encode_outbound_request_bytes(reconstructed) != outbound:
        raise ValueError("outbound request bytes are not a stable encoding")
    return outbound
