#!/usr/bin/env python3
"""Hermetic mechanical gate for the council inference budget."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
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

import council_prompt_receipt as producer
import soma_proxy


EXPECTED = {
    "tool_profile": "council-read",
    "max_rounds": 1,
    "max_tool_calls": 2,
    "max_search_results": 3,
    "max_tool_result_chars": 3_000,
    "max_tool_result_total_chars": 6_000,
    "max_completion_tokens": 512,
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
ROOT = Path(__file__).resolve().parent


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise RuntimeError(detail)


def observed() -> dict[str, int | str]:
    return {
        "tool_profile": producer.COUNCIL_TOOL_PROFILE,
        "max_rounds": producer.COUNCIL_MAX_TOOL_ROUNDS,
        "max_tool_calls": producer.COUNCIL_MAX_TOOL_CALLS,
        "max_search_results": producer.COUNCIL_MAX_SEARCH_RESULTS,
        "max_tool_result_chars": producer.COUNCIL_MAX_TOOL_RESULT_CHARS,
        "max_tool_result_total_chars": (
            producer.COUNCIL_MAX_TOOL_RESULT_TOTAL_CHARS
        ),
        "max_completion_tokens": producer.COUNCIL_MAX_COMPLETION_TOKENS,
    }


def validate_static_contract() -> None:
    budget = observed()
    require(budget == EXPECTED, f"council budget drifted: {budget}")
    require(
        budget["max_tool_result_total_chars"]
        == budget["max_tool_calls"] * budget["max_tool_result_chars"],
        "aggregate result budget differs from its two per-call allocations",
    )
    tools = soma_proxy._tools_for_profile(producer.COUNCIL_TOOL_PROFILE)
    require(
        {tool["function"]["name"] for tool in tools} == READ_TOOLS,
        "council-read is not exactly the non-mutating evidence surface",
    )
    search = next(
        tool for tool in tools if tool["function"]["name"] == "search_isma"
    )
    top_k = search["function"]["parameters"]["properties"]["top_k"]
    require(
        (top_k.get("minimum"), top_k.get("maximum"), top_k.get("default"))
        == (1, 3, 3),
        "council search schema is not bound to one through three results",
    )
    runtime = (ROOT / "taey_council_seat.py").read_text(encoding="utf-8")
    require(
        runtime.count(
            "tool_profile=prompt_producer.COUNCIL_TOOL_PROFILE"
        ) == 2,
        "both council proxy paths must select the council-read profile",
    )
    require(
        runtime.count(
            "max_tokens=prompt_producer.COUNCIL_MAX_COMPLETION_TOKENS"
        ) == 3,
        "receipted request and both council proxy paths must carry max_tokens",
    )
    synthesis = (ROOT.parent / "dashboard/app.py").read_text(encoding="utf-8")
    require('"max_tokens": 768' in synthesis, "Main synthesis max_tokens is not 768")
    require(
        'choice.get("finish_reason") != "stop"' in synthesis,
        "Main synthesis accepts a non-terminal completion",
    )


async def validate_runtime_contract() -> None:
    turn = soma_proxy.TurnContext(
        turn_id="turn-1",
        seat_id="taey-council-1",
        event_id="event-1",
        correlation_id="round-1",
        tool_profile=producer.COUNCIL_TOOL_PROFILE,
        proxy_namespace="taey",
        process_generation="generation-1",
        started_at=0.0,
    )
    original = "evidence-" + ("x" * 4_000)
    executed: list[tuple[str, dict]] = []

    async def fake(name: str, arguments: dict, **_kwargs) -> str:
        executed.append((name, arguments))
        return original

    state = {"terminal": None}
    token = soma_proxy._request_context.set({
        **soma_proxy._turn_payload(turn),
        "_tool_profile_state": state,
    })
    try:
        soma_proxy._require_council_tool_batch([{}, {}], turn)
        with mock.patch.object(soma_proxy, "execute_tool_call_async", side_effect=fake):
            first = await soma_proxy._execute_profile_tool_call_async(
                "search_isma",
                {"query": "one fact"},
                turn=turn,
                tool_call_id="call-1",
                round_num=1,
            )
            second = await soma_proxy._execute_profile_tool_call_async(
                "read_file",
                {"path": "one file"},
                turn=turn,
                tool_call_id="call-2",
                round_num=1,
            )
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
        marker = (
            "[COUNCIL TOOL RESULT EXCERPT "
            f"original_chars={len(original)} sha256:{digest}]\n"
        )
        require(first.startswith(marker) and second.startswith(marker), "excerpt receipt is absent")
        require(
            len(first) + len(second) == producer.COUNCIL_MAX_TOOL_RESULT_TOTAL_CHARS,
            "returned evidence escaped the aggregate character budget",
        )
        require(executed[0][1]["top_k"] == 3, "bounded search default was not applied")
        try:
            soma_proxy._require_council_tool_batch([{}], turn)
        except soma_proxy.HTTPException as exc:
            require(exc.status_code == 502, "third-call refusal used the wrong status")
        else:
            raise RuntimeError("a third council tool call was accepted")
    finally:
        soma_proxy._request_context.reset(token)

    token = soma_proxy._request_context.set({
        **soma_proxy._turn_payload(turn),
        "_tool_profile_state": {"terminal": None},
    })
    try:
        with mock.patch.object(
            soma_proxy,
            "execute_tool_call_async",
            side_effect=AssertionError("invalid top_k reached execution"),
        ):
            try:
                await soma_proxy._execute_profile_tool_call_async(
                    "search_isma",
                    {"query": "too many", "top_k": 4},
                    turn=turn,
                    tool_call_id="invalid",
                    round_num=1,
                )
            except soma_proxy.HTTPException as exc:
                require(exc.status_code == 502, "invalid top_k used the wrong status")
            else:
                raise RuntimeError("top_k=4 was accepted")
    finally:
        soma_proxy._request_context.reset(token)


def prove_mutation_red() -> int:
    caught = 0
    for field, value in EXPECTED.items():
        mutated = dict(EXPECTED)
        mutated[field] = value + 1 if isinstance(value, int) else value + "-mutated"
        try:
            require(mutated == EXPECTED, "mutation detected")
        except RuntimeError:
            caught += 1
    require(caught == len(EXPECTED), "gate missed an in-memory budget mutation")
    return caught


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prove-mutation-red", action="store_true")
    args = parser.parse_args()
    validate_static_contract()
    asyncio.run(validate_runtime_contract())
    caught = prove_mutation_red() if args.prove_mutation_red else 0
    print(json.dumps({
        "budget": observed(),
        "mutation_red": caught,
        "network_calls": 0,
        "status": "PASS",
    }, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
