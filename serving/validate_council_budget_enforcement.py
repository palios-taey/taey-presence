#!/usr/bin/env python3
"""No-network chat-path and mutation gate for council inference budgets."""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

STRUCTURED_OUTPUTS_CONFIG = {
    "backend": "xgrammar",
    "disable_any_whitespace": True,
}
STRUCTURED_OUTPUTS_DECLARATION = (
    "readonly STRUCTURED_OUTPUTS_CONFIG="
    "'{\"backend\":\"xgrammar\",\"disable_any_whitespace\":true}'"
)
STRUCTURED_OUTPUTS_PREFLIGHT = (
    "'${ROOT}/serving/vllm_serve.sh' --validate-structured-outputs-config"
)
INDEX_WORKFLOW_TRIGGER_BLOCK = (
    "on:\n"
    "  pull_request:\n"
    "  push:\n"
    "    branches: [main]\n"
    "\n"
    "jobs:\n"
)


if importlib.util.find_spec("redis") is None:
    redis_stub = ModuleType("redis")
    redis_stub.Redis = mock.MagicMock
    sys.modules["redis"] = redis_stub

if importlib.util.find_spec("starlette") is None:
    starlette_stub = ModuleType("starlette")
    starlette_background_stub = ModuleType("starlette.background")
    starlette_background_stub.BackgroundTask = mock.MagicMock
    sys.modules["starlette"] = starlette_stub
    sys.modules["starlette.background"] = starlette_background_stub

if importlib.util.find_spec("httpx") is None:
    httpx_stub = ModuleType("httpx")
    httpx_stub.AsyncClient = mock.MagicMock
    httpx_stub.Client = mock.MagicMock
    httpx_stub.Response = object
    httpx_stub.Request = object
    httpx_stub.TimeoutException = type("TimeoutException", (Exception,), {})
    httpx_stub.RequestError = type("RequestError", (Exception,), {})
    httpx_stub.RemoteProtocolError = type("RemoteProtocolError", (Exception,), {})
    sys.modules["httpx"] = httpx_stub

if importlib.util.find_spec("fastapi") is None:
    fastapi_stub = ModuleType("fastapi")

    class StubHTTPException(Exception):
        def __init__(self, status_code: int, detail: object = None):
            self.status_code = status_code
            self.detail = detail
            super().__init__(f"{status_code}: {detail}")

    class StubApp:
        def __init__(self, *_args, **_kwargs):
            pass

        def _route(self, *_args, **_kwargs):
            return lambda function: function

        on_event = get = post = middleware = websocket = _route

        def mount(self, *_args, **_kwargs):
            return None

    class StubResponse:
        def __init__(self, content=None, *, headers=None, status_code=200, **_kwargs):
            self.body = json.dumps(content).encode("utf-8")
            self.headers = headers or {}
            self.status_code = status_code

    fastapi_stub.FastAPI = StubApp
    fastapi_stub.HTTPException = StubHTTPException
    fastapi_stub.Request = object
    fastapi_stub.WebSocket = object
    fastapi_stub.WebSocketDisconnect = type(
        "WebSocketDisconnect", (Exception,), {}
    )
    fastapi_responses_stub = ModuleType("fastapi.responses")
    fastapi_responses_stub.HTMLResponse = StubResponse
    fastapi_responses_stub.JSONResponse = StubResponse
    fastapi_responses_stub.StreamingResponse = StubResponse
    fastapi_staticfiles_stub = ModuleType("fastapi.staticfiles")
    fastapi_staticfiles_stub.StaticFiles = mock.MagicMock
    sys.modules["fastapi"] = fastapi_stub
    sys.modules["fastapi.responses"] = fastapi_responses_stub
    sys.modules["fastapi.staticfiles"] = fastapi_staticfiles_stub

if importlib.util.find_spec("uvicorn") is None:
    sys.modules["uvicorn"] = ModuleType("uvicorn")

if importlib.util.find_spec("taey_adapter") is None:
    sys.modules["taey_adapter"] = ModuleType("taey_adapter")

import council_prompt_receipt as producer
import httpx
import redis
import soma_proxy
import taey_council_seat as council_runtime
import taey_seat as executive

with mock.patch.object(redis, "Redis", mock.MagicMock), mock.patch.object(
    httpx,
    "AsyncClient",
    mock.MagicMock,
):
    from dashboard import app as dashboard_app


ROOT = Path(__file__).resolve().parent
EXPECTED = {
    "tool_profile": "council-read",
    "max_rounds": 1,
    "max_tool_calls": 2,
    "max_search_results": 3,
    "max_tool_result_bytes": 3_000,
    "max_tool_result_total_bytes": 6_000,
    "max_completion_tokens": 1_500,
}
OUTPUT_EXPECTED = {
    "status_chars": 24,
    "list_items": 1,
    "item_chars": 96,
    "evidence_items": 2,
    "evidence_chars": 175,
    "recommendation_chars": 192,
    "prompt_revision": 2_147_483_647,
    "structured_response_bytes": 1_350,
    "terminal_token_allowance": 1,
}
READ_TOOLS = {
    "check_body_state",
    "compute",
    "fetch_url",
    "list_dir",
    "read_file",
    "retrieve_document",
    "search_isma",
}
NUMERIC_COMPLETION_BUDGET = re.compile(
    r"\b\d[\d_,]*-token completion budget\b",
    re.IGNORECASE,
)


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise RuntimeError(detail)


