#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
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


def validate_private_launcher(launcher: Path) -> None:
    source = launcher.read_text(encoding="utf-8")
    umask_index = source.index("umask 077")
    require(
        umask_index < source.index("mkdir -m 700")
        and umask_index < source.index(': > "$headers_path"')
        and umask_index < source.index(': > "$response_path"'),
        "private launcher does not establish umask before creation",
    )
    require(
        "set -o noclobber" in source
        and "--dump-header \"$headers_path\"" in source
        and "--output \"$response_path\"" in source,
        "private launcher lost collision-safe capture outputs",
    )

    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        artifact_root = temporary_root / "artifacts"
        fake_bin = temporary_root / "bin"
        artifact_root.mkdir(mode=0o700)
        fake_bin.mkdir(mode=0o700)
        artifact_root.chmod(0o700)
        fake_bin.chmod(0o700)
        curl_marker = temporary_root / "curl-invoked"
        fake_curl = fake_bin / "curl"
        fake_curl.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import stat
import sys

arguments = sys.argv[1:]

def option(name):
    index = arguments.index(name)
    return arguments[index + 1]

headers = Path(option("--dump-header"))
response = Path(option("--output"))
for path in (headers, response):
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SystemExit(91)
    if metadata.st_uid != os.geteuid() or metadata.st_size != 0:
        raise SystemExit(92)
body = json.loads(option("--data-binary"))
if body != {
    "model": "served-model",
    "stream": False,
    "chat_template_kwargs": {"enable_thinking": False},
    "messages": [{
        "role": "user",
        "content": (
            "Prepare the frozen LinkedIn Unit 1 transaction on display :18. "
            "Continue only through the injected profile until "
            "final_bundle_published or the first failure."
        ),
    }],
}:
    raise SystemExit(93)
headers_by_name = {
    value.split(":", 1)[0]: value.split(":", 1)[1].strip()
    for index, value in enumerate(arguments)
    if index > 0 and arguments[index - 1] == "-H"
}
if headers_by_name != {
    "Content-Type": "application/json",
    "X-Taey-Seat-Id": "seat-1",
    "X-Taey-Event-Id": "event-1",
    "X-Taey-Correlation-Id": os.environ.get(
        "FAKE_EXPECTED_CORRELATION_ID", "correlation-1"
    ),
    "X-Taey-Tool-Profile": "linkedin-unit1-prepare",
}:
    raise SystemExit(94)
if arguments[-1] != "http://127.0.0.1:8765/v1/chat/completions":
    raise SystemExit(95)
for flag in ("--fail-with-body", "--silent", "--show-error"):
    if arguments.count(flag) != 1:
        raise SystemExit(96)
if arguments.count("--max-time") != 1 or option("--max-time") != "2400":
    raise SystemExit(97)
Path(os.environ["FAKE_CURL_MARKER"]).write_text("invoked\\n", encoding="utf-8")
if os.environ.get("FAKE_CURL_EXIT") == "28":
    raise SystemExit(28)
