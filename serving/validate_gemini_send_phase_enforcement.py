#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "serving"))

import soma_proxy  # noqa: E402


REVISION = "4" * 64
SEND_REF = "atspi3." + ("a" * 80)
START_REF = "atspi3." + ("b" * 80)


def _card(
    phase: str,
    allowed: dict[str, str] | None,
    next_phase: str | None,
) -> dict[str, object]:
    card: dict[str, object] = {
        "schema": "taey.gemini_dr_send_phase.v1",
        "platform": "gemini",
        "display": ":4",
        "phase": phase,
        "snapshot_revision": REVISION,
        "extraction_output_type": "research_report",
        "allowed": allowed,
        "next_phase": next_phase,
    }
    encoded = json.dumps(
        card,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    card["card_sha256"] = hashlib.sha256(encoded).hexdigest()
    return card


def _context(
    *,
    profile: str = "manual-chat-ui",
    active: bool = False,
    phase: str = "awaiting_initial_send",
) -> dict[str, object]:
    return {
        "seat_id": "phase-seat",
        "turn_id": "phase-turn",
        "process_generation": "a" * 32,
        "tool_profile": profile,
        "tool_round": 1,
        "_ui_sequence": {
            "observations": {},
            "terminal": None,
            "send_phase": {
                "active": active,
                "phase": phase,
                "card": None,
            },
        },
        "_tool_profile_state": {"terminal": None},
    }


def _observe_result(
    *,
    platform: str,
    mapped: list[dict[str, object]],
    scope: str = "base",
    allowed_next: object = ...,
) -> SimpleNamespace:
    result: dict[str, object] = {
        "snapshot_revision": REVISION,
        "surface": "browser",
        "scope": scope,
        "mapped": mapped,
        "key_preconditions": {},
    }
    if allowed_next is not ...:
        result["allowed_next"] = allowed_next
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"ok": True, "platform": platform, "result": result}),
        stderr="",
    )


def _mutation_result(platform: str = "gemini") -> SimpleNamespace:
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps({
            "ok": True,
            "platform": platform,
            "result": {"performed": True},
        }),
        stderr="",
    )


def _run_with_context(
    context: dict[str, object],
    arguments: dict[str, object],
    response: SimpleNamespace,
) -> tuple[dict[str, object], list[str], int]:
    commands: list[list[str]] = []
    monitor_calls = 0

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return response

    def monitor_touch(*_args: object, **_kwargs: object) -> None:
        nonlocal monitor_calls
        monitor_calls += 1
        context["_monitor_touch_kwargs"] = dict(_kwargs)

    token = soma_proxy._request_context.set(context)
    try:
        with patch.object(soma_proxy, "_monitor_touch", side_effect=monitor_touch), patch(
            "subprocess.run", side_effect=run
        ):
            result = json.loads(soma_proxy._do_drive_chat(arguments))
    finally:
        soma_proxy._request_context.reset(token)
    return result, commands[0] if commands else [], monitor_calls


