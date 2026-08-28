#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVING_ROOT = REPO_ROOT / "serving"
sys.path.insert(0, str(SERVING_ROOT))
publisher = importlib.import_module("linkedin_unit1_prepare_publisher")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_refusal(operation, message: str) -> None:
    try:
        operation()
    except (publisher.LinkedInUnit1PreparePublisherError, RuntimeError):
        return
    raise AssertionError(message)


def source_function(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    require(len(matches) == 1, f"{name} is not one exact function")
    return ast.get_source_segment(source, matches[0]) or ""


def assignment(path: Path, name: str) -> ast.expr:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            values.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            values.append(node.value)
    require(len(values) == 1, f"{name} is not one exact assignment")
    return values[0]


def bootstrap(seat: str, event: str, correlation: str) -> dict:
    policy = {
        "draft_policy": "Write one specific, useful response grounded in the exact thread.",
        "expected_author_name": "Taey Operator",
        "identity_context": "Private professional identity and expertise context.",
        "like_authorized": True,
        "selection_policy": "Select one fresh relevant post with no duplicate or author-cooloff conflict.",
    }
    return {
        "schema": publisher.BOOTSTRAP_SCHEMA,
        "seat_id": seat,
        "event_id": event,
        "correlation_id": correlation,
        "identity_context": policy["identity_context"],
        "selection_policy": policy["selection_policy"],
        "draft_policy": policy["draft_policy"],
        "expected_author_name": policy["expected_author_name"],
        "like_authorized": policy["like_authorized"],
        "preparation": {
            "schema": publisher.PREPARATION_ENVELOPE_SCHEMA,
            "operation": "comment_from_notifications_prepare",
            "cycle_id": "cycle-1",
            "transaction_id": "transaction-1",
            "display": ":18",
            "policy_sha256": publisher.canonical_sha256(policy),
            "selection": None,
        },
    }


def inventory() -> dict:
    text = "A target posted: exact notification text"
    artifact = {
        "schema": publisher.NOTIFICATION_INVENTORY_SCHEMA,
        "platform": "linkedin",
        "route": "notifications_all",
        "snapshot_revision": "1" * 64,
        "mounted_article_count": 1,
        "rows": [{
            "activity": "123456789",
            "actionable": True,
            "age_seconds": 3600,
            "age_token": "1h",
            "article_name": "Notification",
            "article_states": ["enabled", "showing"],
            "notification_text": text,
            "notification_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "ordinal": 1,
            "snapshot_revision": "1" * 64,
            "structural_path": [0],
        }],
        "actionable_links": [{
            "activity": "123456789",
            "age_seconds": 3600,
            "element": "linkedin_notification_candidate_001_activity_123456789",
            "element_sha256": "2" * 64,
            "ordinal": 1,
            "uri": "https://www.linkedin.com/feed/update/urn:li:activity:123456789/",
            "uri_sha256": "3" * 64,
        }],
    }
    artifact["inventory_sha256"] = publisher.canonical_sha256(artifact)
    return artifact


def source(selection: dict) -> dict:
    body = "Exact selected post body."
    row_text = "Existing exact comment."
    value = {
        "schema": publisher.SELECTED_SOURCE_SCHEMA,
        "platform": "linkedin",
        "selected_activity": selection["selected_activity"],
        "snapshot_revision": "4" * 64,
        "notification_inventory_sha256": selection[
            "notification_inventory_sha256"
        ],
        "selection_sha256": selection["selection_sha256"],
        "thread_open_receipt_sha256": "5" * 64,
        "transaction_sha256": selection["transaction_sha256"],
        "post": {
            "body": body,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        },
        "thread": {
            "exact_comment_count": 1,
            "read_complete": True,
            "typed_rows": [{
                "author_name": "Another Person",
                "kind": "text",
                "ordinal": 1,
                "text": row_text,
                "text_sha256": hashlib.sha256(row_text.encode()).hexdigest(),
            }],
        },
    }
    value["source_sha256"] = publisher.canonical_sha256(value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hands-root")
    args = parser.parse_args()
    hands_unit1 = None
    soma = None
    if args.hands_root:
        hands_root = Path(args.hands_root).resolve(strict=True)
        sys.path.insert(0, str(hands_root))
        hands_unit1 = importlib.import_module(
            "consultation_v2.platforms.linkedin.unit1"
        )
        soma = importlib.import_module("soma_proxy")

    proxy = SERVING_ROOT / "soma_proxy.py"
    drive = SERVING_ROOT / "ui_drive.py"
    prompt = (SERVING_ROOT / "TAEY_LINKEDIN_UNIT1_PREPARE_SYSTEM.md").read_text(
        encoding="utf-8"
    )
    profile = ast.literal_eval(assignment(proxy, "_LINKEDIN_UNIT1_PREPARE_TOOL_PROFILE"))
    require(profile == "linkedin-unit1-prepare", "preparation profile name drifted")
    profile_map = assignment(proxy, "_TOOL_PROFILE_ALLOWED")
    require(isinstance(profile_map, ast.Dict), "profile map is not exact")
    matches = [
        value
        for key, value in zip(profile_map.keys, profile_map.values, strict=True)
        if isinstance(key, ast.Name)
        and key.id == "_LINKEDIN_UNIT1_PREPARE_TOOL_PROFILE"
    ]
    require(len(matches) == 1, "preparation profile is not unique")
    allowed = matches[0]
    require(
        isinstance(allowed, ast.Call)
        and isinstance(allowed.func, ast.Name)
        and allowed.func.id == "frozenset"
        and frozenset(ast.literal_eval(allowed.args[0]))
        == frozenset({"linkedin_unit1_prepare"}),
        "preparation profile exposes more than one tool",
    )
    tools = ast.literal_eval(assignment(proxy, "TOOLS"))
    tool = next(
        row["function"]
        for row in tools
        if row.get("function", {}).get("name") == "linkedin_unit1_prepare"
    )
    require(
        tool["parameters"]["properties"]["action"]["enum"]
        == ["observe", "operate", "select", "draft"],
        "preparation action grammar drifted",
    )
    for forbidden in ("selector", "coordinate", "url", "path", "element"):
        require(
            forbidden not in tool["parameters"]["properties"],
            f"model-facing preparation tool exposes {forbidden}",
        )
    lowered_prompt = prompt.lower()
    require(
        "there is no human review or approval step" in lowered_prompt,
        "autonomous no-human-approval boundary is missing",
    )
    require(
        "review before" not in lowered_prompt and "await approval" not in lowered_prompt,
        "human approval language entered the preparation prompt",
    )
    operate_source = source_function(drive, "_linkedin_unit1_prepare_operate")
    for token in (
        "accept_preparation_step(",
        "_revenue_snapshot(deps)",
        '"activate": "ui-activate"',
        '"mapped_pointer_activate": "ui-activate"',
        '"scroll_into_view": "ui-scroll-into-view"',
    ):
        require(token in operate_source, f"preparation operation lost {token}")
    for forbidden in (
        "paste_frozen_text",
        "submit_frozen_comment",
        "activate_optional_like",
        "ui-paste",
    ):
        require(
            forbidden not in operate_source,
            f"preparation profile can execute forbidden {forbidden}",
        )
    handler_source = source_function(proxy, "_do_linkedin_unit1_prepare")
    require(
        'sequence["published"] = published' in handler_source
        and "_publish_linkedin_unit1_private_bundle(" in handler_source,
        "publisher does not terminalize exactly after publication",
    )
    transport_source = source_function(
        proxy, "_linkedin_unit1_prepare_transport_action"
    )
    transport_namespace: dict[str, object] = {}
    exec(transport_source, transport_namespace)
    transport_action = transport_namespace[
        "_linkedin_unit1_prepare_transport_action"
    ]
    require(
        transport_action("observe") == "compile"
        and transport_action("operate") == "operate",
        "model actions do not map to the exact production transport domain",
    )
    for private_action in ("select", "draft"):
        expect_refusal(
            lambda action=private_action: transport_action(action),
            f"private {private_action} decision entered the UI transport domain",
        )
    parser_source = source_function(drive, "_parser")
    parser_tree = ast.parse(parser_source)
    prepare_commands = {
        node.args[0].value
        for node in ast.walk(parser_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.startswith("linkedin-unit1-prepare-")
    }
    require(
        prepare_commands
        == {
            "linkedin-unit1-prepare-compile",
            "linkedin-unit1-prepare-operate",
        },
        "ui_drive preparation command domain drifted",
    )
    require(
        "transport_action = _linkedin_unit1_prepare_transport_action(action)"
        in handler_source
        and 'f"linkedin-unit1-prepare-{transport_action}"' in handler_source
        and 'f"linkedin-unit1-prepare-{action}"' not in handler_source,
        "production handler bypasses the exact preparation transport mapping",
    )
    publication_source = source_function(
        proxy, "_publish_linkedin_unit1_private_bundle"
    )
    for token in (
        "_open_private_directory(",
        "_write_private_json(",
        "os.chmod(name, 0o400",
        "except FileExistsError",
    ):
        require(token in publication_source, f"owner-only publisher lost {token}")

    seat, event, correlation = "seat-1", "event-1", "correlation-1"
    frozen_bootstrap = bootstrap(seat, event, correlation)
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        prepare_root = base / "prepare"
        final_root = base / "final"
        for root in (prepare_root, final_root):
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
        bootstrap_dir = prepare_root / "transactions" / seat
        bootstrap_dir.mkdir(parents=True, mode=0o700)
        os.chmod(prepare_root / "transactions", 0o700)
        os.chmod(bootstrap_dir, 0o700)
        bootstrap_path = bootstrap_dir / f"{correlation}.json"
        bootstrap_path.write_bytes(publisher.canonical_json_bytes(frozen_bootstrap))
        os.chmod(bootstrap_path, 0o400)
        context = {
            "seat_id": seat,
            "event_id": event,
            "correlation_id": correlation,
        }
        if soma is not None:
            soma.LINKEDIN_UNIT1_PREPARE_PRIVATE_ROOT = str(prepare_root)
            soma.LINKEDIN_UNIT1_PRIVATE_ROOT = str(final_root)
            loaded = soma._resolve_linkedin_unit1_prepare_bootstrap(context)
            bootstrap_sha256 = loaded.pop("bootstrap_sha256")
        else:
            loaded = publisher.validate_bootstrap(
                frozen_bootstrap,
                seat_id=seat,
                event_id=event,
                correlation_id=correlation,
            )
            bootstrap_sha256 = hashlib.sha256(
                publisher.canonical_json_bytes(frozen_bootstrap)
            ).hexdigest()
        require(
            bootstrap_sha256
            == hashlib.sha256(publisher.canonical_json_bytes(frozen_bootstrap)).hexdigest(),
            "bootstrap digest is not exact",
        )
        selection_input = {
            "schema": "linkedin_unit1_private_selection_input_v1",
            "policy_sha256": loaded["preparation"]["policy_sha256"],
            "transaction_sha256": publisher.preparation_transaction_sha256(
                loaded["preparation"]
            ),
            "notification_inventory": inventory(),
        }
        selection_arguments = {
            "display": ":18",
            "action": "select",
            "selected_activity": "123456789",
            "target_passed": True,
            "dedup_passed": True,
            "author_cooloff_passed": True,
        }
        frozen_selection, exact_inventory = publisher.build_selection(
            selection_input,
            selection_arguments,
            loaded["preparation"],
        )
        selected_source = source(frozen_selection)
        draft_input = {
            "schema": "linkedin_unit1_private_draft_input_v1",
            "policy_sha256": loaded["preparation"]["policy_sha256"],
            "transaction_sha256": frozen_selection["transaction_sha256"],
            "selection_sha256": frozen_selection["selection_sha256"],
            "selected_notification": {
                "activity": frozen_selection["selected_activity"],
                "age_seconds": frozen_selection["selected_age_seconds"],
                "inventory_sha256": frozen_selection[
                    "notification_inventory_sha256"
                ],
                "notification_text": frozen_selection[
                    "selected_notification_text"
                ],
                "notification_text_sha256": frozen_selection[
                    "selected_notification_text_sha256"
                ],
                "ordinal": frozen_selection["selected_notification_ordinal"],
            },
            "source": selected_source,
        }
        draft = "Specific autonomous comment grounded in the exact post and thread."
        bundle, gate = publisher.build_final_bundle(
            bootstrap=loaded,
            selection=frozen_selection,
            inventory=exact_inventory,
            draft_input=draft_input,
            text=draft,
        )
        if hands_unit1 is not None:
            hands_unit1.validate_private_input(bundle["private_input"])
        require(gate["verdict"] == "PASS", "automatic draft gate did not pass")
        if soma is not None:
            bundle_sha256 = soma._publish_linkedin_unit1_private_bundle(bundle)
            final_path = final_root / "transactions" / seat / f"{correlation}.json"
            require(
                stat.S_IMODE(os.lstat(final_path).st_mode) == 0o400,
                "final bundle is not owner-only 0400",
            )
            require(
                hashlib.sha256(final_path.read_bytes()).hexdigest() == bundle_sha256,
                "final bundle digest is not exact",
            )
            resolved_bundle = soma._resolve_linkedin_unit1_private_bundle(context)
            require(
                resolved_bundle["private_input"] == bundle["private_input"],
                "existing linkedin-unit1 resolver cannot consume the published bundle",
            )
            expect_refusal(
                lambda: soma._publish_linkedin_unit1_private_bundle(bundle),
                "spent final identity was overwritten",
            )
        invalid_verdict = dict(selection_arguments)
        invalid_verdict["dedup_passed"] = False
        expect_refusal(
            lambda: publisher.build_selection(
                selection_input,
                invalid_verdict,
                loaded["preparation"],
            ),
            "failed selection verdict was admitted",
        )
        expect_refusal(
            lambda: publisher.build_final_bundle(
                bootstrap=loaded,
                selection=frozen_selection,
                inventory=exact_inventory,
                draft_input=draft_input,
                text="",
            ),
            "empty draft was admitted",
        )
        expect_refusal(
            lambda: publisher.build_final_bundle(
                bootstrap=loaded,
                selection=frozen_selection,
                inventory=exact_inventory,
                draft_input=draft_input,
                text="x" * 1801,
            ),
            "oversized draft was admitted",
        )
        incomplete_draft = json.loads(json.dumps(draft_input))
        incomplete_draft["source"]["thread"]["read_complete"] = False
        expect_refusal(
            lambda: publisher.build_final_bundle(
                bootstrap=loaded,
                selection=frozen_selection,
                inventory=exact_inventory,
                draft_input=incomplete_draft,
                text=draft,
            ),
            "incomplete thread source was admitted",
        )
        if soma is not None:
            os.chmod(bootstrap_path, 0o600)
            expect_refusal(
                lambda: soma._resolve_linkedin_unit1_prepare_bootstrap(context),
                "writable private bootstrap was admitted",
            )

    print("PASS: autonomous LinkedIn Unit 1 preparation profile is fail-closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