headers.write_text("HTTP/1.1 200 OK\\n", encoding="utf-8")
response.write_text('{"ok":true}\\n', encoding="utf-8")
""",
            encoding="utf-8",
        )
        fake_curl.chmod(0o700)
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "FAKE_CURL_MARKER": str(curl_marker),
            "TAEY_LINKEDIN_UNIT1_ARTIFACT_ROOT": str(artifact_root),
            "TAEY_LINKEDIN_UNIT1_SEAT_ID": "seat-1",
            "TAEY_LINKEDIN_UNIT1_EVENT_ID": "event-1",
            "TAEY_LINKEDIN_UNIT1_CORRELATION_ID": "correlation-1",
            "TAEY_LINKEDIN_UNIT1_MODEL": "served-model",
            "TAEY_LINKEDIN_UNIT1_DISPLAY": ":18",
            "TAEY_PROXY_URL": "http://127.0.0.1:8765/v1/chat/completions",
        }
        command = [
            "bash",
            "-c",
            'umask 000; exec "$1"',
            "launcher-validator",
            str(launcher),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            env=environment,
            text=True,
        )
        require(
            completed.returncode == 0,
            f"private launcher failed synthetic launch: {completed.stderr}",
        )
        run_dir = artifact_root / "correlation-1"
        require(
            stat.S_IMODE(os.lstat(run_dir).st_mode) == 0o700,
            "private launcher run directory is not 0700",
        )
        for name in ("headers.txt", "response.json"):
            path = run_dir / name
            metadata = os.lstat(path)
            require(
                stat.S_ISREG(metadata.st_mode)
                and stat.S_IMODE(metadata.st_mode) == 0o600,
                f"private launcher {name} is not 0600",
            )
        require(curl_marker.is_file(), "synthetic curl was not invoked")

        curl_marker.unlink()
        timeout_environment = {
            **environment,
            "FAKE_EXPECTED_CORRELATION_ID": "correlation-timeout",
            "FAKE_CURL_EXIT": "28",
            "TAEY_LINKEDIN_UNIT1_CORRELATION_ID": "correlation-timeout",
        }
        timed_out = subprocess.run(
            command,
            capture_output=True,
            env=timeout_environment,
            text=True,
        )
        timeout_dir = artifact_root / "correlation-timeout"
        require(
            timed_out.returncode == 28
            and timed_out.stdout == ""
            and curl_marker.is_file(),
            "private launcher did not propagate the fixed curl timeout: "
            f"rc={timed_out.returncode} stdout={timed_out.stdout!r} "
            f"marker={curl_marker.is_file()}",
        )
        require(
            stat.S_IMODE(os.lstat(timeout_dir).st_mode) == 0o700,
            "private launcher timeout directory is not 0700",
        )
        for name in ("headers.txt", "response.json"):
            path = timeout_dir / name
            metadata = os.lstat(path)
            require(
                stat.S_ISREG(metadata.st_mode)
                and stat.S_IMODE(metadata.st_mode) == 0o600
                and metadata.st_size == 0,
                f"private launcher timeout {name} is not empty 0600",
            )

        curl_marker.unlink()
        collision = subprocess.run(
            command,
            capture_output=True,
            env=environment,
            text=True,
        )
        require(
            collision.returncode != 0
            and "artifact directory already exists" in collision.stderr
            and not curl_marker.exists(),
            "private launcher did not refuse an existing identity before curl",
        )

        symlink_environment = {
            **environment,
            "TAEY_LINKEDIN_UNIT1_CORRELATION_ID": "correlation-link",
        }
        (artifact_root / "correlation-link").symlink_to(run_dir)
        symlink = subprocess.run(
            command,
            capture_output=True,
            env=symlink_environment,
            text=True,
        )
        require(
            symlink.returncode != 0
            and "artifact directory already exists" in symlink.stderr
            and not curl_marker.exists(),
            "private launcher did not refuse a symlinked identity before curl",
        )


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
    artifact["decision_inventory_sha256"] = publisher.canonical_sha256({
        "schema": publisher.NOTIFICATION_DECISION_INVENTORY_SCHEMA,
        "candidates": [{
            "activity": artifact["actionable_links"][0]["activity"],
            "notification_text_sha256": artifact["rows"][0][
                "notification_text_sha256"
            ],
            "uri_sha256": artifact["actionable_links"][0]["uri_sha256"],
        }],
    })
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
    hands_prepare = None
    soma = None
    if args.hands_root:
        hands_root = Path(args.hands_root).resolve(strict=True)
        sys.path.insert(0, str(hands_root))
        hands_unit1 = importlib.import_module(
            "consultation_v2.platforms.linkedin.unit1"
        )
        hands_prepare = importlib.import_module(
            "consultation_v2.platforms.linkedin.unit1_prepare"
        )
        soma = importlib.import_module("soma_proxy")
        manual_source = source_function(
            hands_root / "consultation_v2/platforms/linkedin/manual.py",
            "stable_scroll_post_action_observation",
        )
        unit1_accept_source = source_function(
            hands_root / "consultation_v2/platforms/linkedin/unit1.py",
            "accept_unit1_step",
        )
        prepare_accept_source = source_function(
            hands_root / "consultation_v2/platforms/linkedin/unit1_prepare.py",
            "accept_preparation_step",
        )
        for generic_field in (
            "scroll_context_intersects_viewport",
            "scroll_target_exact",
            "live_extent_in_viewport",
            "available_below_px",
            "min_downward_clearance_px",
        ):
            require(
                generic_field in manual_source,
                f"Hands scroll barrier lost generic field {generic_field}",
            )
        for accept_source in (unit1_accept_source, prepare_accept_source):
            require(
                "postcondition.get('available_below_px')" in accept_source
                and "postcondition.get('thread_opener_available_below_px')"
                in accept_source,
                "Hands acceptance lost generic/provider clearance equivalence",
            )

    proxy = SERVING_ROOT / "soma_proxy.py"
    drive = SERVING_ROOT / "ui_drive.py"
    launcher = SERVING_ROOT / "launch_linkedin_unit1_prepare.sh"
    require(launcher.is_file(), "private preparation launcher is missing")
    require(
        stat.S_IMODE(os.lstat(launcher).st_mode) == 0o755,
        "private preparation launcher is not executable 0755",
    )
    validate_private_launcher(launcher)
    prompt = (SERVING_ROOT / "TAEY_LINKEDIN_UNIT1_PREPARE_SYSTEM.md").read_text(
        encoding="utf-8"
    )
    profile = ast.literal_eval(assignment(proxy, "_LINKEDIN_UNIT1_PREPARE_TOOL_PROFILE"))
    require(profile == "linkedin-unit1-prepare", "preparation profile name drifted")
    transport_timeout = ast.literal_eval(
        assignment(proxy, "_LINKEDIN_UNIT1_PREPARE_TRANSPORT_TIMEOUT_SECS")
    )
    require(
        transport_timeout == 300,
        "preparation transport timeout no longer covers the bounded Hands barrier",
    )
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
    parameters = tool["parameters"]
    properties = parameters.get("properties", {})
    require(
        parameters.get("type") == "object"
        and parameters.get("additionalProperties") is False
        and parameters.get("required") == ["display", "action"],
        "preparation root grammar is not a closed typed object",
    )
    require(
        properties.get("action", {}).get("enum")
        == ["observe", "operate", "select", "exclude", "draft"],
        "preparation root action enum drifted",
    )
    variants = parameters.get("oneOf")
    require(
        isinstance(variants, list) and len(variants) == 5,
        "preparation action grammar is not five exact variants",
    )
    variants_by_action = {
        variant["properties"]["action"]["const"]: variant
        for variant in variants
    }
    require(
        list(variants_by_action)
        == ["observe", "operate", "select", "exclude", "draft"],
        "preparation action grammar drifted",
    )
    expected_fields = {
        "observe": set(),
        "operate": {"card_sha256"},
        "select": {
            "selected_notification_ordinal", "target_passed", "dedup_passed",
            "author_cooloff_passed",
        },
        "exclude": {"excluded_candidates"},
        "draft": {"text"},
    }
    expected_max_properties = {
        "observe": 2,
        "operate": 3,
        "select": 6,
        "exclude": 3,
        "draft": 3,
    }
    require(
        all(
            set(variant.get("required", [])) == expected_fields[action]
            and variant.get("maxProperties") == expected_max_properties[action]
            and variant.get("properties", {}).get("action") == {"const": action}
            for action, variant in variants_by_action.items()
        ),
        "preparation action variants do not forbid cross-action fields",
    )
    require(
        all(
            properties[field].get("type") == "boolean"
            for field in (
                "target_passed", "dedup_passed", "author_cooloff_passed",
            )
        ),
        "select verdict types are not visible at the root tool boundary",
    )
    require(
        properties["selected_notification_ordinal"].get("type") == "integer"
        and properties["selected_notification_ordinal"].get("minimum") == 1,
        "select does not use one positive notification ordinal",
    )
    select_properties = variants_by_action["select"]["properties"]
    require(
        all(
            select_properties[field] == {"const": True}
            for field in (
                "target_passed", "dedup_passed", "author_cooloff_passed",
            )
        ),
        "select grammar permits a false qualifying verdict",
    )
    exclusion_schema = properties["excluded_candidates"]
    require(
        exclusion_schema["type"] == "array"
        and exclusion_schema["items"]["additionalProperties"] is False
        and exclusion_schema["items"]["required"]
        == ["notification_ordinal", "reason_codes"]
        and exclusion_schema["items"]["properties"]["notification_ordinal"]
        == {"type": "integer", "minimum": 1}
        and set(exclusion_schema["items"]["properties"])
        == {"notification_ordinal", "reason_codes"}
        and exclusion_schema["items"]["properties"]["reason_codes"]["items"][
            "enum"
        ]
        == sorted(publisher.EXCLUSION_REASON_CODES),
        "private exclusion evidence grammar drifted",
    )
    for forbidden in (
        "selector", "coordinate", "url", "path", "element",
        "selected_activity",
    ):
        require(
            forbidden not in properties,
            f"model-facing preparation tool exposes {forbidden}",
        )
    lowered_prompt = prompt.lower()
    normalized_prompt = " ".join(prompt.split()).lower()
    require(
        "there is no human review or approval step" in lowered_prompt,
        "autonomous no-human-approval boundary is missing",
    )
    require(
        'action="exclude"' in prompt
        and "continuation_available" in prompt
        and "qualifying selection always takes priority" in lowered_prompt,
        "candidate-first continuation instructions are incomplete",
    )
    require(
        "before excluding any otherwise eligible candidate" in normalized_prompt
        and "both valid comment shapes" in normalized_prompt
        and "a specific additive insight or different perspective"
        in normalized_prompt
        and "on that candidate's topic" in normalized_prompt
        and "a genuine question whose answer is unknown from the candidate's context"
        in normalized_prompt
        and "non-obvious" in normalized_prompt
        and "answerable in one or two sentences" in normalized_prompt
        and "consistent with the forum" in normalized_prompt,
        "both valid candidate shapes are not required before exclusion",
    )
    require(
        "`notifications_exhausted_without_eligible_target`" in prompt
        and "exact non-success disposition" in normalized_prompt
        and "does not complete the hourly comment floor" in normalized_prompt
        and "owning loop must trigger deterministic in-cycle" in normalized_prompt
        and "source widening" in normalized_prompt
        and "never lower the safety or quality rules" in normalized_prompt
        and all(
            forbidden in normalized_prompt
            for forbidden in (
                "stale", "promotional", "duplicate",
                "author-cooloff-conflicting", "irrelevant", "low-value",
            )
        ),
        "notification exhaustion can still escape as successful or low-quality",
    )
    require(
        "never carry a `card_sha256`" in prompt
        and "on any `ok=false`" in lowered_prompt,
        "cross-action and terminal-call instructions are incomplete",
    )
    require(
        "review before" not in lowered_prompt and "await approval" not in lowered_prompt,
        "human approval language entered the preparation prompt",
    )
    operate_source = source_function(drive, "_linkedin_unit1_prepare_operate")
    compile_source = source_function(drive, "_linkedin_unit1_prepare_compile")
    primitive_timeout = ast.literal_eval(
        assignment(drive, "_LINKEDIN_UNIT1_PREPARE_PRIMITIVE_TIMEOUT_SECS")
    )
    require(
        primitive_timeout == 240
        and transport_timeout - primitive_timeout == 60
        and "timeout=_LINKEDIN_UNIT1_PREPARE_PRIMITIVE_TIMEOUT_SECS"
        in operate_source,
        "preparation timeout stack lost its exact bounded margin",
    )
    require(
        '"kind": "phase_receipt"' in compile_source
        and "PREPARATION_RECEIPT_SCHEMA" in compile_source,
        "fresh exact route proof is not carried through the compile transport",
    )
    for required in (
        "if receipts:",
        'getattr(manual, "stable_initial_preparation_observation", None)',
        "time.monotonic() + LOCK_TTL_DEFAULT",
        '"kind": "initial_observation_timeout"',
        '"initial_observation_barrier": initial_observation_barrier',
        'initial_observation_barrier.get("compile_authorized") is not True',
        'initial_observation_barrier.get("next_mutation_authorized")',
        "preparation_compile_observation_contract()",
        "invalidate_preparation_observation_cache()",
        "preparation_compiled_authority_sha256(observed_result)",
        '"projection": "revision_stripped_compiled_authority"',
        '"kind": "compile_observation_timeout"',
        '"compile_observation_barrier": compile_observation_barrier',
        "except (RuntimeError, ValueError) as exc:",
        "result = compile_preparation_step(",
    ):
        require(required in compile_source, f"initial barrier wiring lost {required}")
    require(
        compile_source.count("_revenue_snapshot(deps)") == 1
        and compile_source.index("stable_observation(")
        < compile_source.index(
            "if not receipts:\n            result = compile_preparation_step("
        ),
        "initial compile does not consume only the barrier-proven snapshot",
    )
    for token in (
        "accept_preparation_step(",
        "_revenue_snapshot(deps)",
        'stored_card.get("phase") == "notifications_navigation"',
        "lease = _lease_context()",
        "lease_receipt = _guard_action(",
        "stable_initial(",
        "activate_notifications(snapshot)",
        "stable_observation(",
        '"lease": lease_receipt',
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
        "timeout=_LINKEDIN_UNIT1_PREPARE_TRANSPORT_TIMEOUT_SECS"
        in handler_source,
        "preparation transport does not consume its exact outer timeout",
    )
    for required in (
        "def initial_barrier_exact(",
        'result["kind"] == "initial_observation_timeout"',
        'initial_barrier_exact(barrier, "TIMEOUT")',
        'initial_barrier_exact(initial_barrier, "PASS")',
        'expected_result_keys.add("initial_observation_barrier")',
        'result["kind"] == "compile_observation_timeout"',
        'compile_barrier_exact(barrier, "TIMEOUT")',
        'compile_barrier_exact(compile_barrier, "PASS")',
        '"compile_observation_barrier"',
        'card.get("phase") != "notifications_navigation"',
        '{"initial_observation_barrier": initial_barrier}',
    ):
        require(required in handler_source, f"initial barrier consumer lost {required}")
    require(
        'sequence["receipts"].append(initial' not in handler_source
        and "_linkedin_unit1_prepare_route_proof(\n                    result"
        not in handler_source.split(
            'if result["kind"] == "initial_observation_timeout":', 1
        )[1].split('if result["kind"] == "action_card":', 1)[0],
        "initial barrier entered the Hands receipt chain",
    )
    handler_tree = ast.parse(handler_source)
    barrier_functions = [
        node
        for node in ast.walk(handler_tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "initial_barrier_exact"
    ]
    require(len(barrier_functions) == 1, "initial barrier consumer is not exact")
    barrier_namespace = {"re": re}
    exec(
        compile(
            ast.fix_missing_locations(
                ast.Module(body=barrier_functions, type_ignores=[])
            ),
            str(proxy),
            "exec",
        ),
        barrier_namespace,
    )
    compile_barrier_functions = [
        node
        for node in ast.walk(handler_tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "compile_barrier_exact"
    ]
    require(
        len(compile_barrier_functions) == 1,
        "compile barrier consumer is not exact",
    )
    compile_barrier_namespace = {"re": re}
    exec(
        compile(
            ast.fix_missing_locations(
                ast.Module(body=compile_barrier_functions, type_ignores=[])
            ),
            "<compile_barrier_exact>",
            "exec",
        ),
        compile_barrier_namespace,
    )
    compile_barrier_exact = compile_barrier_namespace["compile_barrier_exact"]
    authority = "a" * 64
    stale_then_stable = {
        "result": "PASS",
        "compile_authorized": True,
        "next_mutation_authorized": False,
        "projection": "revision_stripped_compiled_authority",
        "refresh_policy": "invalidate_reacquire",
        "stable_cycles_required": 2,
        "stable_cycles_observed": 2,
        "semantic_authority_sha256": authority,
        "samples": [
            {
                "sample": 1,
                "elapsed_ms": 1,
                "snapshot_revision": None,
                "semantic_authority_sha256": None,
                "matched_previous_authority": False,
                "firefox_cache_invalidation": "recursive_success",
                "error": "UiDriveError: detached AT-SPI node",
            },
            {
                "sample": 2,
                "elapsed_ms": 202,
                "snapshot_revision": "1" * 64,
                "semantic_authority_sha256": authority,
                "matched_previous_authority": False,
                "firefox_cache_invalidation": "recursive_success",
                "error": None,
            },
            {
                "sample": 3,
                "elapsed_ms": 403,
                "snapshot_revision": "2" * 64,
                "semantic_authority_sha256": authority,
                "matched_previous_authority": True,
                "firefox_cache_invalidation": "recursive_success",
                "error": None,
            },
        ],
    }
    require(
        compile_barrier_exact(stale_then_stable, "PASS"),
        "stale-first-read then two exact semantic samples did not pass",
    )
    forged_refresh_policy = json.loads(json.dumps(stale_then_stable))
    forged_refresh_policy["samples"][0]["firefox_cache_invalidation"] = None
    require(
        not compile_barrier_exact(forged_refresh_policy, "PASS"),
        "PASS accepted refresh policy not derived from every sample receipt",
    )
    changed_authority = json.loads(json.dumps(stale_then_stable))
    changed_authority["samples"][-1]["semantic_authority_sha256"] = "b" * 64
    require(
        not compile_barrier_exact(changed_authority, "PASS"),
        "two different semantic compile authorities passed",
    )
    timeout_sample = json.loads(json.dumps(stale_then_stable["samples"][0]))
    timeout_sample["firefox_cache_invalidation"] = None
    timeout_barrier = {
        **stale_then_stable,
        "result": "TIMEOUT",
        "compile_authorized": False,
        "refresh_policy": "invalidate_reacquire_incomplete",
        "stable_cycles_observed": 0,
        "semantic_authority_sha256": None,
        "samples": [timeout_sample],
    }
    require(
        compile_barrier_exact(timeout_barrier, "TIMEOUT"),
        "read-only compile timeout did not retain zero authority",
    )
    initial_barrier_exact = barrier_namespace["initial_barrier_exact"]
    timeout_sample = {
        "sample": 1,
        "elapsed_ms": 10000,
        "observed_url": None,
        "notifications_target_match_count": 0,
        "augmented_match_count": 0,
        "declared_method": None,
        "allowed_now": None,
        "target_state_digest": None,
        "exact": False,
        "firefox_cache_invalidation": "recursive_success",
    }
    timeout_barrier = {
        "result": "TIMEOUT",
        "compile_authorized": False,
        "next_mutation_authorized": False,
        "projection": "exact_notifications_navigation",
        "refresh_policy": "invalidate_reacquire",
        "stable_cycles_required": 2,
        "stable_cycles_observed": 0,
        "samples": [timeout_sample],
    }
    require(
        initial_barrier_exact(timeout_barrier, "TIMEOUT"),
        "a truthful absent-document URL timeout receipt is refused",
    )
    require(
        not initial_barrier_exact(
            {
                **timeout_barrier,
                "samples": [{**timeout_sample, "observed_url": 1}],
            },
            "TIMEOUT",
        ),
        "a malformed timeout URL is accepted",
    )
    require(
        not initial_barrier_exact(
            {
                **timeout_barrier,
                "samples": [{
                    **timeout_sample,
                    "firefox_cache_invalidation": "failed",
                }],
            },
            "TIMEOUT",
        ),
        "a failed initial Firefox invalidation is accepted",
    )
    require(
        'sequence["published"] = published' in handler_source
        and "_publish_linkedin_unit1_private_bundle(" in handler_source,
        "publisher does not terminalize exactly after publication",
    )
    require(
        'result["kind"] == "phase_receipt"' in handler_source
        and 'sequence["receipts"].append(receipt)' in handler_source
        and 'return continue_with_observe({' in handler_source
        and '"kind": "phase_receipt"' in handler_source,
        "route proof does not preserve the receipt and chained-observe boundary",
    )
    require(
        "frozen_exclusions = build_exclusions(" in handler_source
        and '"excluded_candidates": []' in handler_source
        and '"kind": "mechanical_empty_inventory"' in handler_source
        and 'not actionable_links' in handler_source
        and '{"action": "select", "alternative_action": "exclude"}'
        in handler_source
        and 'if result["phase"] == "notifications_continuation":' in handler_source
        and 'sequence.pop("selection", None)' in handler_source
        and 'sequence.pop("inventory", None)' in handler_source,
        "accepted continuation does not clear its exact private exclusions",
    )
    require(
        "model_input = selection_decision_input(selection_input)"
        in handler_source
        and '"input": readiness_result["input"]' in handler_source
        and '"input": model_input' in handler_source,
        "full server readiness and slim model decision input are not separated",
    )
    require(
        'def continue_with_observe(' in handler_source
        and 'def refuse_with_evidence(' in handler_source
        and '"validated_transitions"' in handler_source
        and 'invalid success state' in handler_source
        and 'invalid terminal state' in handler_source
        and 'set(first_failure)' in handler_source
        and 'payload.get("error") != first_failure["reason"]' in handler_source
        and '"kind": "private_selection_frozen"' in handler_source
        and '"kind": "private_exclusions_frozen"' in handler_source
        and '"kind": "operated_step"' in handler_source,
        "deterministic preparation transitions still require model-only observe rounds",
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
    for private_action in ("select", "exclude", "draft"):
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
        if soma is not None:
            transaction_sha256 = publisher.preparation_transaction_sha256(
                loaded["preparation"]
            )
            route_receipt = {
                "schema": "linkedin_unit1_preparation_receipt_v1",
                "transaction_sha256": transaction_sha256,
                "sequence": 1,
                "phase": "notifications_navigation",
                "previous_receipt_sha256": None,
                "card_sha256": "1" * 64,
                "snapshot_revision": "2" * 64,
                "element_sha256": "3" * 64,
                "method": "observe",
                "effect_class": "read_only",
                "postcondition_sha256": "4" * 64,
                "postcondition_passed": True,
                "fresh_observation_required": True,
                "next_step_authorized": True,
            }
            route_receipt["receipt_sha256"] = publisher.canonical_sha256(
                route_receipt
            )
            route_result = {
                "schema": "taey_linkedin_unit1_preparation_compiled_step_v1",
                "kind": "phase_receipt",
                "receipt": route_receipt,
            }
            require(
                soma._linkedin_unit1_prepare_route_proof(
                    route_result,
                    transaction_sha256,
                    [],
                )
                == route_receipt,
                "exact route proof was not admitted",
            )
            expect_refusal(
                lambda: soma._linkedin_unit1_prepare_route_proof(
                    route_result,
                    transaction_sha256,
                    [route_receipt],
                ),
                "route proof was admitted after the initial receipt",
            )
            mutation_receipt = dict(route_receipt)
            mutation_receipt["method"] = "activate"
            mutation_receipt["receipt_sha256"] = publisher.canonical_sha256(
                {key: value for key, value in mutation_receipt.items()
                 if key != "receipt_sha256"}
            )
            expect_refusal(
                lambda: soma._linkedin_unit1_prepare_route_proof(
                    {**route_result, "receipt": mutation_receipt},
                    transaction_sha256,
                    [],
                ),
                "a mutation receipt was admitted as read-only route proof",
            )
        selection_input = {
            "schema": "linkedin_unit1_private_selection_input_v1",
            "policy_sha256": loaded["preparation"]["policy_sha256"],
            "transaction_sha256": publisher.preparation_transaction_sha256(
                loaded["preparation"]
            ),
            "notification_inventory": inventory(),
            "continuation_available": True,
        }
        artifact = selection_input["notification_inventory"]
        row = artifact["rows"][0]
        link = artifact["actionable_links"][0]
        selection_input["decision_input"] = {
            "schema": publisher.PRIVATE_SELECTION_DECISION_SCHEMA,
            "policy_sha256": selection_input["policy_sha256"],
            "transaction_sha256": selection_input["transaction_sha256"],
            "continuation_available": True,
            "decision_inventory_sha256": artifact[
                "decision_inventory_sha256"
            ],
            "inventory_sha256": artifact["inventory_sha256"],
            "mounted_article_count": artifact["mounted_article_count"],
            "actionable_candidates": [{
                "activity": link["activity"],
                "notification_text": row["notification_text"],
                "notification_text_sha256": row[
                    "notification_text_sha256"
                ],
                "age_seconds": row["age_seconds"],
                "age_token": row["age_token"],
                "ordinal": link["ordinal"],
                "element": link["element"],
                "element_sha256": link["element_sha256"],
                "uri": link["uri"],
                "uri_sha256": link["uri_sha256"],
            }],
        }
        require(
            publisher.selection_decision_input(selection_input)
            == selection_input["decision_input"],
            "exact actionable decision input did not bind the full inventory",
        )
        require(
            "notification_inventory" not in selection_input["decision_input"]
            and "rows" not in selection_input["decision_input"]
            and len(selection_input["decision_input"]["actionable_candidates"])
            == len(artifact["actionable_links"]),
            "model decision input retained non-decision inventory rows",
        )
        tampered_decision = json.loads(json.dumps(selection_input))
        tampered_decision["decision_input"]["actionable_candidates"][0][
            "notification_text"
        ] += " changed"
        expect_refusal(
            lambda: publisher.selection_decision_input(tampered_decision),
            "changed model decision text retained full-inventory authority",
        )
        selection_arguments = {
            "display": ":18",
            "action": "select",
            "selected_notification_ordinal": 1,
            "target_passed": True,
            "dedup_passed": True,
            "author_cooloff_passed": True,
        }
        frozen_selection, exact_inventory = publisher.build_selection(
            selection_input,
            selection_arguments,
            loaded["preparation"],
        )
        require(
            frozen_selection["selected_activity"] == link["activity"]
            and frozen_selection["selected_notification_ordinal"]
            == link["ordinal"],
            "server did not resolve selected ordinal to the exact activity",
        )
        for invalid_ordinal in (True, 2):
            expect_refusal(
                lambda invalid_ordinal=invalid_ordinal: publisher.build_selection(
                    selection_input,
                    {
                        **selection_arguments,
                        "selected_notification_ordinal": invalid_ordinal,
                    },
                    loaded["preparation"],
                ),
                "invalid selected ordinal retained activity authority",
            )
        legacy_selection = dict(selection_arguments)
        legacy_selection.pop("selected_notification_ordinal")
        legacy_selection["selected_activity"] = link["activity"]
        expect_refusal(
            lambda: publisher.build_selection(
                selection_input,
                legacy_selection,
                loaded["preparation"],
            ),
            "model-supplied activity retained selection authority",
        )
        exclusion_arguments = {
            "display": ":18",
            "action": "exclude",
            "excluded_candidates": [{
                "notification_ordinal": 1,
                "reason_codes": ["off_target"],
            }],
        }
        frozen_exclusions = publisher.build_exclusions(
            selection_input,
            exclusion_arguments,
            loaded["preparation"],
        )
        require(
            frozen_exclusions["schema"]
            == publisher.NOTIFICATION_EXCLUSIONS_SCHEMA
            and frozen_exclusions["decision_inventory_sha256"]
            == exact_inventory["decision_inventory_sha256"]
            and frozen_exclusions["notification_inventory_sha256"]
            == exact_inventory["inventory_sha256"]
            and frozen_exclusions["exclusions_sha256"]
            == publisher.canonical_sha256({
                key: value
                for key, value in frozen_exclusions.items()
                if key != "exclusions_sha256"
            }),
            "complete exact exclusions were not frozen",
        )
        require(
            frozen_exclusions["excluded_candidates"] == [{
                "activity": link["activity"],
                "reason_codes": ["off_target"],
            }],
            "server did not resolve exclusion ordinal to the exact activity",
        )
        for invalid_ordinal in (True, 2):
            invalid_exclusion = json.loads(json.dumps(exclusion_arguments))
            invalid_exclusion["excluded_candidates"][0][
                "notification_ordinal"
            ] = invalid_ordinal
            expect_refusal(
                lambda invalid_exclusion=invalid_exclusion: (
                    publisher.build_exclusions(
                        selection_input,
                        invalid_exclusion,
                        loaded["preparation"],
                    )
                ),
                "invalid exclusion ordinal retained activity authority",
            )
        legacy_exclusion = json.loads(json.dumps(exclusion_arguments))
        legacy_exclusion["excluded_candidates"][0] = {
            "activity": link["activity"],
            "reason_codes": ["off_target"],
        }
        expect_refusal(
            lambda: publisher.build_exclusions(
                selection_input,
                legacy_exclusion,
                loaded["preparation"],
            ),
            "model-supplied activity retained exclusion authority",
        )
        empty_inventory = json.loads(json.dumps(selection_input))
        empty_inventory["notification_inventory"]["rows"][0][
            "actionable"
        ] = False
        empty_inventory["notification_inventory"]["actionable_links"] = []
        empty_inventory["notification_inventory"][
            "decision_inventory_sha256"
        ] = publisher.canonical_sha256({
            "schema": publisher.NOTIFICATION_DECISION_INVENTORY_SCHEMA,
            "candidates": [],
        })
        empty_inventory["notification_inventory"]["inventory_sha256"] = (
            publisher.canonical_sha256({
                key: value
                for key, value in empty_inventory[
                    "notification_inventory"
                ].items()
                if key != "inventory_sha256"
            })
        )
        automatic_exclusions = publisher.build_exclusions(
            empty_inventory,
            {
                "display": ":18",
                "action": "exclude",
                "excluded_candidates": [],
            },
            loaded["preparation"],
        )
        require(
            automatic_exclusions["excluded_candidates"] == []
            and automatic_exclusions["notification_inventory_sha256"]
            == empty_inventory["notification_inventory"]["inventory_sha256"],
            "exact empty actionable inventory did not freeze empty exclusions",
        )
        if hands_prepare is not None:
            hands_prepare.validate_preparation_envelope({
                **loaded["preparation"],
                "selection": frozen_exclusions,
            })
        partial_exclusions = {
            **exclusion_arguments,
            "excluded_candidates": [],
        }
        expect_refusal(
            lambda: publisher.build_exclusions(
                selection_input,
                partial_exclusions,
                loaded["preparation"],
            ),
            "partial exclusion evidence was admitted",
        )
        duplicate_exclusions = {
            **exclusion_arguments,
            "excluded_candidates": [
                *exclusion_arguments["excluded_candidates"],
                *exclusion_arguments["excluded_candidates"],
            ],
        }
        expect_refusal(
            lambda: publisher.build_exclusions(
                selection_input,
                duplicate_exclusions,
                loaded["preparation"],
            ),
            "duplicate exclusion evidence was admitted",
        )
        unknown_reason = json.loads(json.dumps(exclusion_arguments))
        unknown_reason["excluded_candidates"][0]["reason_codes"] = ["unknown"]
        expect_refusal(
            lambda: publisher.build_exclusions(
                selection_input,
                unknown_reason,
                loaded["preparation"],
            ),
            "unknown exclusion reason was admitted",
        )
        no_continuation = {**selection_input, "continuation_available": False}
        expect_refusal(
            lambda: publisher.build_exclusions(
                no_continuation,
                exclusion_arguments,
                loaded["preparation"],
            ),
            "exclusions authorized an absent continuation",
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
