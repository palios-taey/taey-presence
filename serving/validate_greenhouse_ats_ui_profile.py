#!/usr/bin/env python3
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import uuid
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVING_ROOT = REPO_ROOT / "serving"
SOMA_PROXY = SERVING_ROOT / "soma_proxy.py"
SOURCE = SOMA_PROXY.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE, filename=str(SOMA_PROXY))
REQUIRED_HANDS_COMMIT = "043a45e3414c02bb7805d2ddf12eb6ce02ee7889"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def assignment(name: str) -> ast.expr:
    matches: list[ast.expr] = []
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            matches.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            matches.append(node.value)
    require(len(matches) == 1, f"{name} is not one exact assignment")
    return matches[0]


def source_function(name: str) -> str:
    matches = [
        node
        for node in TREE.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    require(len(matches) == 1, f"{name} is not one exact function")
    return ast.get_source_segment(SOURCE, matches[0]) or ""


def load_function(name: str, namespace: dict[str, object]) -> object:
    node = next(
        item
        for item in TREE.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SOMA_PROXY), "exec"), namespace)
    return namespace[name]


class RequestContext:
    value: dict = {}

    @classmethod
    def get(cls) -> dict:
        return cls.value


class ValidationHTTPException(Exception):
    def __init__(self, status_code: int, detail: object):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def request_context(round_num: int) -> dict:
    return {
        "tool_profile": "greenhouse-ats-ui",
        "seat_id": "seat-1",
        "event_id": "event-1",
        "correlation_id": "correlation-1",
        "turn_id": "turn-1",
        "process_generation": "a" * 32,
        "tool_round": round_num,
        "_greenhouse_ats_ui_sequence": {"pending": None, "terminal": None},
        "_tool_profile_state": {"terminal": None},
    }


def write_private_transaction(root: Path, action_kind: str = "observe_form") -> dict:
    transaction_id = "00000000-0000-4000-8000-000000000000"
    action_id = "00000000-0000-4000-8000-000000000001"
    action = {
        "schema": "ats_greenhouse_frozen_action_v1",
        "provider": "greenhouse",
        "transaction_id": transaction_id,
        "action_id": action_id,
        "application_identity_sha256": "1" * 64,
        "expected_prior_event_hash": None,
        "action": {"kind": action_kind},
    }
    action_dir = root / "actions" / "seat-1"
    transaction_dir = root / "transactions" / "seat-1"
    action_dir.mkdir(parents=True, mode=0o700)
    transaction_dir.mkdir(parents=True, mode=0o700)
    for directory in (root, root / "actions", action_dir, root / "transactions", transaction_dir):
        os.chmod(directory, 0o700)
    action_path = action_dir / "action.json"
    action_raw = canonical_bytes(action)
    action_path.write_bytes(action_raw)
    os.chmod(action_path, 0o400)
    manifest = {
        "schema": "taey_greenhouse_ats_private_manifest_v1",
        "seat_id": "seat-1",
        "event_id": "event-1",
        "correlation_id": "correlation-1",
        "platform": "greenhouse",
        "display": ":17",
        "hands_commit": REQUIRED_HANDS_COMMIT,
        "frozen_action_path": str(action_path),
        "frozen_action_sha256": hashlib.sha256(action_raw).hexdigest(),
    }
    manifest_path = transaction_dir / "correlation-1.json"
    manifest_path.write_bytes(canonical_bytes(manifest))
    os.chmod(manifest_path, 0o400)
    return action


