"""Regression guards for tool receipts and transcript-event construction.

These are guards, NOT production acceptance. They prove the constructors and the
receipt-policy behave as written; they say nothing about a live turn. Acceptance
is a real production observation on a real turn.

What each guard exists to stop, stated as the defect it caught:

  * tool_end recorded `result_chars` and dropped the outcome, so an attempt and
    its result were the same event -- a `type` call was read from the audit and
    reported as text having been entered when the page showed it never landed.
  * The obvious fix -- persist the result body -- is worse: content-returning
    tools hand back file contents, database rows and whole Chat answers, so a
    durable log would manufacture a second copy of exactly the material the
    artifact transport exists to keep out of the model.
  * Two writers produced `executive_ingress` under two schemas (`content` vs
    `context_content`), so a reader following one saw an empty prompt body.
  * The audit inherited the process umask (observed 0664, world-readable) while
    holding command arguments and error text.

An earlier revision of this file asserted on `event["source"] == "ui"`. That
checks a string a writer happened to set, not that the event was built by the
canonical constructor or that a lifecycle can be joined -- so it would have
passed against every one of the defects above. These assert construction and
correlation instead.
"""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import uuid

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(module_name, relpath):
    """Load by absolute path.

    `dashboard/app.py` and `serving/soma_proxy.py` are not unique basenames on
    this machine -- there are same-named modules in sibling repos, and a plain
    `import app` resolves by sys.path order, so a test can silently assert
    against a different repo's file. Loading by path removes the ambiguity.
    """
    path = os.path.join(REPO, relpath)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    assert os.path.abspath(module.__file__) == os.path.abspath(path)
    return module


@pytest.fixture(scope="module")
def proxy():
    os.environ.setdefault(
        "SYSTEM_PROMPT_PATH", os.path.join(REPO, "serving", "TAEY_OPERATING_PROMPT.md")
    )
    sys.path.insert(0, os.path.join(REPO, "serving"))
    return _load("_obs_soma_proxy", "serving/soma_proxy.py")


@pytest.fixture(scope="module")
def dash():
    os.environ.setdefault(
        "SYSTEM_PROMPT_PATH", os.path.join(REPO, "serving", "TAEY_OPERATING_PROMPT.md")
    )
    sys.path.insert(0, os.path.join(REPO, "dashboard"))
    return _load("_obs_dashboard_app", "dashboard/app.py")


def _fake_credential():
    """Built at runtime so this file contains no credential-shaped literal.

    A hard-coded fake still trips the secret scanner, and the correct response
    to that is to not write one -- not to add an ignore rule, which would blind
    the gate to a real one later.
    """
    return "".join(uuid.uuid4().hex for _ in range(2))


# ---------------------------------------------------------------------------
# Receipt policy: prove the outcome, do not persist the content
# ---------------------------------------------------------------------------


def test_content_returning_tools_persist_no_body(proxy):
    """run_command / file+db reads / fetches return content by definition."""
    body = "row1\nrow2\nSSN 123-45-6789 and a customer address"
    for name in sorted(proxy._CONTENT_RETURNING):
        receipt = proxy._tool_receipt(name, {}, body, ok=True)
        flat = json.dumps(receipt)
        assert "row1" not in flat, f"{name} persisted its result body"
        assert "123-45-6789" not in flat, f"{name} persisted its result body"
        assert "result_preview" not in receipt, f"{name} persisted a preview"
        # It must still be provable WHICH bytes came back.
        assert receipt["result_chars"] == len(body)
        assert receipt["result_sha256"] == hashlib.sha256(body.encode()).hexdigest()


def test_chat_extraction_is_digest_only(proxy):
    """extract/read_clipboard results ARE the Chat answer body."""
    answer = "Gaia's full consultation response, several thousand characters of it."
    for action in ("extract", "read_clipboard"):
        receipt = proxy._tool_receipt(
            "drive_chat",
            {"action": action, "display": ":3"},
            json.dumps({"ok": True, "result": {"text": answer}}),
            ok=True,
        )
        flat = json.dumps(receipt)
        assert "consultation response" not in flat
        assert "result_preview" not in receipt
        assert receipt["action"] == action
        assert receipt["result_sha256"]