def observed() -> dict[str, int | str]:
    return {
        "tool_profile": producer.COUNCIL_TOOL_PROFILE,
        "max_rounds": producer.COUNCIL_MAX_TOOL_ROUNDS,
        "max_tool_calls": producer.COUNCIL_MAX_TOOL_CALLS,
        "max_search_results": producer.COUNCIL_MAX_SEARCH_RESULTS,
        "max_tool_result_bytes": producer.COUNCIL_MAX_TOOL_RESULT_BYTES,
        "max_tool_result_total_bytes": (
            producer.COUNCIL_MAX_TOOL_RESULT_TOTAL_BYTES
        ),
        "max_completion_tokens": producer.COUNCIL_MAX_COMPLETION_TOKENS,
    }


def validate_launcher_contract(launcher: str, deployer: str) -> None:
    declaration = re.search(
        r"^readonly STRUCTURED_OUTPUTS_CONFIG='([^']+)'$",
        launcher,
        re.MULTILINE,
    )
    require(declaration is not None, "vLLM launcher has no fixed structured-output config")
    try:
        config = json.loads(declaration.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError("vLLM structured-output config is not JSON") from exc
    require(
        config == STRUCTURED_OUTPUTS_CONFIG,
        "vLLM launcher must select xgrammar with free whitespace disabled",
    )
    require(
        "--validate-structured-outputs-config" in launcher
        and "from vllm.config import StructuredOutputsConfig" in launcher
        and 'if config.backend != "xgrammar"' in launcher
        and "config.disable_any_whitespace is not True" in launcher
        and "raise SystemExit(" in launcher
        and '--structured-outputs-config "${STRUCTURED_OUTPUTS_CONFIG}"' in launcher,
        "vLLM launcher does not parse and enforce its effective structured-output semantics",
    )
    require(
        STRUCTURED_OUTPUTS_PREFLIGHT in deployer
        and deployer.index(STRUCTURED_OUTPUTS_PREFLIGHT)
        < deployer.index('sudo systemctl restart taey-ep3'),
        "Thor deploy does not parse the pinned-image config before restart",
    )


def validate_repo_local_structured_output_sources() -> None:
    sources_with_literal: set[str] = set()
    for source in REPO_ROOT.rglob("*.py"):
        relative = source.relative_to(REPO_ROOT)
        if (
            relative.parts[0] == "tests"
            or source.name.startswith("validate_")
            or ".git" in relative.parts
        ):
            continue
        if "response_format" in source.read_text(encoding="utf-8"):
            sources_with_literal.add(str(relative))
    require(
        sources_with_literal
        == {
            "serving/council_prompt_receipt.py",
            "serving/soma_proxy.py",
            "serving/taey_council_seat.py",
            "serving/taey_seat.py",
        },
        "taey-presence repo-local committed production Python sources containing the "
        "literal response_format drifted from the receipt, council, request-builder, "
        f"and proxy paths: {sorted(sources_with_literal)}",
    )


def validate_index_workflow_trigger(workflow: str | None = None) -> None:
    if workflow is None:
        workflow = (
            REPO_ROOT / ".github/workflows/knowledge-index.yml"
        ).read_text(encoding="utf-8")
    _, on_separator, after_on = workflow.partition("\non:\n")
    trigger_block, jobs_separator, _ = after_on.partition("\njobs:\n")
    require(
        on_separator and jobs_separator,
        "knowledge-index workflow has no bounded trigger block",
    )
    actual_trigger_block = "on:\n" + trigger_block + "\njobs:\n"
    require(
        actual_trigger_block == INDEX_WORKFLOW_TRIGGER_BLOCK,
        "knowledge-index trigger must contain only an unfiltered pull_request and "
        "an unfiltered main push",
    )


def validate_contract_values() -> None:
    require(observed() == EXPECTED, f"council budget drifted: {observed()}")
    output = {
        "status_chars": producer.CONTRIBUTION_STATUS_MAX_CHARS,
        "list_items": producer.CONTRIBUTION_LIST_MAX_ITEMS,
        "item_chars": producer.CONTRIBUTION_ITEM_MAX_CHARS,
        "evidence_items": producer.CONTRIBUTION_EVIDENCE_MAX_ITEMS,
        "evidence_chars": producer.CONTRIBUTION_EVIDENCE_MAX_CHARS,
        "recommendation_chars": producer.CONTRIBUTION_RECOMMENDATION_MAX_CHARS,
        "prompt_revision": producer.CONTRIBUTION_MAX_PROMPT_REVISION,
        "structured_response_bytes": (
            producer.COUNCIL_MAX_STRUCTURED_RESPONSE_BYTES
        ),
        "terminal_token_allowance": (
            producer.COUNCIL_COMPLETION_TERMINAL_TOKEN_ALLOWANCE
        ),
    }
    require(output == OUTPUT_EXPECTED, f"contribution budget drifted: {output}")
    tools = soma_proxy._tools_for_profile(producer.COUNCIL_TOOL_PROFILE)
    require(
        {tool["function"]["name"] for tool in tools} == READ_TOOLS,
        "council-read is not exactly the non-mutating evidence surface",
    )
    search = next(tool for tool in tools if tool["function"]["name"] == "search_isma")
    top_k = search["function"]["parameters"]["properties"]["top_k"]
    require(
        (top_k.get("minimum"), top_k.get("maximum"), top_k.get("default"))
        == (1, 3, 3),
        "council search schema is not bound to one through three results",
    )
    manifest = producer.load_manifest(ROOT / "council_seats.json")
    for seat in manifest.seats:
        model_prompt = producer.system_message(seat)["content"]
        require(
            "runtime-issued completion budget" in model_prompt,
            f"{seat.seat_id} lost the runtime-issued completion-budget instruction",
        )
        require(
            NUMERIC_COMPLETION_BUDGET.search(model_prompt) is None,
            f"{seat.seat_id} model prompt duplicates a numeric completion budget",
        )
    properties = producer.response_format(
        manifest.seats[0], 1, ["fleet_message:budget-gate"]
    )["json_schema"]["schema"]["properties"]
    require(
        properties["status"]["enum"] == list(producer.CONTRIBUTION_STATUS_VALUES),
        "status values drifted",
    )
    require(
        producer.CONTRIBUTION_ITEM_PATTERN
        == r"^[\x20-\x21\x23-\x5B\x5D-\x7E]{1,96}$"
        and producer.CONTRIBUTION_RECOMMENDATION_PATTERN
        == r"^[\x20-\x21\x23-\x5B\x5D-\x7E]{1,192}$",
        "response patterns lost their bounded safe-ASCII language",
    )
    require(
        properties["recommendation"]["maxLength"] == 192
        and properties["recommendation"]["pattern"]
        == producer.CONTRIBUTION_RECOMMENDATION_PATTERN,
        "recommendation bound drifted",
    )
    for field_name in producer.CONTRIBUTION_NARRATIVE_FIELDS:
        require(
            properties[field_name]["maxItems"] == 1
            and properties[field_name]["items"]["maxLength"] == 96
            and properties[field_name]["items"]["pattern"]
            == producer.CONTRIBUTION_ITEM_PATTERN,
            f"{field_name} schema bound drifted",
        )
    require(
        properties["evidence_refs"]["maxItems"] == 2
        and properties["evidence_refs"]["items"]["maxLength"] == 175,
        "evidence_refs schema bound drifted",
    )
    for unbounded_reference in (
        'unsafe"reference',
        "unsafe\\reference",
        "unsafe\nreference",
    ):
        try:
            producer.response_format(manifest.seats[0], 1, [unbounded_reference])
        except ValueError:
            continue
        raise RuntimeError("unsafe evidence reference entered the response grammar")
    longest_role_id = max((seat.role_id for seat in manifest.seats), key=len)
    maximal_contribution = {
        "schema_version": 1,
        "seat_id": "taey-council-7",
        "role_id": longest_role_id,
        "status": max(producer.CONTRIBUTION_STATUS_VALUES, key=len),
        "prompt_revision": producer.CONTRIBUTION_MAX_PROMPT_REVISION,
        "observations": ["W" * producer.CONTRIBUTION_ITEM_MAX_CHARS],
        "inferences": ["W" * producer.CONTRIBUTION_ITEM_MAX_CHARS],
        "unknowns": ["W" * producer.CONTRIBUTION_ITEM_MAX_CHARS],
        "evidence_refs": [
            "W" * producer.CONTRIBUTION_EVIDENCE_MAX_CHARS,
            "W" * producer.CONTRIBUTION_EVIDENCE_MAX_CHARS,
        ],
        "concerns": ["W" * producer.CONTRIBUTION_ITEM_MAX_CHARS],
        "questions": ["W" * producer.CONTRIBUTION_ITEM_MAX_CHARS],
        "recommendation": "W" * producer.CONTRIBUTION_RECOMMENDATION_MAX_CHARS,
        "confidence": 0.25,
    }
    maximal_structured_bytes = json.dumps(maximal_contribution).encode("utf-8")
    require(
        len(maximal_structured_bytes)
        <= producer.COUNCIL_MAX_STRUCTURED_RESPONSE_BYTES
        and producer.COUNCIL_MAX_STRUCTURED_RESPONSE_BYTES
        + producer.COUNCIL_COMPLETION_TERMINAL_TOKEN_ALLOWANCE
        < producer.COUNCIL_MAX_COMPLETION_TOKENS,
        "structured response envelope is not bounded: "
        f"{len(maximal_structured_bytes)} bytes",
    )
    launcher = (ROOT / "vllm_serve.sh").read_text(encoding="utf-8")
    deployer = (ROOT / "deploy_thor.sh").read_text(encoding="utf-8")
    validate_launcher_contract(launcher, deployer)
    validate_repo_local_structured_output_sources()
    validate_index_workflow_trigger()
    require(
        "ghcr.io/nvidia-ai-iot/vllm@sha256:"
        "b587dd56b4cb076209ad5156a626ac75f5a976d0e8e7d1e6a9fccd56d1bd65e8"
        in launcher,
        "deployed tokenizer and xgrammar image pin drifted",
    )


def request_material() -> dict:
    manifest = producer.load_manifest(ROOT / "council_seats.json")
    seat = manifest.seats[0]
    evidence_registry = ["fleet_message:budget-gate"]
    lineage = {
        "request_contract": producer.DCM_REQUEST_CONTRACT,
        "prompt_contract_sha256": producer.prompt_contract_receipt(
            manifest, seat
        )["prompt_contract_sha256"],
        "request_id": "sha256:" + ("1" * 64),
        "council_run_id": "dcm-budget-gate",
        "round_id": "dcm-budget-gate",
        "prompt_revision": 1,
        "model_identity_receipt_sha256": "sha256:" + ("2" * 64),
        "evidence_registry": evidence_registry,
    }
    messages = [
        producer.system_message(seat),
        {"role": "user", "content": "Bounded council production decision."},
    ]
    response_format = producer.response_format(seat, 1, evidence_registry)
    model_request = executive.ProxyClient.model_request_body(
        messages,
        response_format,
        max_rounds=producer.COUNCIL_MAX_TOOL_ROUNDS,
        max_tokens=producer.COUNCIL_MAX_COMPLETION_TOKENS,
    )
    outbound = producer.encode_outbound_request_bytes(model_request)
    claim = SimpleNamespace(
        source=SimpleNamespace(name="inbox"),
        message_id="budget-gate",
        raw='{"body":"bounded council production decision"}',
        payload={"attachments": []},
    )
    receipt = producer.model_request_receipt(
        manifest=manifest,
        seat=seat,
        lineage=lineage,
        model_request=model_request,
        outbound_request_bytes=outbound,
        claims=[claim],
    )
    contribution = {
        "schema_version": 1,
        "seat_id": seat.seat_id,
        "role_id": seat.role_id,
        "status": "contributed",
        "prompt_revision": 1,
        "observations": ["Observed: bounded path exercised."],
        "inferences": [],
        "unknowns": [],
        "evidence_refs": evidence_registry,
        "concerns": [],
        "questions": [],
        "recommendation": "Use the bounded path.",
        "confidence": 0.75,
    }
    return {
        "lineage": lineage,
        "model_request": model_request,
        "outbound": outbound,
        "receipt": receipt,
        "seat": seat,
        "contribution": contribution,
    }


def _tool_calls(count: int, *, top_k: int | None = None) -> list[dict]:
    names = ["search_isma", "read_file", "check_body_state"]
    calls = []
    for index in range(count):
        name = names[index]
        arguments = {"query": "one fact"} if name == "search_isma" else {}
        if name == "read_file":
            arguments = {"path": "/tmp/evidence"}
        if top_k is not None and name == "search_isma":
            arguments["top_k"] = top_k
        calls.append({
            "id": f"call-{index + 1}",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, separators=(",", ":")),
            },
        })
    return calls


