#!/usr/bin/env python3
"""Dedicated inference and execution path for supervised non-UI turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

import httpx

try:
    from .supervised_capture import (
        CaptureError,
        SUPERVISED_TOOLS,
        SupervisedTrace,
    )
except ImportError:
    from supervised_capture import (
        CaptureError,
        SUPERVISED_TOOLS,
        SupervisedTrace,
    )


@dataclass(frozen=True)
class SupervisedCompletion:
    payload: dict[str, Any]
    status_code: int
    prompt_tokens: int
    completion_tokens: int
    tool_rounds: int
    trace_id: str


def _exact_request_bytes(body: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CaptureError("resolved upstream request is not exact JSON") from exc


def _response_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = json.loads(response.content)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CaptureError("upstream model response is not exact JSON") from exc
    if not isinstance(payload, dict):
        raise CaptureError("upstream model response must be a JSON object")
    return payload


async def _resolved_model_identity(
    client: httpx.AsyncClient,
    headers: dict[str, str],
) -> dict[str, Any]:
    response = await client.get("/v1/models", headers=headers)
    if not 200 <= response.status_code < 300:
        raise CaptureError(
            f"upstream model catalogue returned HTTP {response.status_code}"
        )
    catalogue = _response_object(response)
    models = catalogue.get("data")
    if not isinstance(models, list) or len(models) != 1:
        raise CaptureError("supervised capture requires exactly one loaded upstream model")
    selected = models[0]
    if (
        not isinstance(selected, dict)
        or not str(selected.get("id") or "").strip()
        or not str(selected.get("root") or "").strip()
    ):
        raise CaptureError("loaded upstream model lacks exact id/root provenance")
    return {
        "catalogue": catalogue,
        "catalogue_sha256": hashlib.sha256(response.content).hexdigest(),
        "selected": selected,
    }


def _model_settings(body: dict[str, Any]) -> dict[str, Any]:
    return {key: body[key] for key in sorted(body) if key != "messages"}


async def _post_model(
    *,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    trace: SupervisedTrace,
    body: dict[str, Any],
    round_num: int,
    phase: str,
    caller_model: Any,
    model_identity: dict[str, Any],
) -> tuple[httpx.Response, dict[str, Any]]:
    request_payload = _exact_request_bytes(body)
    model_call_id = trace.record_model_request(
        request_payload,
        round_num=round_num,
        phase=phase,
        caller_model=caller_model,
        model_identity=model_identity,
        model_settings=_model_settings(body),
    )
    response = await client.post(
        "/v1/chat/completions",
        content=request_payload,
        headers=headers,
    )
    trace.record_model_response(
        response.content,
        status_code=response.status_code,
        model_call_id=model_call_id,
    )
    return response, _response_object(response)


def _choice(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise CaptureError("upstream model response has no first choice")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise CaptureError("upstream model response has no exact message object")
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list) or any(
        not isinstance(tool_call, dict) for tool_call in tool_calls
    ):
        raise CaptureError("upstream model tool_calls must be an array of objects")
    return message, tool_calls


def _final_body(
    body: dict[str, Any],
    messages: list[Any],
    response_format: Any,
) -> dict[str, Any]:
    final_body = dict(body)
    final_body["messages"] = messages
    final_body.pop("tools", None)
    final_body["tool_choice"] = "none"
    if response_format is not None:
        final_body["response_format"] = response_format
    return final_body


async def run_supervised_completion(
    *,
    client: httpx.AsyncClient,
    caller_body: dict[str, Any],
    caller_request_bytes: bytes,
    upstream_headers: dict[str, str],
    capture_root: str,
    trace_id: str,
    source_ref: str,
    approval_wait_seconds: float,
    request_metadata: dict[str, Any],
    inject_preamble: Callable[[dict[str, Any]], dict[str, Any]],
    max_tool_rounds: int,
) -> SupervisedCompletion:
    trace: SupervisedTrace | None = None
    try:
        trace = SupervisedTrace.start(
            root=capture_root,
            trace_id=trace_id,
            request_bytes=caller_request_bytes,
            source_ref=source_ref,
            approval_wait_seconds=approval_wait_seconds,
            request_metadata=request_metadata,
        )
        caller_model = caller_body.get("model")
        body = dict(caller_body)
        body.pop("max_rounds", None)
        body.pop("model", None)
        body = inject_preamble(body)
        if not isinstance(body, dict):
            raise CaptureError("preamble transformation did not return a request object")
        if body.get("stream"):
            raise CaptureError("supervised non-UI capture refuses streaming requests")
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise CaptureError("resolved upstream request requires a messages array")
        body["tools"] = SUPERVISED_TOOLS
        body["tool_choice"] = "auto"
        held_response_format = body.pop("response_format", None)
        model_identity = await _resolved_model_identity(client, upstream_headers)

        round_num = 0
        response: httpx.Response | None = None
        result: dict[str, Any] = {}
        while True:
            phase = "initial" if round_num == 0 else "next"
            response, result = await _post_model(
                client=client,
                headers=upstream_headers,
                trace=trace,
                body=body,
                round_num=round_num,
                phase=phase,
                caller_model=caller_model,
                model_identity=model_identity,
            )
            message, tool_calls = _choice(result)
            if not tool_calls:
                if held_response_format is not None:
                    response, result = await _post_model(
                        client=client,
                        headers=upstream_headers,
                        trace=trace,
                        body=_final_body(body, messages, held_response_format),
                        round_num=round_num,
                        phase="final",
                        caller_model=caller_model,
                        model_identity=model_identity,
                    )
                break
            if round_num >= max_tool_rounds:
                response, result = await _post_model(
                    client=client,
                    headers=upstream_headers,
                    trace=trace,
                    body=_final_body(body, messages, held_response_format),
                    round_num=round_num,
                    phase="final",
                    caller_model=caller_model,
                    model_identity=model_identity,
                )
                break

            round_num += 1
            messages.append(message)
            for tool_call in tool_calls:
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    raise CaptureError("model tool call lacks a function object")
                name = str(function.get("name") or "")
                raw_arguments = function.get("arguments", {})
                if isinstance(raw_arguments, dict):
                    parsed_arguments = raw_arguments
                else:
                    try:
                        parsed_arguments = json.loads(raw_arguments) if raw_arguments else {}
                    except (json.JSONDecodeError, TypeError):
                        parsed_arguments = {}
                tool_result = await asyncio.to_thread(
                    trace.execute_tool_call,
                    name=name,
                    call_id=str(tool_call.get("id") or ""),
                    parsed_arguments=parsed_arguments,
                    raw_arguments=raw_arguments,
                    round_num=round_num,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(tool_call.get("id") or ""),
                        "content": tool_result,
                    }
                )
            body["messages"] = messages

        if response is None:
            raise CaptureError("supervised completion produced no upstream response")
        usage = result.get("usage") or {}
        if not isinstance(usage, dict):
            raise CaptureError("upstream usage must be an object")
        try:
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
        except (TypeError, ValueError) as exc:
            raise CaptureError("upstream usage token counts must be integers") from exc
        if prompt_tokens < 0 or completion_tokens < 0:
            raise CaptureError("upstream usage token counts cannot be negative")
        trace.complete(http_status=response.status_code)
        return SupervisedCompletion(
            payload=result,
            status_code=response.status_code,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tool_rounds=round_num,
            trace_id=trace.trace_id,
        )
    except BaseException as exc:
        if trace is not None:
            try:
                trace.fail(exc)
            except BaseException as capture_exc:
                raise RuntimeError(
                    "supervised capture could not durably record the failed turn"
                ) from capture_exc
        raise