def test_receipt_policy_is_selected_by_action_not_only_tool_name(proxy):
    """The builder is passed the arguments, so drive_chat's policy varies by
    action: a click verdict is not content, an extracted answer is."""
    click = proxy._tool_receipt(
        "drive_chat",
        {"action": "click", "display": ":3", "element": "toggle_menu"},
        json.dumps({"ok": True, "result": {"performed": True, "via": "element_map"}}),
        ok=True,
    )
    extract = proxy._tool_receipt(
        "drive_chat",
        {"action": "extract", "display": ":3"},
        json.dumps({"ok": True, "result": {"text": "an answer"}}),
        ok=True,
    )
    assert click["performed"] is True and click["via"] == "element_map"
    assert "performed" not in extract and "text" not in extract


def test_receipt_separates_attempt_from_result(proxy):
    """The original defect: a call that ran and a call that WORKED were the same
    record. A tool reporting ok:false at the transport layer must be readable as
    a failure without re-running anything."""
    failed = proxy._tool_receipt(
        "drive_chat",
        {"action": "type", "display": ":3"},
        json.dumps({"ok": False, "error": "composer did not accept text"}),
        ok=True,  # the CALL succeeded; the ACTION did not
    )
    assert failed["tool_ok"] is False
    assert "composer did not accept text" in failed["tool_error"]


def test_exception_paths_are_redacted_too(proxy):
    """A failing run_command puts the command's output -- and any credential in
    it -- into the exception message."""
    secret = _fake_credential()
    exc = RuntimeError(f"curl failed: api_key={secret} rejected by upstream")
    receipt = proxy._tool_receipt("run_command", {}, exc, ok=False)
    assert receipt["ok"] is False
    assert secret not in json.dumps(receipt)
    assert "[REDACTED]" in receipt["error"]


def test_successful_results_are_redacted(proxy):
    secret = _fake_credential()
    receipt = proxy._tool_receipt(
        "some_unlisted_tool", {}, f"config loaded, token: {secret}", ok=True
    )
    assert secret not in json.dumps(receipt)


# ---------------------------------------------------------------------------
# Audit file: created 0600, bounded on disk
# ---------------------------------------------------------------------------