def exercise_council_chat(
    *,
    call_count: int = 2,
    top_k: int | None = None,
    tool_result: str | None = None,
) -> dict:
    material = request_material()
    upstream_bodies: list[dict] = []
    client_headers: list[dict[str, str]] = []
    executed: list[tuple[str, dict]] = []
    original_result = tool_result or ("evidence-" + ("🌍" * 2_000))
    tool_payload = {
        "choices": [{
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": _tool_calls(call_count, top_k=top_k),
            },
        }],
        "usage": {"completion_tokens": 1},
    }
    final_payload = {
        "choices": [{
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": json.dumps(material["contribution"], separators=(",", ":")),
            },
        }],
        "usage": {"prompt_tokens": 40, "completion_tokens": 100},
    }

    async def upstream_post(*_args, **kwargs):
        body = json.loads(bytes(kwargs["content"]).decode("utf-8"))
        upstream_bodies.append(body)
        payload = tool_payload if len(upstream_bodies) == 1 else final_payload
        return SimpleNamespace(status_code=200, json=lambda: payload)

    async def execute(name: str, arguments: dict, **_kwargs) -> str:
        executed.append((name, arguments))
        return original_result

    upstream = SimpleNamespace(post=mock.AsyncMock(side_effect=upstream_post))
    invoked_result: dict[str, object] = {}

    def urlopen(request):
        headers = {key.lower(): value for key, value in request.header_items()}
        client_headers.append(headers)
        body = json.loads(request.data)
        turn = soma_proxy._turn_context(SimpleNamespace(headers=headers), body)
        payload = {
            **soma_proxy._turn_payload(turn),
            "_tool_profile_state": {"terminal": None},
        }
        token = soma_proxy._request_context.set(payload)
        try:
            with mock.patch.object(
                soma_proxy, "inject_preamble", side_effect=lambda value: value
            ), mock.patch.object(
                soma_proxy, "_http", upstream
            ), mock.patch.object(
                soma_proxy, "execute_tool_call_async", side_effect=execute
            ):
                response = asyncio.run(
                    soma_proxy._chat_completions_for_turn(
                        body,
                        turn,
                        liveness_registered=False,
                    )
                )
        finally:
            soma_proxy._request_context.reset(token)
        wrapped = mock.MagicMock()
        wrapped.__enter__.return_value = wrapped
        wrapped.read.return_value = bytes(response.body)
        wrapped.headers = soma_proxy._turn_headers(turn)
        return wrapped

    claim = executive.ClaimedMessage(
        source=SimpleNamespace(name="inbox"),
        raw='{"body":"bounded council production decision"}',
        payload={
            "dcm_session_id": "dcm-budget-gate",
            "wave_id": "wave-budget-gate",
            "request_id": material["lineage"]["request_id"],
        },
        message_id="budget-gate",
    )
    contribution_id = "contrib-budget-gate"
    transport_receipt = {
        "terminal_outcome": "contributed",
        "contrib_id": contribution_id,
    }

    def execute_wave_request(
        _request,
        _observation,
        *,
        invoke,
        validate_response,
        acknowledge,
        **_kwargs,
    ):
        result = invoke({})
        invoked_result["proxy"] = result
        validated = validate_response(result, {})
        require(
            validated == material["contribution"],
            "DCM runtime did not validate the bounded contribution",
        )
        acknowledge(transport_receipt)
        return {
            "graph": {"contrib_id": contribution_id},
            "transport_receipt": transport_receipt,
        }

    proxy = executive.ProxyClient()
    store = SimpleNamespace(
        append=mock.MagicMock(),
        remember_outcome=mock.MagicMock(),
    )
    inbox = SimpleNamespace(acknowledge_dcm=mock.MagicMock())
    liveness = SimpleNamespace(assert_healthy=mock.MagicMock())
    wave = {
        "slots": [{
            "request_id": material["lineage"]["request_id"],
            "state": "pending",
        }],
    }
    wave_error = type("WaveRequestExecutionError", (Exception,), {})
    with mock.patch.object(
        executive.urllib.request,
        "urlopen",
        side_effect=urlopen,
    ), mock.patch.object(
        proxy,
        "ask",
        wraps=proxy.ask,
    ) as ask, mock.patch.object(
        council_runtime.dcm_adapter,
        "mesh",
        SimpleNamespace(read_wave=lambda *_args: wave),
        create=True,
    ), mock.patch.object(
        council_runtime.dcm_adapter,
        "execute_wave_request",
        side_effect=execute_wave_request,
        create=True,
    ), mock.patch.object(
        council_runtime.dcm_adapter,
        "WaveRequestExecutionError",
        wave_error,
        create=True,
    ), mock.patch.object(
        council_runtime,
        "_dcm_claim_observation",
        return_value={},
    ), mock.patch.object(
        council_runtime,
        "_graph_contribution",
        return_value=material["contribution"],
    ), mock.patch.object(
        council_runtime.executive,
        "SESSION",
        material["seat"].seat_id,
    ), mock.patch.object(
        council_runtime,
        "ROLE_ID",
        material["seat"].role_id,
    ):
        dcm_reply = council_runtime._run_dcm_turn(
            claim=claim,
            inbox=inbox,
            store=store,
            proxy=proxy,
            liveness=liveness,
            lineage=material["lineage"],
            prompt="bounded council production decision",
            messages=material["model_request"]["messages"],
            contribution_format=material["model_request"]["response_format"],
            attempt_fields={},
            event_id="event-budget-gate",
            correlation_id="dcm-budget-gate",
            previously_attempted=False,
            outbound_request_bytes=material["outbound"],
            producer_receipt=material["receipt"],
        )
    forwarded = ask.call_args.kwargs
    require(
        forwarded.get("tool_profile_receipt") is material["receipt"]
        and "tool_profile" not in forwarded,
        "DCM seat did not forward the producer receipt as the sole profile source",
    )
    require(
        json.loads(dcm_reply) == material["contribution"],
        "DCM seat result differs from the validated contribution",
    )
    return {
        **material,
        "client_headers": client_headers,
        "executed": executed,
        "original_result": original_result,
        "result": invoked_result["proxy"],
        "upstream_bodies": upstream_bodies,
    }


