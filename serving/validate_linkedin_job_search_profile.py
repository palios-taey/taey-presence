#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import types


REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_PATH = REPO_ROOT / "serving" / "soma_proxy.py"
PROMPT_PATH = REPO_ROOT / "serving" / "TAEY_LINKEDIN_JOB_SEARCH_SYSTEM.md"
EXPECTED_RESULT_KEYS = {
    "ok",
    "platform",
    "display",
    "state",
    "failure_code",
    "batches_observed",
    "batches_written",
    "cards_observed",
    "content_digest",
    "receipt_sha256",
    "turn_lineage_sha256",
}


def load_proxy() -> dict:
    environment = {
        "TAEY_LINKEDIN_JOB_SEARCH_PYTHON": sys.executable,
        "TAEY_LINKEDIN_JOB_SEARCH_PRIVATE_ROOT": "/private/linkedin-job-search",
        "TAEY_LINKEDIN_JOB_SEARCH_DISPLAYS": ":18",
        "TAEY_LINKEDIN_JOB_SEARCH_TIMEOUT_SECS": "1800",
        "TAEYS_HANDS_ROOT": "/public/taeys-hands",
    }
    prior = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    module = types.ModuleType("linkedin_job_search_profile_validation")
    module.__file__ = str(PROXY_PATH)
    sys.modules[module.__name__] = module
    try:
        source = PROXY_PATH.read_text(encoding="utf-8")
        exec(compile(source, str(PROXY_PATH), "exec"), module.__dict__)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module.__dict__


def result(
    *,
    state: str = "captured",
    ok: bool = True,
    failure_code: str | None = None,
    batches_observed: int = 1,
    batches_written: int | None = 1,
    cards_observed: int = 25,
    content_digest: str | None = "a" * 64,
) -> dict:
    return {
        "ok": ok,
        "platform": "linkedin",
        "display": ":18",
        "state": state,
        "failure_code": failure_code,
        "batches_observed": batches_observed,
        "batches_written": batches_written,
        "cards_observed": cards_observed,
        "content_digest": content_digest,
        "receipt_sha256": "b" * 64,
        "turn_lineage_sha256": "c" * 64,
    }


def main() -> int:
    namespace = load_proxy()
    profile = namespace["_LINKEDIN_JOB_SEARCH_TOOL_PROFILE"]
    assert profile == "linkedin-job-search"
    assert namespace["_TOOL_PROFILE_ALLOWED"][profile] == frozenset({
        "linkedin_job_search"
    })
    tools = {
        item["function"]["name"]: item["function"]
        for item in namespace["TOOLS"]
    }
    tool = tools["linkedin_job_search"]
    assert tool["parameters"]["required"] == ["display"]
    assert tool["parameters"]["additionalProperties"] is False
    spec = namespace["_private_transaction_spec_for_tool"]("linkedin_job_search")
    assert spec.profile == profile
    assert spec.runner_name == "run_linkedin_job_search.py"
    assert spec.claim_schema == "linkedin_job_search_claim_v1"
    assert spec.expected_result_keys == frozenset(EXPECTED_RESULT_KEYS)
    assert spec.displays == (":18",)
    assert spec.deadline_secs == 1700
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "Call `linkedin_job_search` exactly once" in prompt
    assert "Never retry" in prompt
    context = {
        "tool_profile": profile,
        "_tool_profile_state": {},
    }
    original_dispatch = namespace["_do_linkedin_job_search"]
    namespace["_do_linkedin_job_search"] = lambda arguments: json.dumps(arguments)
    token = namespace["_request_context"].set(context)
    try:
        assert json.loads(namespace["execute_tool_call"](
            "linkedin_job_search", {"display": ":18"}
        )) == {"display": ":18"}
        refusal = namespace["execute_tool_call"]("linkedin_jobs", {"display": ":18"})
        assert "not available in profile" in refusal
    finally:
        namespace["_request_context"].reset(token)
        namespace["_do_linkedin_job_search"] = original_dispatch

    validate = namespace["_linkedin_job_search_result_error"]
    valid_cases = [
        (result(), 0),
        (result(state="already_captured", batches_written=0), 0),
        (result(state="no_cards", cards_observed=0), 0),
        (
            result(
                state="postcondition_failed",
                ok=False,
                failure_code="postcondition_failed",
                batches_written=0,
            ),
            2,
        ),
        (
            result(
                state="technical_failure",
                ok=False,
                failure_code="pre_observation_failed",
                batches_observed=0,
                batches_written=0,
                cards_observed=0,
                content_digest=None,
            ),
            2,
        ),
        (
            result(
                state="technical_failure",
                ok=False,
                failure_code="sink_write_indeterminate",
                batches_written=None,
            ),
            2,
        ),
    ]
    for payload, returncode in valid_cases:
        assert set(payload) == EXPECTED_RESULT_KEYS
        assert validate(payload, returncode) is None

    invalid_cases = [
        (result(ok=False), 0),
        (result(cards_observed=0), 0),
        (result(content_digest="not-a-digest"), 0),
        (result(batches_written=2), 0),
        (
            result(
                state="technical_failure",
                ok=False,
                failure_code="unexpected",
                batches_observed=0,
                batches_written=0,
                cards_observed=0,
                content_digest=None,
            ),
            2,
        ),
    ]
    for payload, returncode in invalid_cases:
        assert validate(payload, returncode) is not None

    print(json.dumps({
        "profile": profile,
        "result_keys": sorted(EXPECTED_RESULT_KEYS),
        "runner": spec.runner_name,
        "status": "PASS",
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
