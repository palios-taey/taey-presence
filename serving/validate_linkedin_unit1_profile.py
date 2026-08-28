#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVING_ROOT = REPO_ROOT / "serving"
SOMA_PROXY = SERVING_ROOT / "soma_proxy.py"
PROXY_SOURCE = SOMA_PROXY.read_text(encoding="utf-8")
PROXY_TREE = ast.parse(PROXY_SOURCE, filename=str(SOMA_PROXY))


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


def source_function(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


def proxy_assignment(name: str) -> ast.expr:
    candidates: list[ast.expr] = []
    for node in PROXY_TREE.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            candidates.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            candidates.append(node.value)
    require(len(candidates) == 1, f"{name} is not one exact assignment")
    return candidates[0]


def proxy_function(name: str, namespace: dict[str, object]) -> object:
    candidates = [
        node
        for node in PROXY_TREE.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    require(len(candidates) == 1, f"{name} is not one exact function")
    module = ast.Module(body=[candidates[0]], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SOMA_PROXY), "exec"), namespace)
    return namespace[name]


class RequestContext:
    value: dict = {}

    @classmethod
    def get(cls) -> dict:
        return cls.value


def private_input() -> dict:
    text = "Frozen private comment."
    return {
        "schema": "linkedin_unit1_private_input_v1",
        "operation": "comment_from_notifications",
        "cycle_id": "cycle-1",
        "transaction_id": "transaction-1",
        "display": ":18",
        "policy_sha256": "1" * 64,
        "notification_stream_sha256": "2" * 64,
        "selected_activity": "123456789",
        "selected_age_seconds": 3600,
        "freshness_max_hours": 72,
        "target_passed": True,
        "dedup_passed": True,
        "author_cooloff_passed": True,
        "selected_post_body_sha256": "3" * 64,
        "thread_evidence_sha256": "4" * 64,
        "like_authorized": True,
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "expected_author_name": "Private Author",
    }


def write_bundle(root: Path) -> None:
    transaction_dir = root / "transactions" / "seat-1"
    transaction_dir.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    os.chmod(root / "transactions", 0o700)
    os.chmod(transaction_dir, 0o700)
    bundle = {
        "schema": "taey_linkedin_unit1_private_bundle_v1",
        "seat_id": "seat-1",
        "event_id": "event-1",
        "correlation_id": "correlation-1",
        "private_input": private_input(),
        "receipts": [],
    }
    path = transaction_dir / "correlation-1.json"
    path.write_bytes(canonical_bytes(bundle))
    os.chmod(path, 0o400)


def context(round_num: int) -> dict:
    return {
        "tool_profile": "linkedin-unit1",
        "seat_id": "seat-1",
        "event_id": "event-1",
        "correlation_id": "correlation-1",
        "turn_id": "turn-1",
        "process_generation": "a" * 32,
        "tool_round": round_num,
        "_linkedin_unit1_sequence": {"receipts": [], "terminal": None},
        "_tool_profile_state": {"terminal": None},
    }


def completed(result: dict) -> SimpleNamespace:
    payload = {
        "ok": True,
        "display": ":18",
        "platform": "linkedin",
        "result": result,
        "error": None,
    }
    return SimpleNamespace(
        returncode=0,
        stdout=canonical_bytes(payload),
        stderr=b"",
    )