def validate_council_chat_path() -> None:
    observed_path = exercise_council_chat()
    require(len(observed_path["client_headers"]) == 1, "client did not send once")
    require(
        observed_path["client_headers"][0].get("x-taey-tool-profile")
        == EXPECTED["tool_profile"],
        "receipt-bound council profile did not reach the actual header",
    )
    require(len(observed_path["upstream_bodies"]) == 2, "round limit was not one")
    first, final = observed_path["upstream_bodies"]
    require(
        first.get("max_tokens") == 1_500,
        "seat completion cap did not reach vLLM",
    )
    require(
        {tool["function"]["name"] for tool in first.get("tools", [])}
        == READ_TOOLS,
        "council chat path did not expose the exact read-only profile",
    )
    require("tools" not in final and final.get("tool_choice") == "none", "round did not terminate")
    require(
        len(observed_path["executed"]) == 2
        and observed_path["executed"][0][1].get("top_k") == 3,
        "tool-call or top_k bound did not execute",
    )
    results = [
        message["content"]
        for message in final["messages"]
        if message.get("role") == "tool"
    ]
    result_bytes = [len(value.encode("utf-8")) for value in results]
    require(
        len(results) == 2
        and all(size <= EXPECTED["max_tool_result_bytes"] for size in result_bytes)
        and sum(result_bytes) <= EXPECTED["max_tool_result_total_bytes"],
        "UTF-8 tool evidence escaped its per-result or cumulative budget",
    )
    original = observed_path["original_result"]
    marker = (
        f"original_chars={len(original)} "
        f"original_bytes={len(original.encode('utf-8'))} "
        f"sha256:{hashlib.sha256(original.encode('utf-8')).hexdigest()}]"
    )
    require(all(marker in value for value in results), "excerpt byte receipt is absent")
    with mock.patch.object(
        council_runtime.executive,
        "SESSION",
        observed_path["seat"].seat_id,
    ), mock.patch.object(
        council_runtime,
        "ROLE_ID",
        observed_path["seat"].role_id,
    ):
        contribution = council_runtime._validated_contribution(
            observed_path["result"].reply,
            observed_path["lineage"],
        )
    require(contribution == observed_path["contribution"], "seat output validation drifted")
    completion_receipt = observed_path["result"].completion_receipt
    require(
        completion_receipt["finish_reason"] == "stop"
        and completion_receipt["usage"]["completion_tokens"] == 100
        and completion_receipt["requested_max_tokens"] == 1_500
        and completion_receipt["cap_exhausted"] is False,
        "successful completion receipt drifted",
    )