def test_audit_file_is_created_private(proxy):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "audit.jsonl")
        os.umask(0o000)  # the umask that produced the observed world-readable log
        with proxy._audit_open(path) as f:
            f.write("{}\n")
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_audit_tightens_an_already_permissive_file(proxy):
    """A log that already leaked its mode must not stay that way."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "audit.jsonl")
        with open(path, "w") as f:
            f.write("{}\n")
        os.chmod(path, 0o664)
        with proxy._audit_open(path) as f:
            f.write("{}\n")
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_audit_rotates_and_retains(proxy, monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "audit.jsonl")
        monkeypatch.setattr(proxy, "_AUDIT_MAX_BYTES", 100)
        monkeypatch.setattr(proxy, "_AUDIT_KEEP", 2)
        for _ in range(6):
            proxy._audit_rotate_if_needed(path)
            with proxy._audit_open(path) as f:
                f.write("x" * 80 + "\n")
        rotated = sorted(p for p in os.listdir(tmp) if p != "audit.jsonl")
        assert rotated == ["audit.jsonl.1", "audit.jsonl.2"], rotated
        assert os.path.getsize(path) < 200


# ---------------------------------------------------------------------------
# Transcript events: one constructor, one join key
# ---------------------------------------------------------------------------


def test_ingress_constructor_emits_both_reader_schemas_identically(dash):
    """Not `event["source"] == "ui"` -- that string was already correct while
    readers of `context_content` saw nothing."""
    event = dash._ingress_event(
        event_id="e1", correlation_id="c1", source="ui",
        kind="user_prompt", body="  do the thing  ",
    )
    assert event["content"] == event["context_content"] == "  do the thing  "
    assert event["event_type"] == "executive_ingress"
    assert event["source_id"] == "e1"


def test_ingress_constructor_never_emits_a_null_body(dash):
    event = dash._ingress_event(
        event_id="e1", correlation_id="c1", source="seat", kind="user_prompt", body=None
    )
    assert event["content"] == "" and event["context_content"] == ""


def test_every_ingress_writer_uses_the_constructor(dash):
    """The guard against a third schema appearing. A writer that builds the dict
    inline is exactly how the two-schema split happened in the first place."""
    source = open(os.path.join(REPO, "dashboard", "app.py")).read()
    inline = source.count('"event_type": "executive_ingress"')
    assert inline == 1, (
        f"{inline} inline executive_ingress literals; only the constructor "
        "itself may build one"
    )
    assert source.count('"context_content"') == 1


def test_attempt_and_outcomes_join_on_the_lifecycle_key(dash):
    """event_id identifies one prompt-to-outcome lifecycle; attempt_id
    distinguishes retries within it."""
    event_id, correlation_id = "lifecycle-1", "corr-1"
    ingress = dash._ingress_event(
        event_id=event_id, correlation_id=correlation_id,
        source="ui", kind="user_prompt", body="prompt",
    )
    attempt = dash._turn_attempt_event(
        event_id=event_id, correlation_id=correlation_id, attempt_id="a1",
        source="ui", kind="user_prompt", prompt="prompt",
    )
    retry = dash._turn_attempt_event(
        event_id=event_id, correlation_id=correlation_id, attempt_id="a2",
        source="ui", kind="user_prompt", prompt="prompt",
    )
    outcome = {
        "event_type": "turn_outcome", "event_id": event_id,
        "correlation_id": correlation_id, "kind": "assistant_reply", "ok": True,
    }
    unrelated = dash._ingress_event(
        event_id="lifecycle-2", correlation_id="corr-2",
        source="ui", kind="user_prompt", body="other",
    )

    transcript = [ingress, attempt, unrelated, retry, outcome]
    joined = [e for e in transcript if e.get("event_id") == event_id]
    assert len(joined) == 4
    assert {e["event_type"] for e in joined} == {
        "executive_ingress", "turn_attempt", "turn_outcome"
    }
    assert {e["attempt_id"] for e in joined if "attempt_id" in e} == {"a1", "a2"}
    assert unrelated not in joined


def test_streaming_turn_opens_an_attempt_under_the_ingress_key(dash):
    """An in-flight turn was indistinguishable from no turn at all: only the
    outcome was recorded, so there was no start, no duration, and nothing to
    join from. Observed 2026-08-16 -- no turn appeared open since 21:43 while
    work ran continuously from 21:55.

    Checked over the AST rather than by string search: the claim is that the
    handler CONSTRUCTS an attempt bound to the same `event_id` the ingress used,
    and a substring match cannot tell that from the same words in a comment.
    """
    import ast

    tree = ast.parse(open(os.path.join(REPO, "dashboard", "app.py")).read())
    handler = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "chat_session_stream"
    )

    def kwarg(call, name):
        return next((k.value for k in call.keywords if k.arg == name), None)

    def calls_to(fn_name, kind):
        """The handler builds several ingress events -- a user prompt and, on an
        active round, an amendment. They are separate lifecycles, so select by
        kind rather than taking whichever happens to come last."""
        return [
            node for node in ast.walk(handler)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == fn_name
            and isinstance(kwarg(node, "kind"), ast.Constant)
            and kwarg(node, "kind").value == kind
        ]

    ingress = calls_to("_ingress_event", "user_prompt")
    attempts = calls_to("_turn_attempt_event", "user_prompt")
    assert len(ingress) == 1, "streaming prompt ingress bypasses the constructor"
    assert len(attempts) == 1, "streaming turn opens no attempt record"

    ingress_key = kwarg(ingress[0], "event_id")
    attempt_key = kwarg(attempts[0], "event_id")
    assert isinstance(ingress_key, ast.Name) and isinstance(attempt_key, ast.Name)
    assert ingress_key.id == attempt_key.id, (
        "attempt is keyed to a different variable than the ingress, so the two "
        "cannot be joined"
    )
    # A retry must be distinguishable from the turn it retries.
    assert kwarg(attempts[0], "attempt_id") is not None