def main() -> int:
    tools = soma_proxy._tools_for_profile("manual-chat-ui-send")
    assert len(tools) == 1
    parameters = tools[0]["function"]["parameters"]
    properties = parameters["properties"]
    assert set(properties) == {"display", "action", "scope", "key", "element"}
    assert properties["display"]["enum"] == [":4", ":22"]
    assert properties["action"]["enum"] == ["observe", "key", "click"]
    assert properties["scope"]["enum"] == ["base"]
    assert "output_file" not in properties

    dormant_menu = _context()
    menu_observed, menu_command, menu_monitor_calls = _run_with_context(
        dormant_menu,
        {"display": ":4", "action": "observe", "scope": "menu_snapshot"},
        _observe_result(platform="gemini", mapped=[], scope="menu_snapshot"),
    )
    assert menu_observed["ok"] is True
    assert menu_observed["ui_sequence"]["snapshot_scope"] == "menu_snapshot"
    assert menu_monitor_calls == 1
    assert "--send-phase" not in menu_command
    assert dormant_menu["_ui_sequence"]["send_phase"] == {
        "active": False,
        "phase": "awaiting_initial_send",
        "card": None,
    }

    active_menu = _context(active=True)
    active_menu_refused, active_menu_command, active_menu_monitor_calls = (
        _run_with_context(
            active_menu,
            {"display": ":4", "action": "observe", "scope": "menu_snapshot"},
            _observe_result(platform="gemini", mapped=[], scope="menu_snapshot"),
        )
    )
    assert active_menu_refused["ok"] is False
    assert "does not exactly match" in active_menu_refused["error"]
    assert active_menu_command == []
    assert active_menu_monitor_calls == 0

    initial = _context()
    ready_initial = _card(
        "ready_initial_send",
        {"action": "key", "key": "space"},
        "awaiting_start_research",
    )
    observed, observe_command, initial_monitor_calls = _run_with_context(
        initial,
        {"display": ":4", "action": "observe", "scope": "base"},
        _observe_result(
            platform="gemini",
            mapped=[{"element": "send_button", "ref": SEND_REF}],
            allowed_next=ready_initial,
        ),
    )
    assert observed["ok"] is True
    assert initial_monitor_calls == 0
    assert "allowed_next" not in observed["result"]
    assert observed["ui_sequence"]["allowed_next"] == {
        "action": "key",
        "key": "space",
    }
    assert observe_command[-2:] == ["--send-phase", "awaiting_initial_send"]
    send_state = initial["_ui_sequence"]["send_phase"]
    assert send_state["active"] is True
    assert send_state["card"] == ready_initial

    initial["tool_round"] = 2
    sent, _, _ = _run_with_context(
        initial,
        {"display": ":4", "action": "key", "key": "space"},
        _mutation_result(),
    )
    assert sent["ok"] is True
    assert send_state == {
        "active": True,
        "phase": "awaiting_start_research",
        "card": None,
    }

    initial["tool_round"] = 3
    refused_extract, extract_command, _ = _run_with_context(
        initial,
        {"display": ":4", "action": "read_clipboard", "output_file": "/tmp/x"},
        _mutation_result(),
    )
    assert refused_extract["ok"] is False
    assert "does not exactly match" in refused_extract["error"]
    assert extract_command == []

    start = _context(active=True, phase="awaiting_start_research")
    ready_start = _card(
        "ready_start_research",
        {"action": "click", "element": "yaml_owned_start"},
        "awaiting_research_stop",
    )
    start_observed, _, plan_monitor_calls = _run_with_context(
        start,
        {"display": ":4", "action": "observe", "scope": "base"},
        _observe_result(
            platform="gemini",
            mapped=[{"element": "yaml_owned_start", "ref": START_REF}],
            allowed_next=ready_start,
        ),
    )
    assert start_observed["ui_sequence"]["state"] == "ready_for_one_action"
    assert plan_monitor_calls == 0
    start["tool_round"] = 2
    clicked, click_command, _ = _run_with_context(
        start,
        {"display": ":4", "action": "click", "element": "yaml_owned_start"},
        _mutation_result(),
    )
    assert clicked["ok"] is True
    assert click_command[click_command.index("--ref") + 1] == START_REF
    assert start["_ui_sequence"]["send_phase"] == {
        "active": True,
        "phase": "awaiting_research_stop",
        "card": None,
    }

    start["tool_round"] = 3
    refused_ctrl_end, ctrl_end_command, _ = _run_with_context(
        start,
        {"display": ":4", "action": "key", "key": "ctrl+End"},
        _mutation_result(),
    )
    assert refused_ctrl_end["ok"] is False
    assert ctrl_end_command == []

    monitor = _context(active=True, phase="awaiting_research_stop")
    monitor_card = _card("monitor_ready", None, None)
    monitor_observed, _, monitor_calls = _run_with_context(
        monitor,
        {"display": ":4", "action": "observe", "scope": "base"},
        _observe_result(
            platform="gemini",
            mapped=[],
            allowed_next=monitor_card,
        ),
    )
    assert monitor_observed["ui_sequence"]["state"] == "monitor_ready"
    assert monitor_calls == 1
    assert monitor["_monitor_touch_kwargs"] == {
        "extraction_output_type": "research_report"
    }
    monitor["tool_round"] = 2
    refused_after_handoff, after_handoff_command, _ = _run_with_context(
        monitor,
        {"display": ":4", "action": "observe", "scope": "base"},
        _observe_result(platform="gemini", mapped=[], allowed_next=monitor_card),
    )
    assert refused_after_handoff["ok"] is False
    assert "completion monitor" in refused_after_handoff["error"]
    assert after_handoff_command == []

    other_platform = _context()
    other_observed, other_command, other_monitor_calls = _run_with_context(
        other_platform,
        {"display": ":6", "action": "observe", "scope": "base"},
        _observe_result(platform="perplexity", mapped=[]),
    )
    assert other_observed["ok"] is True
    assert other_monitor_calls == 1
    assert "--send-phase" not in other_command
    assert other_platform["_ui_sequence"]["send_phase"]["active"] is False

    tampered = dict(ready_initial)
    tampered["next_phase"] = "tampered"
    try:
        soma_proxy._validate_send_phase_card(
            tampered,
            platform="gemini",
            display=":4",
            snapshot_revision=REVISION,
        )
    except ValueError as exc:
        assert "digest does not verify" in str(exc)
    else:
        raise AssertionError("tampered Hands card was accepted")

    missing_output_type = dict(ready_initial)
    missing_output_type.pop("extraction_output_type")
    try:
        soma_proxy._validate_send_phase_card(
            missing_output_type,
            platform="gemini",
            display=":4",
            snapshot_revision=REVISION,
        )
    except ValueError as exc:
        assert "fields do not match" in str(exc)
    else:
        raise AssertionError("Hands card without extraction output type was accepted")

    wrong_output_type = dict(ready_initial)
    wrong_output_type["extraction_output_type"] = "assistant_text"
    wrong_output_type.pop("card_sha256")
    wrong_output_type["card_sha256"] = hashlib.sha256(
        json.dumps(
            wrong_output_type,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    try:
        soma_proxy._validate_send_phase_card(
            wrong_output_type,
            platform="gemini",
            display=":4",
            snapshot_revision=REVISION,
        )
    except ValueError as exc:
        assert "is not research_report" in str(exc)
    else:
        raise AssertionError("Hands assistant-text send card was accepted")

    class FakeRedis:
        def __init__(self) -> None:
            self.records: dict[str, str] = {}
            self.set_members: dict[str, set[str]] = {}

        def set(self, key: str, value: str, **_kwargs: object) -> None:
            self.records[key] = value

        def sadd(self, key: str, value: str) -> None:
            self.set_members.setdefault(key, set()).add(value)

    fake_redis = FakeRedis()
    stop_result = {
        "surface": "browser",
        "stop_keys": ["yaml_owned_stop"],
        "mapped": [{"element": "yaml_owned_stop"}],
        "lease": {"owned": True},
        "snapshot_revision": REVISION,
        "current_url": "https://example.invalid/thread",
    }
    route_context = {
        "seat_id": "phase-seat",
        "turn_id": "card-route",
        "process_generation": "a" * 32,
    }
    with patch.object(soma_proxy, "_mira_redis", None), patch.object(
        soma_proxy, "_redis", fake_redis
    ):
        soma_proxy._monitor_touch(
            ":4",
            "gemini",
            "observe",
            stop_result,
            route_context,
            extraction_output_type="research_report",
        )
        route_context["turn_id"] = "ordinary-route"
        soma_proxy._monitor_touch(
            ":4",
            "gemini",
            "observe",
            stop_result,
            route_context,
        )
    card_record = json.loads(
        fake_redis.records["taey:phase-seat:active_session:phase-seat-4-card-route"]
    )
    ordinary_record = json.loads(
        fake_redis.records[
            "taey:phase-seat:active_session:phase-seat-4-ordinary-route"
        ]
    )
    assert card_record["extraction_output_type"] == "research_report"
    assert "extraction_output_type" not in ordinary_record

    ui_drive_source = (REPO_ROOT / "serving" / "ui_drive.py").read_text(
        encoding="utf-8"
    )
    assert 'result["allowed_next"] = classify(' in ui_drive_source
    assert 'observe.add_argument("--send-phase", help=argparse.SUPPRESS)' in ui_drive_source
    assert "Start research" not in ui_drive_source

    prompt = (REPO_ROOT / "serving" / "TAEY_CHAT_UI_SEND_SYSTEM.md").read_text(
        encoding="utf-8"
    )
    assert "clipboard reads are outside this profile" in prompt
    print("Gemini SEND phase Presence enforcement: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