def validate_proxy_terminal_parser() -> None:
    payload = {
        "choices": [{
            "finish_reason": "length",
            "message": {"role": "assistant", "content": '{"partial":true'},
        }],
        "usage": {
            "prompt_tokens": 7_590,
            "completion_tokens": 1_500,
            "total_tokens": 9_090,
        },
    }
    response = mock.MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.headers = {
        "X-Taey-Turn-Id": "turn-cap",
        "X-Taey-Event-Id": "event-cap",
        "X-Taey-Correlation-Id": "round-cap",
    }
    response.__enter__.return_value = response
    with mock.patch.object(
        executive.urllib.request,
        "urlopen",
        return_value=response,
    ):
        try:
            executive.ProxyClient().ask(
                "bounded",
                event_id="event-cap",
                correlation_id="round-cap",
                messages=[{"role": "user", "content": "bounded"}],
                max_tokens=producer.COUNCIL_MAX_COMPLETION_TOKENS,
            )
        except executive.CompletionContractError as exc:
            require(
                exc.code == "proxy_completion_budget_exhausted"
                and exc.completion_receipt == {
                    "contract": "taey-model-completion-receipt/v1",
                    "finish_reason": "length",
                    "usage": {
                        "prompt_tokens": 7_590,
                        "completion_tokens": 1_500,
                        "total_tokens": 9_090,
                    },
                    "requested_max_tokens": 1_500,
                    "cap_exhausted": True,
                },
                "cap exhaustion was not preserved as a content-free receipt",
            )
            diagnostics = council_runtime._completion_diagnostics(error=exc)
            require(
                diagnostics["validation_error_class"]
                == "CompletionContractError"
                and diagnostics["validation_error_code"]
                == "proxy_completion_budget_exhausted"
                and diagnostics["model_completion_receipt"]
                == exc.completion_receipt,
                "council did not retain terminal parser diagnostics",
            )
            return
    raise RuntimeError("ProxyClient accepted a completion that exhausted its cap")


