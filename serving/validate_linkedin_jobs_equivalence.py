#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


BASELINE_SHA = "d5295bd836c70be7a7acf1b41d82c4cb623ba017"
REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_PATH = REPO_ROOT / "serving" / "soma_proxy.py"
JOBS_PROMPT_PATH = REPO_ROOT / "serving" / "TAEY_LINKEDIN_JOBS_SYSTEM.md"
JOBS_RESULT_KEYS = {
    "ok",
    "platform",
    "display",
    "state",
    "failure_code",
    "records_observed",
    "records_written",
    "content_digest",
    "receipt_sha256",
    "turn_lineage_sha256",
}
ENGAGERS_RESULT_KEYS = {*JOBS_RESULT_KEYS, "restore_verified"}


def git_show(path: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{BASELINE_SHA}:{path}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"cannot read equivalence baseline {BASELINE_SHA}:{path}: "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout


@contextmanager
def configured_environment(private_root: Path, hands_root: Path):
    keys = {
        "TAEY_LINKEDIN_JOBS_PYTHON": sys.executable,
        "TAEY_LINKEDIN_JOBS_PRIVATE_ROOT": str(private_root),
        "TAEY_LINKEDIN_JOBS_DISPLAYS": ":18",
        "TAEY_LINKEDIN_JOBS_TIMEOUT_SECS": "1800",
        "TAEYS_HANDS_ROOT": str(hands_root),
    }
    prior = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_proxy(source: str, label: str, private_root: Path, hands_root: Path) -> dict:
    module = types.ModuleType(label)
    module.__file__ = str(PROXY_PATH)
    sys.modules[label] = module
    with configured_environment(private_root, hands_root):
        exec(compile(source, label, "exec"), module.__dict__)
    return module.__dict__


def prepare_private_root(root: Path, seat: str, correlation: str) -> Path:
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    for section in ("transactions", "receipts", "claims"):
        parent = root / section
        parent.mkdir(mode=0o700)
        os.chmod(parent, 0o700)
        seat_parent = parent / seat
        seat_parent.mkdir(mode=0o700)
        os.chmod(seat_parent, 0o700)
    transaction = root / "transactions" / seat / f"{correlation}.json"
    transaction.write_text(
        '{"operation":"capture_selected_job","schema":'
        '"linkedin_jobs_private_input_v1","search_ref":"opaque",'
        '"sink_ref":"opaque"}',
        encoding="utf-8",
    )
    transaction.chmod(0o400)
    return transaction


def lineage_sha256(context: dict) -> str:
    payload = {
        "correlation_id": context["correlation_id"],
        "process_generation": context["process_generation"],
        "requester": context["seat_id"],
        "turn_id": context["turn_id"],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def run_jobs_case(
    namespace: dict,
    root: Path,
    context: dict,
    payload_overrides: dict,
    returncode: int,
) -> tuple[str, list[str], dict]:
    prepare_private_root(root, context["seat_id"], context["correlation_id"])
    captured_command: list[str] = []
    receipt_bytes = b'{"schema":"linkedin_jobs_equivalence_receipt_v1"}'

    def fake_run(command, **kwargs):
        captured_command.extend(str(value) for value in command)
        receipt_path = Path(command[command.index("--receipt-file") + 1])
        receipt_path.write_bytes(receipt_bytes)
        receipt_path.chmod(0o400)
        payload = {
            "ok": True,
            "platform": "linkedin",
            "display": ":18",
            "state": "captured",
            "failure_code": None,
            "records_observed": 1,
            "records_written": 1,
            "content_digest": "a" * 64,
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "turn_lineage_sha256": lineage_sha256(context),
        }
        payload.update(payload_overrides)
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=json.dumps(payload, separators=(",", ":")),
            stderr="",
        )

    token = namespace["_request_context"].set(context)
    try:
        with patch("subprocess.run", fake_run):
            result = namespace["_do_linkedin_jobs"]({"display": ":18"})
    finally:
        namespace["_request_context"].reset(token)
    claim_path = root / "claims" / context["seat_id"] / f"{context['correlation_id']}.json"
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    return result, captured_command, claim


def normalized_command(command: list[str], root: Path, hands_root: Path) -> list[str]:
    return [
        value.replace(str(root), "PRIVATE_ROOT").replace(str(hands_root), "HANDS_ROOT")
        for value in command
    ]


def jobs_contract_cases() -> list[tuple[str, dict, int]]:
    return [
        ("captured", {}, 0),
        (
            "already_captured",
            {"state": "already_captured", "records_written": 0},
            0,
        ),
        (
            "no_selected_job",
            {
                "ok": False,
                "state": "no_selected_job",
                "failure_code": "selected_job_not_exact",
                "records_observed": 0,
                "records_written": 0,
                "content_digest": None,
            },
            1,
        ),
        (
            "postcondition_failed",
            {
                "ok": False,
                "state": "postcondition_failed",
                "failure_code": "postcondition_failed",
                "records_written": 0,
            },
            1,
        ),
        (
            "technical_failure_before_observation",
            {
                "ok": False,
                "state": "technical_failure",
                "failure_code": "pre_observation_failed",
                "records_observed": 0,
                "records_written": 0,
                "content_digest": None,
            },
            1,
        ),
        (
            "sink_write_indeterminate",
            {
                "ok": False,
                "state": "technical_failure",
                "failure_code": "sink_write_indeterminate",
                "records_written": None,
            },
            1,
        ),
        ("invalid_state", {"state": "unexpected"}, 1),
        ("status_disagreement", {}, 1),
        ("invalid_failure_code", {"failure_code": "unexpected"}, 0),
        ("invalid_observed_count", {"records_observed": 2}, 0),
        ("invalid_written_count", {"records_written": 2}, 0),
        ("invalid_content_digest", {"content_digest": "not-a-digest"}, 0),
        ("invalid_lineage", {"turn_lineage_sha256": "b" * 64}, 0),
        ("invalid_receipt_digest", {"receipt_sha256": "c" * 64}, 0),
    ]


def engagers_contract_cases() -> list[tuple[str, dict, int, bool]]:
    base = {
        "ok": True,
        "platform": "linkedin",
        "display": ":18",
        "state": "captured",
        "failure_code": None,
        "records_observed": 1,
        "records_written": 1,
        "content_digest": "a" * 64,
        "receipt_sha256": "b" * 64,
        "turn_lineage_sha256": "c" * 64,
        "restore_verified": True,
    }

    def case(overrides: dict, returncode: int, valid: bool):
        payload = dict(base)
        payload.update(overrides)
        return payload, returncode, valid

    cases = [
        ("captured", *case({}, 0, True)),
        (
            "already_known",
            *case(
                {
                    "state": "already_known",
                    "records_written": 0,
                },
                0,
                True,
            ),
        ),
        (
            "no_new_signal",
            *case(
                {
                    "state": "no_new_signal",
                    "records_observed": 0,
                    "records_written": 0,
                    "content_digest": None,
                },
                0,
                True,
            ),
        ),
        (
            "ambiguous_signal",
            *case(
                {
                    "ok": False,
                    "state": "ambiguous_signal",
                    "failure_code": "ambiguous_signal",
                    "records_observed": 0,
                    "records_written": 0,
                    "content_digest": None,
                    "restore_verified": False,
                },
                1,
                True,
            ),
        ),
        (
            "postcondition_failed",
            *case(
                {
                    "ok": False,
                    "state": "postcondition_failed",
                    "failure_code": "postcondition_failed",
                    "records_written": 0,
                    "restore_verified": False,
                },
                1,
                True,
            ),
        ),
        (
            "sink_write_indeterminate",
            *case(
                {
                    "ok": False,
                    "state": "sink_write_indeterminate",
                    "failure_code": "sink_write_indeterminate",
                    "records_written": None,
                    "restore_verified": False,
                },
                1,
                True,
            ),
        ),
        (
            "technical_failure",
            *case(
                {
                    "ok": False,
                    "state": "technical_failure",
                    "failure_code": "pre_observation_failed",
                    "records_observed": 0,
                    "records_written": 0,
                    "content_digest": None,
                    "restore_verified": False,
                },
                1,
                True,
            ),
        ),
        (
            "ambiguous_signal_with_restore",
            *case(
                {
                    "ok": False,
                    "state": "ambiguous_signal",
                    "failure_code": "ambiguous_signal",
                    "records_observed": 0,
                    "records_written": 0,
                    "content_digest": None,
                },
                1,
                False,
            ),
        ),
        (
            "postcondition_failed_with_restore",
            *case(
                {
                    "ok": False,
                    "state": "postcondition_failed",
                    "failure_code": "postcondition_failed",
                    "records_written": 0,
                },
                1,
                False,
            ),
        ),
        (
            "sink_write_indeterminate_with_restore",
            *case(
                {
                    "ok": False,
                    "state": "sink_write_indeterminate",
                    "failure_code": "sink_write_indeterminate",
                    "records_written": None,
                },
                1,
                False,
            ),
        ),
        ("success_without_restore", *case({"restore_verified": False}, 0, False)),
        (
            "technical_failure_inconsistent_facts",
            *case(
                {
                    "ok": False,
                    "state": "technical_failure",
                    "failure_code": "action_failed",
                    "content_digest": None,
                },
                1,
                False,
            ),
        ),
        ("unknown_failure", *case({"ok": False, "state": "technical_failure", "failure_code": "unknown", "records_written": 0}, 1, False)),
        ("invalid_digest", *case({"content_digest": "invalid"}, 0, False)),
    ]
    return cases


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeHttp:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    async def post(self, *_args, **_kwargs) -> FakeResponse:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("one-shot profile requested a second inference round")
        return FakeResponse(self.payload)


async def one_shot_response(
    namespace: dict,
    *,
    stream: bool,
    tool_name: str,
    profile: str = "linkedin-jobs",
) -> tuple:
    arguments = json.dumps({"display": ":18"}, separators=(",", ":"))
    payload = {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": arguments},
                }],
            },
        }],
        "usage": {"completion_tokens": 1, "prompt_tokens": 1},
    }
    namespace["_http"] = FakeHttp(payload)
    namespace["publish_metrics"] = lambda *_args, **_kwargs: None
    terminal = json.dumps({"ok": True, "state": "captured"}, separators=(",", ":"))

    async def fake_execute(*_args, **_kwargs):
        return terminal

    namespace["execute_tool_call_async"] = fake_execute
    prompt_path = (
        JOBS_PROMPT_PATH
        if profile == "linkedin-jobs"
        else REPO_ROOT / "serving" / "TAEY_LINKEDIN_ENGAGERS_SYSTEM.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    if "_linkedin_jobs_system_prompt" in namespace:
        namespace["_linkedin_jobs_system_prompt"] = prompt
    else:
        namespace["_one_shot_system_prompts"] = {profile: prompt}
    turn = namespace["TurnContext"](
        turn_id="jobs-equivalence-turn",
        seat_id="jobs-equivalence-seat",
        event_id="jobs-equivalence-event",
        correlation_id="jobs-equivalence-correlation",
        tool_profile=profile,
        proxy_namespace="jobs-equivalence-proxy",
        process_generation="1" * 32,
        started_at=0.0,
    )
    try:
        response = await namespace["_chat_completions_for_turn"](
            {
                "stream": stream,
                "messages": [{"role": "user", "content": "Run once on :18."}],
            },
            turn,
            False,
        )
    except namespace["HTTPException"] as exc:
        return ("error", exc.status_code, exc.detail)
    if stream:
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return ("stream", "".join(chunks))
    return ("nonstream", response.status_code, response.body.decode("utf-8"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def direct_jobs_call(namespace: dict, context: dict, arguments) -> str:
    token = namespace["_request_context"].set(context)
    try:
        return namespace["_do_linkedin_jobs"](arguments)
    finally:
        namespace["_request_context"].reset(token)


def main() -> int:
    old_source = git_show("serving/soma_proxy.py")
    new_source = PROXY_PATH.read_text(encoding="utf-8")
    require(
        git_show("serving/TAEY_LINKEDIN_JOBS_SYSTEM.md")
        == JOBS_PROMPT_PATH.read_text(encoding="utf-8"),
        "LinkedIn Jobs prompt bytes changed from the frozen baseline",
    )

    with tempfile.TemporaryDirectory(prefix="linkedin-jobs-equivalence-") as temp:
        temp_root = Path(temp)
        hands_root = temp_root / "hands"
        scripts = hands_root / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "run_linkedin_jobs.py").touch()
        old_boot_root = temp_root / "old-boot"
        new_boot_root = temp_root / "new-boot"
        old_boot_root.mkdir(mode=0o700)
        new_boot_root.mkdir(mode=0o700)
        old = load_proxy(old_source, "jobs_equivalence_old", old_boot_root, hands_root)
        new = load_proxy(new_source, "jobs_equivalence_new", new_boot_root, hands_root)

        require(
            old["_tools_for_profile"]("linkedin-jobs")
            == new["_tools_for_profile"]("linkedin-jobs"),
            "LinkedIn Jobs tool schema/profile changed",
        )
        spec = new["_private_transaction_spec_for_profile"]("linkedin-jobs")
        require(spec is not None, "LinkedIn Jobs registry entry is missing")
        require(spec.tool == "linkedin_jobs", "LinkedIn Jobs tool name changed")
        require(spec.runner_name == "run_linkedin_jobs.py", "LinkedIn Jobs runner changed")
        require(spec.claim_schema == "linkedin_jobs_claim_v1", "LinkedIn Jobs claim schema changed")
        require(spec.expected_result_keys == JOBS_RESULT_KEYS, "LinkedIn Jobs result keys changed")
        require(spec.python_env_name == "TAEY_LINKEDIN_JOBS_PYTHON", "LinkedIn Jobs Python env name changed")
        require(spec.private_root_env_name == "TAEY_LINKEDIN_JOBS_PRIVATE_ROOT", "LinkedIn Jobs private root env name changed")
        require(spec.displays_env_name == "TAEY_LINKEDIN_JOBS_DISPLAYS", "LinkedIn Jobs display env name changed")
        require(spec.timeout_env_name == "TAEY_LINKEDIN_JOBS_TIMEOUT_SECS", "LinkedIn Jobs timeout env name changed")
        require(
            spec.terminal_reason == "the one frozen LinkedIn Jobs invocation has been spent",
            "LinkedIn Jobs terminal reason changed",
        )
        require(spec.python_path == old["LINKEDIN_JOBS_PYTHON"], "LinkedIn Jobs Python env changed")
        require(
            old["LINKEDIN_JOBS_PRIVATE_ROOT"] == str(old_boot_root)
            and spec.private_root == str(new_boot_root),
            "LinkedIn Jobs private root env binding changed",
        )
        require(spec.displays == old["LINKEDIN_JOBS_DISPLAYS"], "LinkedIn Jobs display env changed")
        require(spec.timeout_secs == old["LINKEDIN_JOBS_TIMEOUT_SECS"], "LinkedIn Jobs timeout changed")
        require(spec.deadline_secs == old["LINKEDIN_JOBS_DEADLINE_SECS"], "LinkedIn Jobs deadline changed")

        engager_spec = new["_private_transaction_spec_for_profile"]("linkedin-engagers")
        require(engager_spec is not None, "LinkedIn Engagers registry entry is missing")
        require(engager_spec.tool == "linkedin_engagers", "LinkedIn Engagers tool name is wrong")
        require(engager_spec.runner_name == "run_linkedin_jobs.py", "LinkedIn Engagers runner is wrong")
        require(engager_spec.claim_schema == "linkedin_engagers_claim_v1", "LinkedIn Engagers claim schema is wrong")
        require(engager_spec.expected_result_keys == ENGAGERS_RESULT_KEYS, "LinkedIn Engagers result keys are wrong")
        engager_tools = new["_tools_for_profile"]("linkedin-engagers")
        require(len(engager_tools) == 1, "LinkedIn Engagers profile does not expose exactly one tool")
        require(
            engager_tools[0]["function"]["name"] == "linkedin_engagers"
            and engager_tools[0]["function"]["parameters"].get("required") == ["display"]
            and set(engager_tools[0]["function"]["parameters"].get("properties", {})) == {"display"},
            "LinkedIn Engagers tool accepts more than the display",
        )
        for name, payload, returncode, valid in engagers_contract_cases():
            error = new["_linkedin_engagers_result_error"](payload, returncode)
            require((error is None) is valid, f"LinkedIn Engagers contract case disagrees: {name}")
        registered_specs = new["_PRIVATE_TRANSACTION_TOOL_SPECS"]

        context = {
            "tool_profile": "linkedin-jobs",
            "seat_id": "jobs-equivalence-seat",
            "turn_id": "jobs-equivalence-turn",
            "event_id": "jobs-equivalence-event",
            "correlation_id": "jobs-equivalence-correlation",
            "process_generation": "1" * 32,
            "_tool_profile_state": {"terminal": None},
        }
        for name, case_context, arguments in (
            (
                "wrong_profile",
                {**context, "tool_profile": "full", "_tool_profile_state": {"terminal": None}},
                {"display": ":18"},
            ),
            (
                "invalid_arguments",
                {**context, "_tool_profile_state": {"terminal": None}},
                {"display": ":18", "extra": True},
            ),
        ):
            require(
                direct_jobs_call(old, case_context, arguments)
                == direct_jobs_call(new, case_context, arguments),
                f"Jobs pre-admission error drift in case {name}",
            )
        for index, (name, overrides, returncode) in enumerate(jobs_contract_cases()):
            case_context = dict(context)
            case_context["_tool_profile_state"] = {"terminal": None}
            case_context["turn_id"] = f"jobs-equivalence-turn-{index}"
            case_context["event_id"] = f"jobs-equivalence-event-{index}"
            case_context["correlation_id"] = f"jobs-equivalence-correlation-{index}"
            old_root = temp_root / f"old-{index}"
            new_root = temp_root / f"new-{index}"
            old["LINKEDIN_JOBS_PRIVATE_ROOT"] = str(old_root)
            old_result, old_command, old_claim = run_jobs_case(
                old, old_root, case_context, overrides, returncode
            )
            new_spec = dataclasses.replace(spec, private_root=str(new_root))
            new["_PRIVATE_TRANSACTION_TOOL_SPECS"] = (new_spec,)
            new_result, new_command, new_claim = run_jobs_case(
                new, new_root, case_context, overrides, returncode
            )
            require(old_result == new_result, f"Jobs result/error drift in case {name}")
            require(
                normalized_command(old_command, old_root, hands_root)
                == normalized_command(new_command, new_root, hands_root),
                f"Jobs runner argv drift in case {name}",
            )
            require(old_claim == new_claim, f"Jobs claim drift in case {name}")

        new["_PRIVATE_TRANSACTION_TOOL_SPECS"] = registered_specs

        for stream in (True, False):
            old_success = asyncio.run(
                one_shot_response(old, stream=stream, tool_name="linkedin_jobs")
            )
            new_success = asyncio.run(
                one_shot_response(new, stream=stream, tool_name="linkedin_jobs")
            )
            require(old_success == new_success, f"Jobs one-shot response drift stream={stream}")
            old_error = asyncio.run(
                one_shot_response(old, stream=stream, tool_name="wrong_tool")
            )
            new_error = asyncio.run(
                one_shot_response(new, stream=stream, tool_name="wrong_tool")
            )
            require(old_error == new_error, f"Jobs one-shot error drift stream={stream}")
            engager_success = asyncio.run(
                one_shot_response(
                    new,
                    stream=stream,
                    tool_name="linkedin_engagers",
                    profile="linkedin-engagers",
                )
            )
            require(
                engager_success[0] == ("stream" if stream else "nonstream")
                and "captured" in engager_success[-1],
                f"LinkedIn Engagers one-shot result failed stream={stream}",
            )
            engager_error = asyncio.run(
                one_shot_response(
                    new,
                    stream=stream,
                    tool_name="linkedin_jobs",
                    profile="linkedin-engagers",
                )
            )
            require(
                engager_error == (
                    "error",
                    502,
                    {
                        "error": "linkedin_engagers_one_shot_tool_call_required",
                        "turn_id": "jobs-equivalence-turn",
                    },
                ),
                f"LinkedIn Engagers one-shot refusal failed stream={stream}",
            )

    print(
        "PASS linkedin-jobs equivalence: baseline="
        f"{BASELINE_SHA} cases={len(jobs_contract_cases())} streaming=PASS nonstream=PASS; "
        f"linkedin-engagers-contract-cases={len(engagers_contract_cases())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