def main() -> int:
    profile = ast.literal_eval(proxy_assignment("_LINKEDIN_UNIT1_TOOL_PROFILE"))
    require(profile == "linkedin-unit1", "LinkedIn Unit 1 profile name drifted")
    profile_map = proxy_assignment("_TOOL_PROFILE_ALLOWED")
    require(isinstance(profile_map, ast.Dict), "tool profile map is not exact")
    matching_values = [
        value
        for key, value in zip(profile_map.keys, profile_map.values, strict=True)
        if isinstance(key, ast.Name) and key.id == "_LINKEDIN_UNIT1_TOOL_PROFILE"
    ]
    require(len(matching_values) == 1, "LinkedIn Unit 1 profile is not unique")
    allowed_call = matching_values[0]
    require(
        isinstance(allowed_call, ast.Call)
        and isinstance(allowed_call.func, ast.Name)
        and allowed_call.func.id == "frozenset"
        and len(allowed_call.args) == 1
        and frozenset(ast.literal_eval(allowed_call.args[0]))
        == frozenset({"linkedin_unit1"}),
        "LinkedIn Unit 1 profile exposes more than one isolated tool",
    )
    tools = ast.literal_eval(proxy_assignment("TOOLS"))
    tool = next(
        item["function"]
        for item in tools
        if item.get("function", {}).get("name") == "linkedin_unit1"
    )
    require(
        tool["parameters"]["properties"]["action"]["enum"]
        == ["observe", "operate"],
        "LinkedIn Unit 1 model action grammar widened",
    )
    require(
        "element" not in tool["parameters"]["properties"],
        "LinkedIn Unit 1 exposes element choice to the model",
    )
    ui_action_source = source_function(SERVING_ROOT / "soma_proxy.py", "_do_ui_action")
    require(
        "context.get(\"tool_profile\") != _REVENUE_UI_TOOL_PROFILE"
        in ui_action_source,
        "generic revenue ui_action profile boundary was widened",
    )
    compile_source = source_function(
        SERVING_ROOT / "ui_drive.py",
        "_linkedin_unit1_compile",
    )
    operate_source = source_function(
        SERVING_ROOT / "ui_drive.py",
        "_linkedin_unit1_operate",
    )
    for token in (
        "_revenue_snapshot(deps)",
        "compile_unit1_step(",
        "_linkedin_unit1_runtime_card",
    ):
        require(token in compile_source, f"compile path lost {token}")
    for token in (
        "fresh_card != stored_card",
        "fresh_runtime_card != stored_runtime_card",
        "accept_unit1_step(",
        '"ui-activate"',
        '"ui-scroll-into-view"',
        '"ui-paste"',
    ):
        require(token in operate_source, f"operate path lost {token}")

    resolver_namespace = {
        "LINKEDIN_UNIT1_PRIVATE_ROOT": "",
        "Path": Path,
        "_SEAT_ID_RE": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"),
        "_TRACE_ID_RE": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"),
        "hashlib": hashlib,
        "json": json,
        "os": os,
        "stat": stat,
    }
    proxy_function("_read_revenue_ui_private_json", resolver_namespace)
    resolve_bundle = proxy_function(
        "_resolve_linkedin_unit1_private_bundle",
        resolver_namespace,
    )
    action_namespace = {
        "_request_context": RequestContext,
        "_LINKEDIN_UNIT1_TOOL_PROFILE": profile,
        "_UI_ACTION_BINDINGS": {":18": "linkedin"},
        "_SEAT_ID_RE": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"),
        "_TRACE_ID_RE": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"),
        "_DRIVE_GENERATION_FENCE_KEY": "taey:soma:drive_process_generation:validator",
        "UI_DRIVE_PYTHON": "/usr/bin/python3",
        "UI_DRIVE_SCRIPT": "/public/serving/ui_drive.py",
        "canonical_json_bytes": canonical_bytes,
        "hashlib": hashlib,
        "os": os,
        "re": re,
    }
    do_linkedin_unit1 = proxy_function("_do_linkedin_unit1", action_namespace)

    with tempfile.TemporaryDirectory(prefix="linkedin-unit1-presence-") as temp:
        root = Path(temp)
        write_bundle(root)
        resolver_namespace["LINKEDIN_UNIT1_PRIVATE_ROOT"] = str(root)
        action_namespace["_resolve_linkedin_unit1_private_bundle"] = resolve_bundle
        request_context = context(1)
        RequestContext.value = request_context
        try:
            card_sha256 = "5" * 64
            runtime_card_sha256 = "6" * 64
            compile_result = {
                "schema": "taey_linkedin_unit1_compiled_step_v1",
                "card": {
                    "card_sha256": card_sha256,
                    "phase": "notifications_navigation",
                },
                "runtime_card": {"card_sha256": runtime_card_sha256},
            }
            with patch.object(subprocess, "run", return_value=completed(compile_result)):
                observed = json.loads(do_linkedin_unit1({
                    "display": ":18",
                    "action": "observe",
                }))
            require(observed["ok"] is True, "observe/compile did not succeed")
            require(
                observed["unit1_sequence"]["allowed_next"]
                == {"action": "operate", "card_sha256": card_sha256},
                "observe did not return one exact opaque-card operation",
            )
            request_context["tool_round"] = 2
            receipt = {
                "receipt_sha256": "7" * 64,
                "terminal_delivery_verified": True,
            }
            operate_result = {
                "schema": "taey_linkedin_unit1_operated_step_v1",
                "card_sha256": card_sha256,
                "phase": "comment_submit",
                "receipt": receipt,
                "terminal": True,
                "operation_evidence_sha256": "8" * 64,
            }
            with patch.object(subprocess, "run", return_value=completed(operate_result)):
                operated = json.loads(do_linkedin_unit1({
                    "display": ":18",
                    "action": "operate",
                    "card_sha256": card_sha256,
                }))
            require(
                operated["unit1_sequence"]["state"]
                == "terminal_delivery_verified",
                "final rendered-comment receipt was not terminal",
            )
            require(
                request_context["_linkedin_unit1_sequence"]["receipts"] == [receipt],
                "server-owned receipt chain did not append the accepted receipt",
            )
            require(
                "pending" not in request_context["_linkedin_unit1_sequence"],
                "spent opaque card remained reusable",
            )
        finally:
            RequestContext.value = {}

        wrong_context = context(1)
        RequestContext.value = wrong_context
        try:
            refusal = json.loads(do_linkedin_unit1({
                "display": ":18",
                "action": "operate",
                "card_sha256": "9" * 64,
            }))
            require(refusal["ok"] is False, "operate without observe was accepted")
            require(
                isinstance(wrong_context["_tool_profile_state"]["terminal"], dict),
                "first mismatch did not terminalize the profile",
            )
        finally:
            RequestContext.value = {}

        for advance_round, supplied_sha256, expected_reason in (
            (1, "5" * 64, "earlier model round"),
            (2, "9" * 64, "exact preceding opaque card"),
        ):
            guarded_context = context(1)
            RequestContext.value = guarded_context
            try:
                compile_result = {
                    "schema": "taey_linkedin_unit1_compiled_step_v1",
                    "card": {
                        "card_sha256": "5" * 64,
                        "phase": "notifications_navigation",
                    },
                    "runtime_card": {"card_sha256": "6" * 64},
                }
                with patch.object(
                    subprocess,
                    "run",
                    return_value=completed(compile_result),
                ):
                    observed = json.loads(do_linkedin_unit1({
                        "display": ":18",
                        "action": "observe",
                    }))
                require(observed["ok"] is True, "guard setup observe failed")
                guarded_context["tool_round"] = advance_round
                with patch.object(subprocess, "run") as run:
                    refusal = json.loads(do_linkedin_unit1({
                        "display": ":18",
                        "action": "operate",
                        "card_sha256": supplied_sha256,
                    }))
                require(refusal["ok"] is False, f"{expected_reason} guard failed")
                require(
                    expected_reason in refusal["error"],
                    f"{expected_reason} was not the first mismatch",
                )
                run.assert_not_called()
            finally:
                RequestContext.value = {}

    prompt = (SERVING_ROOT / "TAEY_LINKEDIN_UNIT1_SYSTEM.md").read_text(
        encoding="utf-8"
    )
    require("Never choose or supply an element" in prompt, "prompt exposes target choice")
    require("Never retry" in prompt, "prompt lost first-error containment")
    print("linkedin Unit 1 isolated Presence profile: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