def validate_profile_receipt_drift_rejected() -> None:
    material = request_material()
    receipt = copy.deepcopy(material["receipt"])
    receipt["tool_profile"] = "full"
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    receipt["receipt_sha256"] = producer.canonical_sha256(unsigned)
    urlopen = mock.MagicMock(side_effect=AssertionError("invalid receipt reached transport"))
    with mock.patch.object(executive.urllib.request, "urlopen", urlopen):
        try:
            executive.ProxyClient().ask(
                "drift",
                event_id="event-drift",
                correlation_id="round-drift",
                messages=material["model_request"]["messages"],
                response_format=material["model_request"]["response_format"],
                max_rounds=material["model_request"]["max_rounds"],
                max_tokens=material["model_request"]["max_tokens"],
                tool_profile_receipt=receipt,
                outbound_request_bytes=material["outbound"],
            )
        except executive.SeatFailure:
            pass
        else:
            raise RuntimeError("receipt/header profile drift was accepted")
    require(urlopen.call_count == 0, "invalid profile receipt reached transport")


def validate_call_limit_rejection() -> None:
    try:
        exercise_council_chat(call_count=3, tool_result="small evidence")
    except executive.SeatFailure:
        return
    raise RuntimeError("three council tool calls were accepted")


def validate_top_k_rejection() -> None:
    try:
        exercise_council_chat(call_count=1, top_k=4)
    except executive.SeatFailure:
        return
    raise RuntimeError("council top_k=4 was accepted")


def validate_seat_output_rejection() -> None:
    material = request_material()
    invalid = copy.deepcopy(material["contribution"])
    invalid["observations"] = ["one", "two", "three", "four"]
    with mock.patch.object(
        council_runtime.executive,
        "SESSION",
        material["seat"].seat_id,
    ), mock.patch.object(
        council_runtime,
        "ROLE_ID",
        material["seat"].role_id,
    ):
        try:
            council_runtime._validated_contribution(
                json.dumps(invalid),
                material["lineage"],
            )
        except council_runtime.ContributionContractError as exc:
            require(
                exc.code == "observations_bounds"
                and council_runtime._completion_diagnostics(error=exc)
                == {
                    "validation_error_class": "ContributionContractError",
                    "validation_error_code": "observations_bounds",
                },
                "oversized contribution did not retain a bounded error code",
            )
            try:
                council_runtime._validated_contribution(
                    '{"unfinished":',
                    material["lineage"],
                )
            except council_runtime.ContributionContractError as invalid_json:
                require(
                    invalid_json.code == "invalid_json",
                    "invalid JSON did not retain its bounded error code",
                )
                return
    raise RuntimeError("oversized council contribution was accepted")