def success_result(action: dict) -> SimpleNamespace:
    action_kind = action["action"]["kind"]
    submit = action_kind == "submit"
    revision = "3" * 64
    samples = [
        {
            "sample": sample,
            "elapsed_ms": sample * 100,
            "revision": revision,
            "postcondition_matched": True,
            "refresh_policy": "invalidate_reacquire",
        }
        for sample in (1, 2)
    ]
    surface = {
        "schema": "ats_greenhouse_action_surface_v1",
        "surface": "form",
        "provider": "greenhouse",
        "application_identity_sha256": action["application_identity_sha256"],
        "route_grammar": "hosted_confirmation" if submit else "hosted_job",
        "controls": [
            {
                "ref": "r_" + ("2" * 32),
                "name": "First name",
                "role": "entry",
                "states": ["showing", "visible", "enabled"],
                "operations": ["focus", "fill"],
                "value_length": 17,
                "value_sha256": "9" * 64,
                "semantic_values": ["private-applicant-value"],
            }
        ],
        "complete_form_sha256": "5" * 64,
        "required_controls_complete": False,
        "revision": revision,
    }
    surface_capsule = {
        "schema": "ats_greenhouse_next_action_surface_v1",
        "provider": "greenhouse",
        "application_identity_sha256": action["application_identity_sha256"],
        "surface": "form",
        "revision": revision,
        "source_surface_sha256": hashlib.sha256(canonical_bytes(surface)).hexdigest(),
        "controls": [
            {
                "ref": "r_" + ("2" * 32),
                "name": "First name",
                "role": "entry",
                "operations": ["focus", "fill"],
                "is_empty": False,
                "has_semantic_value": True,
            }
        ],
        "route_grammar": "hosted_job",
        "complete_form_sha256": "5" * 64,
        "required_controls_complete": False,
    }
    payload = {
        "schema": "ats_greenhouse_one_action_result_v1",
        "ok": True,
        "provider": "greenhouse",
        "display": ":17",
        "transaction_id": action["transaction_id"],
        "action_id": action["action_id"],
        "application_identity_sha256": action["application_identity_sha256"],
        "action": {"kind": action_kind},
        "environment": {},
        "state": "employer_confirmation_proven" if submit else "action_ready",
        "surface": surface,
        "mutation_count": 0 if action_kind == "observe_form" else 1,
        "next_mutation_authorized": not submit,
        "receipt_event_hash": "4" * 64,
    }
    if action_kind == "observe_form":
        payload["samples"] = samples
        payload["surface_capsule"] = surface_capsule
    else:
        payload["source_samples"] = samples
        payload["postcondition_samples"] = samples
        if submit:
            payload["employer_confirmation"] = {
                "schema": "ats_greenhouse_employer_confirmation_v1",
                "provider": "greenhouse",
                "application_identity_sha256": action[
                    "application_identity_sha256"
                ],
                "route_id": "hosted_confirmation",
                "route_sha256": "6" * 64,
                "anchor_sha256": "7" * 64,
                "stable_surface_revision": revision,
                "stable_sample_count": 2,
                "observation_samples_sha256": hashlib.sha256(
                    canonical_bytes(samples)
                ).hexdigest(),
                "receipt_sha256": "4" * 64,
            }
        else:
            payload["surface_capsule"] = surface_capsule
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def main() -> int:
    profile = ast.literal_eval(assignment("_GREENHOUSE_ATS_UI_TOOL_PROFILE"))
    require(profile == "greenhouse-ats-ui", "Greenhouse profile name drifted")
    require(
        ast.literal_eval(assignment("GREENHOUSE_ATS_REQUIRED_HANDS_COMMIT"))
        == REQUIRED_HANDS_COMMIT,
        "reviewed Hands fd-only merge SHA drifted",
    )
    profile_map = assignment("_TOOL_PROFILE_ALLOWED")
    require(isinstance(profile_map, ast.Dict), "tool profile map is not exact")
    matching = [
        value
        for key, value in zip(profile_map.keys, profile_map.values, strict=True)
        if isinstance(key, ast.Name) and key.id == "_GREENHOUSE_ATS_UI_TOOL_PROFILE"
    ]
    require(len(matching) == 1, "Greenhouse profile is not unique")
    allowed = matching[0]
    require(
        isinstance(allowed, ast.Call)
        and isinstance(allowed.func, ast.Name)
        and allowed.func.id == "frozenset"
        and frozenset(ast.literal_eval(allowed.args[0])) == {"greenhouse_ats_ui"},
        "Greenhouse profile exposes more than its isolated tool",
    )
    tools = ast.literal_eval(assignment("TOOLS"))
    tool = next(
        item["function"]
        for item in tools
        if item.get("function", {}).get("name") == "greenhouse_ats_ui"
    )
    properties = tool["parameters"]["properties"]
    require(properties["action"]["enum"] == ["observe", "operate"], "action grammar widened")
    require(set(properties) == {"display", "action", "card_sha256"}, "private field exposed")
    forbidden = {"element", "ref", "value", "path", "selector", "approval", "review"}
    require(not forbidden.intersection(properties), "model received a private or approval field")

    direct_namespace = {
        "HTTPException": ValidationHTTPException,
        "TurnContext": object,
        "json": json,
        "re": re,
    }
    direct_display = load_function(
        "_greenhouse_ats_direct_display", direct_namespace
    )
    direct_result = load_function(
        "_greenhouse_ats_direct_result", direct_namespace
    )
    direct_namespace["_greenhouse_ats_direct_result"] = direct_result
    require(
        direct_display({"display": ":17"}) == ":17",
        "exact direct-route display was rejected",
    )
    for invalid_body in (
        {},
        {"display": ":17", "path": "/private"},
        {"display": ":17", "human_review_required": True},
        {"display": ":17", "approval": True},
        {"display": ":17", "review_queue": []},
    ):
        try:
            direct_display(invalid_body)
        except ValidationHTTPException as exc:
            require(exc.status_code == 400, "invalid direct input used the wrong status")
        else:
            raise AssertionError("direct route admitted an extra or forbidden request field")
    card_sha256 = "8" * 64
    direct_calls: list[tuple[str, dict, str, int]] = []
    observed_direct = {
        "ok": True,
        "display": ":17",
        "action": "observe",
        "greenhouse_ats_sequence": {
            "state": "ready_for_one_action",
            "card_sha256": card_sha256,
            "allowed_next": {
                "action": "operate",
                "card_sha256": card_sha256,
            },
            "next_mutation_authorized": False,
        },
    }
    operated_direct = {
        "ok": True,
        "display": ":17",
        "action": "operate",
        "greenhouse_ats_sequence": {
            "state": "action_receipted",
            "postcondition_proven": True,
            "receipt_event_hash": "4" * 64,
            "hands_result_sha256": "5" * 64,
            "hands_state": "action_ready",
            "mutation_count": 1,
            "hands_next_mutation_authorized": True,
            "next_mutation_authorized": False,
            "surface_capsule": {
                "schema": "ats_greenhouse_next_action_surface_v1",
            },
        },
    }

    async def execute_direct(
        name: str,
        arguments: dict,
        *,
        tool_call_id: str,
        round_num: int,
    ) -> str:
        direct_calls.append((name, arguments, tool_call_id, round_num))
        payload = observed_direct if round_num == 1 else operated_direct
        return json.dumps(payload)

    direct_namespace["execute_tool_call_async"] = execute_direct
    run_direct = load_function(
        "_run_greenhouse_ats_direct_one_action", direct_namespace
    )
    direct_terminal = asyncio.run(
        run_direct(SimpleNamespace(turn_id="turn-1"), ":17")
    )
    require(
        direct_terminal == operated_direct,
        "direct route did not return the raw terminal tool object",
    )
    require(
        direct_calls
        == [
            (
                "greenhouse_ats_ui",
                {"display": ":17", "action": "observe"},
                "direct:turn-1:observe",
                1,
            ),
            (
                "greenhouse_ats_ui",
                {
                    "display": ":17",
                    "action": "operate",
                    "card_sha256": card_sha256,
                },
                "direct:turn-1:operate",
                2,
            ),
        ],
        "direct route did not execute one exact two-phase frozen action",
    )
    direct_calls.clear()
    refused_direct = {
        "ok": False,
        "display": ":17",
        "action": "observe",
        "greenhouse_ats_sequence": {
            "state": "terminal_refusal",
            "first_failure": {"reason": "exact first mismatch"},
            "next_mutation_authorized": False,
        },
    }

    async def execute_refusal(
        name: str,
        arguments: dict,
        *,
        tool_call_id: str,
        round_num: int,
    ) -> str:
        direct_calls.append((name, arguments, tool_call_id, round_num))
        return json.dumps(refused_direct)

    direct_namespace["execute_tool_call_async"] = execute_refusal
    run_direct_refusal = load_function(
        "_run_greenhouse_ats_direct_one_action", direct_namespace
    )
    require(
        asyncio.run(
            run_direct_refusal(SimpleNamespace(turn_id="turn-refused"), ":17")
        )
        == refused_direct,
        "direct route did not return the first terminal refusal unchanged",
    )
    require(
        len(direct_calls) == 1 and direct_calls[0][3] == 1,
        "direct route retried or operated after the first refusal",
    )
    encoded_direct_terminal = json.dumps(direct_terminal, sort_keys=True)
    require(
        all(
            field not in encoded_direct_terminal
            for field in ("human_review_required", "approval", "review_queue")
        ),
        "direct terminal object added a human gate",
    )

    direct_route = source_function("greenhouse_ats_one_action")
    for token in (
        "_greenhouse_ats_direct_display(raw_body)",
        "_turn_context(request, {})",
        "turn.tool_profile != _GREENHOUSE_ATS_UI_TOOL_PROFILE",
        "_start_turn(turn)",
        "_run_while_downstream_connected(",
        "_run_greenhouse_ats_direct_one_action(turn, display)",
        'await _end_turn(turn, "greenhouse_one_action_complete")',
        "except BaseException:",
        'await _end_turn(turn, "greenhouse_one_action_error")',
        "_close_greenhouse_ats_pending(",
        "_request_context.reset(context_token)",
        "JSONResponse(content=result, headers=_turn_headers(turn))",
    ):
        require(token in direct_route, f"direct route lost lifecycle token {token}")
    for forbidden_token in ("_http", "messages", "model", "human_review", "approval"):
        require(
            forbidden_token not in direct_route,
            f"direct route entered forbidden path {forbidden_token}",
        )
    require(
        SOURCE.count('@app.post("/v1/greenhouse-ats/one-action")') == 1,
        "direct one-action route is missing or duplicated",
    )

    runtime_source = source_function("_greenhouse_ats_runtime")
    for token in (
        "run_ats_greenhouse_one_action.py",
        "rev-parse",
        "GREENHOUSE_ATS_HANDS_ROOT",
        "TAEY_GREENHOUSE_ATS_HANDS_COMMIT",
        "TAEY_GREENHOUSE_ATS_AT_SPI_BUS_FILE",
        "uuid.UUID(GREENHOUSE_ATS_HANDS_INCARNATION_ID)",
        "GREENHOUSE_ATS_HANDS_INCARNATION_ID != hands_incarnation_id",
    ):
        require(token in runtime_source, f"runtime lost exact binding {token}")
    require(
        "Path(TAEYS_HANDS_ROOT)" not in runtime_source,
        "Greenhouse still borrows the shared Hands checkout",
    )
    runtime_namespace = {
        "GREENHOUSE_ATS_BINDING": "greenhouse=:17",
        "GREENHOUSE_ATS_LEASE_SECRET": "5" * 64,
        "GREENHOUSE_ATS_HANDS_COMMIT": REQUIRED_HANDS_COMMIT,
        "GREENHOUSE_ATS_REQUIRED_HANDS_COMMIT": REQUIRED_HANDS_COMMIT,
        "GREENHOUSE_ATS_HANDS_INCARNATION_ID": "hands-greenhouse-prod",
        "GREENHOUSE_ATS_HANDS_ROOT": "",
        "GREENHOUSE_ATS_TIMEOUT_SECS": 29,
        "re": re,
        "uuid": uuid,
    }
    greenhouse_runtime = load_function("_greenhouse_ats_runtime", runtime_namespace)
    try:
        greenhouse_runtime()
    except RuntimeError as exc:
        require(
            str(exc)
            == "TAEY_GREENHOUSE_ATS_HANDS_INCARNATION_ID must be a lowercase UUID",
            "non-UUID Hands incarnation did not fail at the incarnation contract",
        )
    else:
        raise AssertionError("non-UUID Hands incarnation was accepted")
    runtime_namespace["GREENHOUSE_ATS_HANDS_INCARNATION_ID"] = (
        "00000000-0000-4000-8000-000000000010"
    )
    try:
        greenhouse_runtime()
    except RuntimeError as exc:
        require(
            str(exc) == "TAEY_GREENHOUSE_ATS_TIMEOUT_SECS must be 30-900",
            "canonical Hands UUID did not pass the incarnation contract",
        )
    else:
        raise AssertionError("runtime fixture unexpectedly passed its timeout sentinel")
    runtime_namespace.update({
        "GREENHOUSE_ATS_TIMEOUT_SECS": 180,
        "GREENHOUSE_ATS_FIREFOX_PID": "1",
        "GREENHOUSE_ATS_PYTHON": "/usr/bin/python3",
        "GREENHOUSE_ATS_RECEIPT_ROOT": "/tmp",
        "GREENHOUSE_ATS_AT_SPI_BUS_FILE": "/tmp/missing-at-spi-bus",
        "Path": Path,
        "os": os,
    })
    try:
        greenhouse_runtime()
    except RuntimeError as exc:
        require(
            str(exc)
            == "TAEY_GREENHOUSE_ATS_HANDS_ROOT does not contain the Greenhouse runner",
            "unset dedicated Hands root did not fail before shared-checkout access",
        )
    else:
        raise AssertionError("unset dedicated Greenhouse Hands root was accepted")
    action_source = source_function("_do_greenhouse_ats_ui")
    for token in (
        '"--transaction-fd"',
        '"--expected-transaction-sha256"',
        'pass_fds=(action_fd,)',
        '"ATS_ONE_ACTION_RECEIPT_ROOT"',
        '"ATS_PRESENCE_INCARNATION_ID"',
        'uuid.UUID(hex=str(context["process_generation"]))',
        '"side_effect_uncertain"',
        '"next_mutation_authorized": False',
    ):
        require(token in action_source, f"adapter lost containment token {token}")
    require('"--transaction",' not in action_source, "adapter retained path execution")
    require("action_path" not in action_source, "operate can still resolve a private path")
    sample_source = source_function("_greenhouse_ats_samples_prove")
    for token in (
        'samples[-2:]',
        '"postcondition_matched"',
        'stable[0]["revision"] == stable[1]["revision"]',
    ):
        require(token in sample_source, f"sample proof lost {token}")

    resolver_namespace = {
        "GREENHOUSE_ATS_PRIVATE_ROOT": "",
        "Path": Path,
        "_SEAT_ID_RE": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"),
        "_TRACE_ID_RE": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"),
        "hashlib": hashlib,
        "json": json,
        "os": os,
        "re": re,
        "stat": stat,
        "uuid": uuid,
    }
    load_function("_read_revenue_ui_private_json", resolver_namespace)
    open_action = load_function(
        "_open_greenhouse_ats_frozen_action", resolver_namespace
    )
    resolve_manifest = load_function(
        "_resolve_greenhouse_ats_private_manifest", resolver_namespace
    )
    action_namespace = {
        "_request_context": RequestContext,
        "_GREENHOUSE_ATS_UI_TOOL_PROFILE": profile,
        "_SEAT_ID_RE": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"),
        "_TRACE_ID_RE": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"),
        "GREENHOUSE_ATS_HANDS_COMMIT": REQUIRED_HANDS_COMMIT,
        "GREENHOUSE_ATS_FIREFOX_PID": "12345",
        "GREENHOUSE_ATS_LEASE_SECRET": "5" * 64,
        "GREENHOUSE_ATS_HANDS_INCARNATION_ID": "00000000-0000-4000-8000-000000000010",
        "canonical_json_bytes": canonical_bytes,
        "hashlib": hashlib,
        "os": os,
        "re": re,
        "uuid": uuid,
    }
    close_pending = load_function("_close_greenhouse_ats_pending", action_namespace)
    samples_prove = load_function("_greenhouse_ats_samples_prove", action_namespace)
    surface_capsule_proves = load_function(
        "_greenhouse_ats_surface_capsule_proves",
        action_namespace,
    )
    confirmation_proves = load_function(
        "_greenhouse_ats_confirmation_proves",
        action_namespace,
    )
    action_namespace["_greenhouse_ats_surface_capsule_proves"] = (
        surface_capsule_proves
    )
    action_namespace["_greenhouse_ats_confirmation_proves"] = confirmation_proves
    do_action = load_function("_do_greenhouse_ats_ui", action_namespace)

    valid_samples = [
        {
            "sample": sample,
            "elapsed_ms": sample * 100,
            "revision": "3" * 64,
            "postcondition_matched": True,
            "refresh_policy": "invalidate_reacquire",
        }
        for sample in (1, 2)
    ]
    require(
        samples_prove(valid_samples, refresh_policy="invalidate_reacquire"),
        "two exact stable postcondition samples were rejected",
    )
    unmatched = [dict(item) for item in valid_samples]
    unmatched[-1]["postcondition_matched"] = False
    require(
        not samples_prove(unmatched, refresh_policy="invalidate_reacquire"),
        "an unmatched required postcondition sample was accepted",
    )
    require(
        not samples_prove([], refresh_policy="invalidate_reacquire"),
        "empty postcondition evidence was accepted",
    )
    capsule_fixture = json.loads(success_result({
        "transaction_id": "00000000-0000-4000-8000-000000000000",
        "action_id": "00000000-0000-4000-8000-000000000001",
        "application_identity_sha256": "1" * 64,
        "action": {"kind": "observe_form"},
    }).stdout)
    require(
        surface_capsule_proves(
            capsule_fixture["surface_capsule"],
            application_identity_sha256="1" * 64,
            full_surface=capsule_fixture["surface"],
        ),
        "bounded next-action surface capsule was rejected",
    )
    leaked_capsule = dict(capsule_fixture["surface_capsule"])
    leaked_capsule["value_sha256"] = "9" * 64
    require(
        not surface_capsule_proves(
            leaked_capsule,
            application_identity_sha256="1" * 64,
            full_surface=capsule_fixture["surface"],
        ),
        "applicant value digest was accepted in the bounded capsule",
    )
    missing_completion = dict(capsule_fixture["surface_capsule"])
    missing_completion.pop("required_controls_complete")
    require(
        not surface_capsule_proves(
            missing_completion,
            application_identity_sha256="1" * 64,
            full_surface=capsule_fixture["surface"],
        ),
        "missing required-control completion evidence was accepted",
    )
    non_boolean_completion = dict(capsule_fixture["surface_capsule"])
    non_boolean_completion["required_controls_complete"] = 0
    require(
        not surface_capsule_proves(
            non_boolean_completion,
            application_identity_sha256="1" * 64,
            full_surface=capsule_fixture["surface"],
        ),
        "non-boolean required-control completion evidence was accepted",
    )
    mismatched_completion = dict(capsule_fixture["surface_capsule"])
    mismatched_completion["required_controls_complete"] = True
    require(
        not surface_capsule_proves(
            mismatched_completion,
            application_identity_sha256="1" * 64,
            full_surface=capsule_fixture["surface"],
        ),
        "required-control completion evidence diverged from the full surface",
    )
    for forbidden_field in (
        "human_review_required",
        "approval",
        "review_queue",
    ):
        context = request_context(1)
        RequestContext.value = context
        refused = json.loads(do_action({
            "display": ":17",
            "action": "observe",
            forbidden_field: True,
        }))
        require(
            refused["ok"] is False
            and context["_tool_profile_state"]["terminal"] is not None,
            f"{forbidden_field} was not terminally refused",
        )
    RequestContext.value = {}

    with tempfile.TemporaryDirectory(prefix="greenhouse-ats-presence-") as temp:
        root = Path(temp)
        frozen_action = write_private_transaction(root)
        resolver_namespace["GREENHOUSE_ATS_PRIVATE_ROOT"] = str(root)
        action_namespace["_resolve_greenhouse_ats_private_manifest"] = resolve_manifest
        action_namespace["_greenhouse_ats_runtime"] = lambda: {
            "display": ":17",
            "python": "/usr/bin/python3",
            "runner": "/public/run_ats_greenhouse_one_action.py",
            "bus": "unix:path=/run/user/1000/at-spi/bus_0",
            "receipt_root": str(root),
            "timeout": 180,
        }
        active = request_context(1)
        RequestContext.value = active
        observed = json.loads(do_action({"display": ":17", "action": "observe"}))
        require(observed["ok"] is True, "opaque-card observation failed")
        card_sha256 = observed["greenhouse_ats_sequence"]["card_sha256"]
        require(re.fullmatch(r"[0-9a-f]{64}", card_sha256) is not None, "card is not opaque")
        require("kind" not in json.dumps(observed), "action kind leaked to model")
        held_fd = active["_greenhouse_ats_ui_sequence"]["pending"]["action_fd"]
        original_raw = os.pread(held_fd, 4 * 1024 * 1024, 0)
        action_path = root / "actions" / "seat-1" / "action.json"
        action_path.unlink()
        replacement = dict(frozen_action)
        replacement["action_id"] = "00000000-0000-4000-8000-000000000009"
        action_path.write_bytes(canonical_bytes(replacement))
        os.chmod(action_path, 0o400)
        active["tool_round"] = 2
        def execute_held_inode(*args: object, **kwargs: object) -> SimpleNamespace:
            inherited = kwargs.get("pass_fds")
            require(inherited == (held_fd,), "subprocess did not inherit exactly the held fd")
            child_env = kwargs.get("env")
            require(
                isinstance(child_env, dict)
                and child_env.get("ATS_PRESENCE_INCARNATION_ID")
                == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "Presence process generation was not preserved as a canonical UUID",
            )
            require(
                child_env.get("ATS_HANDS_INCARNATION_ID")
                == "00000000-0000-4000-8000-000000000010",
                "Hands incarnation was not forwarded as its canonical UUID",
            )
            require(
                os.pread(held_fd, 4 * 1024 * 1024, 0) == original_raw,
                "operate did not retain the observe-time inode bytes",
            )
            return success_result(frozen_action)
        with patch.object(subprocess, "run", side_effect=execute_held_inode) as run:
            operated = json.loads(do_action({
                "display": ":17",
                "action": "operate",
                "card_sha256": card_sha256,
            }))
        require(operated["ok"] is True, "one frozen Hands action did not receipt")
        receipt = operated["greenhouse_ats_sequence"]
        require(receipt["postcondition_proven"] is True, "postcondition was not surfaced")
        require(receipt["receipt_event_hash"] == "4" * 64, "Hands receipt hash drifted")
        require(receipt["next_mutation_authorized"] is False, "same turn retained mutation authority")
        require(
            set(receipt)
            == {
                "state",
                "postcondition_proven",
                "receipt_event_hash",
                "hands_result_sha256",
                "hands_state",
                "mutation_count",
                "hands_next_mutation_authorized",
                "next_mutation_authorized",
                "surface_capsule",
            },
            "Presence exposed more than the bounded non-submit receipt",
        )
        encoded_receipt = json.dumps(receipt)
        require(
            "private-applicant-value" not in encoded_receipt
            and "value_sha256" not in encoded_receipt
            and "semantic_values" not in encoded_receipt,
            "Presence exposed applicant values from the full Hands surface",
        )
        command = run.call_args.args[0]
        require(
            command[-4:]
            == [
                "--transaction-fd",
                str(held_fd),
                "--expected-transaction-sha256",
                hashlib.sha256(original_raw).hexdigest(),
            ],
            "runner did not receive the exact inherited-fd contract",
        )
        try:
            os.fstat(held_fd)
        except OSError:
            pass
        else:
            raise AssertionError("spent action fd remained open after operate")
        RequestContext.value = {}

        action_path.unlink()
        action_path.write_bytes(original_raw)
        os.chmod(action_path, 0o400)
        os.chmod(action_path, 0o600)
        try:
            resolve_manifest(request_context(1))
        except RuntimeError:
            pass
        else:
            raise AssertionError("mutable 0600 frozen action was accepted")
        finally:
            os.chmod(action_path, 0o400)
        symlink_path = action_path.with_name("symlink.json")
        symlink_path.symlink_to(action_path)
        try:
            open_action(symlink_path, root)
        except RuntimeError:
            pass
        else:
            raise AssertionError("symlink frozen action was accepted")
        symlink_path.unlink()

        guarded = request_context(1)
        RequestContext.value = guarded
        observed = json.loads(do_action({"display": ":17", "action": "observe"}))
        guarded_fd = guarded["_greenhouse_ats_ui_sequence"]["pending"]["action_fd"]
        guarded["tool_round"] = 2
        with patch.object(subprocess, "run") as run:
            refused = json.loads(do_action({
                "display": ":17",
                "action": "operate",
                "card_sha256": "9" * 64,
            }))
        require(refused["ok"] is False, "wrong opaque card was accepted")
        require(guarded["_tool_profile_state"]["terminal"] is not None, "mismatch was not terminal")
        run.assert_not_called()
        try:
            os.fstat(guarded_fd)
        except OSError:
            pass
        else:
            raise AssertionError("wrong-card terminal refusal leaked the action fd")
        RequestContext.value = {}

        cleanup = request_context(1)
        RequestContext.value = cleanup
        observed = json.loads(do_action({"display": ":17", "action": "observe"}))
        require(observed["ok"] is True, "cleanup setup observe failed")
        cleanup_fd = cleanup["_greenhouse_ats_ui_sequence"]["pending"]["action_fd"]
        close_pending(cleanup["_greenhouse_ats_ui_sequence"])
        try:
            os.fstat(cleanup_fd)
        except OSError:
            pass
        else:
            raise AssertionError("outer cleanup helper leaked the action fd")
        RequestContext.value = {}

    with tempfile.TemporaryDirectory(prefix="greenhouse-ats-submit-presence-") as temp:
        root = Path(temp)
        frozen_submit = write_private_transaction(root, "submit")
        resolver_namespace["GREENHOUSE_ATS_PRIVATE_ROOT"] = str(root)
        action_namespace["_resolve_greenhouse_ats_private_manifest"] = resolve_manifest
        action_namespace["_greenhouse_ats_runtime"] = lambda: {
            "display": ":17",
            "python": "/usr/bin/python3",
            "runner": "/public/run_ats_greenhouse_one_action.py",
            "bus": "unix:path=/run/user/1000/at-spi/bus_0",
            "receipt_root": str(root),
            "timeout": 180,
        }
        active = request_context(1)
        RequestContext.value = active
        observed = json.loads(do_action({"display": ":17", "action": "observe"}))
        active["tool_round"] = 2
        with patch.object(
            subprocess,
            "run",
            return_value=success_result(frozen_submit),
        ):
            operated = json.loads(do_action({
                "display": ":17",
                "action": "operate",
                "card_sha256": observed["greenhouse_ats_sequence"]["card_sha256"],
            }))
        require(operated["ok"] is True, "terminal submit evidence was refused")
        receipt = operated["greenhouse_ats_sequence"]
        require(
            set(receipt)
            == {
                "state",
                "postcondition_proven",
                "receipt_event_hash",
                "hands_result_sha256",
                "hands_state",
                "mutation_count",
                "hands_next_mutation_authorized",
                "next_mutation_authorized",
                "employer_confirmation",
            },
            "Presence exposed more than terminal employer confirmation",
        )
        confirmation = receipt["employer_confirmation"]
        submit_result = json.loads(success_result(frozen_submit).stdout)
        require(
            confirmation_proves(
                confirmation,
                application_identity_sha256="1" * 64,
                full_surface=submit_result["surface"],
                samples=submit_result["postcondition_samples"],
                receipt_event_hash="4" * 64,
            ),
            "exact employer confirmation capsule was rejected",
        )
        wrong_receipt = dict(confirmation)
        wrong_receipt["receipt_sha256"] = "8" * 64
        require(
            not confirmation_proves(
                wrong_receipt,
                application_identity_sha256="1" * 64,
                full_surface=submit_result["surface"],
                samples=submit_result["postcondition_samples"],
                receipt_event_hash="4" * 64,
            ),
            "employer confirmation accepted the wrong Hands receipt",
        )
        require(
            "private-applicant-value" not in json.dumps(receipt),
            "terminal confirmation exposed applicant values",
        )
        RequestContext.value = {}

    prompt = (SERVING_ROOT / "TAEY_GREENHOUSE_ATS_UI_SYSTEM.md").read_text(encoding="utf-8")
    require("Never choose or supply" in prompt, "prompt exposed private choice")
    require("Never retry" in prompt, "prompt lost first-error containment")
    require(
        "same exact display and the exact returned `card_sha256`" in prompt,
        "operate prompt no longer carries the bound display",
    )
    require("review" not in prompt.lower() and "approval" not in prompt.lower(), "prompt added a human gate")
    require(
        "_close_greenhouse_ats_pending" in source_function("chat_completions"),
        "outer request cleanup no longer closes a pending action fd",
    )
    print("greenhouse ATS isolated one-action Presence profile: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