def validate_full_profile_unchanged() -> None:
    client_headers: list[dict[str, str]] = []
    upstream_bodies: list[dict] = []
    final_payload = {
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "generic answer"},
        }],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }

    async def upstream_post(*_args, **kwargs):
        upstream_bodies.append(json.loads(bytes(kwargs["content"]).decode("utf-8")))
        return SimpleNamespace(status_code=200, json=lambda: final_payload)

    upstream = SimpleNamespace(post=mock.AsyncMock(side_effect=upstream_post))

    def urlopen(request):
        headers = {key.lower(): value for key, value in request.header_items()}
        client_headers.append(headers)
        body = json.loads(request.data)
        turn = soma_proxy._turn_context(SimpleNamespace(headers=headers), body)
        token = soma_proxy._request_context.set({
            **soma_proxy._turn_payload(turn),
            "_tool_profile_state": {"terminal": None},
        })
        try:
            with mock.patch.object(
                soma_proxy, "inject_preamble", side_effect=lambda value: value
            ), mock.patch.object(soma_proxy, "_http", upstream):
                response = asyncio.run(
                    soma_proxy._chat_completions_for_turn(body, turn, False)
                )
        finally:
            soma_proxy._request_context.reset(token)
        wrapped = mock.MagicMock()
        wrapped.__enter__.return_value = wrapped
        wrapped.read.return_value = bytes(response.body)
        wrapped.headers = soma_proxy._turn_headers(turn)
        return wrapped

    with mock.patch.object(executive.urllib.request, "urlopen", side_effect=urlopen):
        result = executive.ProxyClient().ask(
            "generic",
            event_id="event-generic",
            correlation_id="round-generic",
            messages=[{"role": "user", "content": "generic"}],
        )
    require(result.reply == "generic answer", "generic full-profile answer drifted")
    require(
        "x-taey-tool-profile" not in client_headers[0],
        "generic client gained a specialized profile header",
    )
    require(
        {tool["function"]["name"] for tool in upstream_bodies[0]["tools"]}
        == {tool["function"]["name"] for tool in soma_proxy.TOOLS},
        "generic request no longer receives the full profile",
    )
    require(
        "max_rounds" not in upstream_bodies[0]
        and "max_tokens" not in upstream_bodies[0],
        "generic request inherited council budgets",
    )


async def validate_main_synthesis() -> None:
    packet = {
        "conversation_id": "main-budget-gate",
        "round_id": "dcm-main-budget-gate",
        "prompt_revision": 1,
    }

    def response(finish_reason: str):
        return SimpleNamespace(
            headers={
                "X-Taey-Event-Id": "dcm-main-budget-gate:1:synthesis",
                "X-Taey-Correlation-Id": "dcm-main-budget-gate",
                "X-Taey-Turn-Id": "turn-main-budget-gate",
            },
            raise_for_status=lambda: None,
            json=lambda: {
                "model": "ep3",
                "choices": [{
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": "Main decision."},
                }],
            },
        )

    post = mock.AsyncMock(return_value=response("stop"))
    with mock.patch.object(dashboard_app, "_http", SimpleNamespace(post=post)):
        result = await dashboard_app._synthesize_native_council(
            "main-budget-gate", packet
        )
    require(result["answer"] == "Main decision.", "Main synthesis answer drifted")
    require(
        post.await_args.kwargs["json"].get("max_tokens") == 768
        and post.await_args.kwargs["json"].get("tools") == [],
        "Main synthesis cap or no-tool contract did not reach the request",
    )
    post = mock.AsyncMock(return_value=response("length"))
    with mock.patch.object(dashboard_app, "_http", SimpleNamespace(post=post)):
        try:
            await dashboard_app._synthesize_native_council(
                "main-budget-gate", packet
            )
        except RuntimeError:
            return
    raise RuntimeError("Main accepted a non-terminal synthesis")


def _expect_red(label: str, operation, caught: list[str]) -> None:
    try:
        operation()
    except Exception:
        caught.append(label)
        return
    raise RuntimeError(f"production mutation stayed green: {label}")


def prove_mutation_red() -> list[str]:
    caught: list[str] = []
    profile = producer.COUNCIL_TOOL_PROFILE
    allowed = soma_proxy._TOOL_PROFILE_ALLOWED[profile]
    require(isinstance(allowed, frozenset), "council tool profile is not finite")
    with mock.patch.dict(
        soma_proxy._TOOL_PROFILE_ALLOWED,
        {profile: allowed | {"drive_chat"}},
    ):
        _expect_red("profile-tool-surface", validate_council_chat_path, caught)
    with mock.patch.object(
        producer,
        "verified_model_request_tool_profile",
        return_value="full",
    ):
        _expect_red("receipt-profile-selection", validate_council_chat_path, caught)
    original_turn_headers = soma_proxy._turn_headers

    def wrong_echo(turn):
        headers = original_turn_headers(turn)
        headers["X-Taey-Tool-Profile"] = "full"
        return headers

    with mock.patch.object(soma_proxy, "_turn_headers", side_effect=wrong_echo):
        _expect_red("profile-echo", validate_council_chat_path, caught)
    with mock.patch.object(producer, "COUNCIL_MAX_TOOL_ROUNDS", 2):
        _expect_red("round-limit", validate_council_chat_path, caught)
    with mock.patch.object(producer, "COUNCIL_MAX_TOOL_CALLS", 3):
        _expect_red("total-call-limit", validate_call_limit_rejection, caught)
    with mock.patch.object(producer, "COUNCIL_MAX_SEARCH_RESULTS", 4):
        _expect_red("top-k-limit", validate_top_k_rejection, caught)
    with mock.patch.object(producer, "COUNCIL_MAX_TOOL_RESULT_BYTES", 4_000):
        _expect_red("per-result-byte-limit", validate_council_chat_path, caught)
    with mock.patch.object(
        producer,
        "COUNCIL_MAX_TOOL_RESULT_TOTAL_BYTES",
        5_000,
    ):
        _expect_red("cumulative-byte-limit", validate_council_chat_path, caught)
    with mock.patch.object(producer, "COUNCIL_MAX_COMPLETION_TOKENS", 1_501):
        _expect_red("seat-completion-limit", validate_council_chat_path, caught)
    with mock.patch.object(
        producer,
        "CONTRIBUTION_ITEM_PATTERN",
        r"^[\x20-\x21\x23-\x5B\x5D-\x7E]+$",
    ):
        _expect_red("response-pattern-bound", validate_contract_values, caught)
    launcher = (ROOT / "vllm_serve.sh").read_text(encoding="utf-8")
    deployer = (ROOT / "deploy_thor.sh").read_text(encoding="utf-8")
    _expect_red(
        "structured-config-missing-backend",
        lambda: validate_launcher_contract(
            launcher.replace(
                STRUCTURED_OUTPUTS_DECLARATION,
                "readonly STRUCTURED_OUTPUTS_CONFIG="
                "'{\"disable_any_whitespace\":true}'",
            ),
            deployer,
        ),
        caught,
    )
    _expect_red(
        "structured-config-whitespace-enabled",
        lambda: validate_launcher_contract(
            launcher.replace(
                STRUCTURED_OUTPUTS_DECLARATION,
                "readonly STRUCTURED_OUTPUTS_CONFIG="
                "'{\"backend\":\"xgrammar\",\"disable_any_whitespace\":false}'",
            ),
            deployer,
        ),
        caught,
    )
    _expect_red(
        "structured-config-missing-pre-restart-image-gate",
        lambda: validate_launcher_contract(
            launcher,
            deployer.replace(STRUCTURED_OUTPUTS_PREFLIGHT, "missing-runtime-gate"),
        ),
        caught,
    )
    workflow = (
        REPO_ROOT / ".github/workflows/knowledge-index.yml"
    ).read_text(encoding="utf-8")
    _expect_red(
        "index-workflow-pr-paths-filter",
        lambda: validate_index_workflow_trigger(
            workflow.replace(
                "  pull_request:\n",
                "  pull_request:\n    paths:\n      - 'serving/**'\n",
                1,
            )
        ),
        caught,
    )
    _expect_red(
        "index-workflow-pr-paths-ignore-filter",
        lambda: validate_index_workflow_trigger(
            workflow.replace(
                "  pull_request:\n",
                "  pull_request:\n    paths-ignore :\n      - 'dashboard/**'\n",
                1,
            )
        ),
        caught,
    )
    _expect_red(
        "index-workflow-unknown-extra-key",
        lambda: validate_index_workflow_trigger(
            workflow.replace(
                "  pull_request:\n",
                "  pull_request:\n    future-filter: anything\n",
                1,
            )
        ),
        caught,
    )
    _expect_red(
        "index-workflow-main-push-paths-filter",
        lambda: validate_index_workflow_trigger(
            workflow.replace(
                "    branches: [main]\n",
                "    branches: [main]\n    paths:\n      - 'serving/**'\n",
                1,
            )
        ),
        caught,
    )
    _expect_red(
        "index-workflow-main-push-paths-ignore-filter",
        lambda: validate_index_workflow_trigger(
            workflow.replace(
                "    branches: [main]\n",
                "    branches: [main]\n    paths-ignore:\n      - 'dashboard/**'\n",
                1,
            )
        ),
        caught,
    )
    original_system_message = producer.system_message

    def numeric_budget_prompt(seat):
        message = original_system_message(seat)
        message["content"] = message["content"].replace(
            "runtime-issued completion budget",
            "512-token completion budget",
        )
        return message

    with mock.patch.object(
        producer,
        "system_message",
        side_effect=numeric_budget_prompt,
    ):
        _expect_red(
            "prompt-numeric-completion-budget",
            validate_contract_values,
            caught,
        )
    with mock.patch.object(
        council_runtime,
        "_validated_contribution",
        side_effect=lambda reply, _lineage: json.loads(reply),
    ):
        _expect_red("seat-output-validation", validate_seat_output_rejection, caught)
    with mock.patch.object(dashboard_app, "COUNCIL_SYNTHESIS_MAX_TOKENS", 4_096):
        _expect_red(
            "main-completion-limit",
            lambda: asyncio.run(validate_main_synthesis()),
            caught,
        )
    with mock.patch.object(
        executive,
        "_terminal_reply",
        side_effect=lambda payload, **_kwargs: payload["choices"][0]["message"][
            "content"
        ],
    ):
        _expect_red(
            "proxy-terminal-parser",
            validate_proxy_terminal_parser,
            caught,
        )
    with mock.patch.object(
        dashboard_app,
        "_terminal_council_synthesis",
        side_effect=lambda data: data["choices"][0]["message"]["content"],
    ):
        _expect_red(
            "main-terminal-stop",
            lambda: asyncio.run(validate_main_synthesis()),
            caught,
        )
    return caught


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prove-mutation-red", action="store_true")
    args = parser.parse_args()
    validate_contract_values()
    validate_council_chat_path()
    validate_profile_receipt_drift_rejected()
    validate_call_limit_rejection()
    validate_top_k_rejection()
    validate_seat_output_rejection()
    validate_proxy_terminal_parser()
    validate_full_profile_unchanged()
    asyncio.run(validate_main_synthesis())
    mutations = prove_mutation_red() if args.prove_mutation_red else []
    print(json.dumps({
        "budget": observed(),
        "mutation_red": {"count": len(mutations), "cases": mutations},
        "network_calls": 0,
        "status": "PASS",
    }, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
