from dashboard import app as dashboard


def _ingress(event_id, content, *, source="ui"):
    return {
        "event_type": "executive_ingress",
        "event_id": event_id,
        "source": source,
        "kind": "user_prompt" if source == "ui" else "fleet_message",
        "role": "user",
        "content": content,
    }


def _outcome(event_id, content, *, ok=True, source="taey"):
    return {
        "event_type": "turn_outcome",
        "event_id": event_id,
        "source": source,
        "kind": "assistant_reply",
        "role": "assistant",
        "content": content,
        "ok": ok,
    }


def test_history_contains_completed_ui_pairs_and_current_prompt_only():
    events = [
        _ingress("complete", "Do completed work"),
        _outcome("complete", "Completed answer"),
        _ingress("failed", "Prior assignment with no answer"),
        {
            "event_type": "turn_outcome",
            "event_id": "failed",
            "source": "ui",
            "kind": "assistant_failure",
            "ok": False,
        },
        _ingress("fleet", "Monitor packet", source="fleet"),
        _outcome("fleet", "Acknowledged; no follow-up needed"),
        _outcome("seat", "Nothing else actionable queued"),
        _ingress("current", "Current production assignment"),
    ]

    assert dashboard._ui_model_history(
        events,
        current_event_id="current",
    ) == [
        {"role": "user", "content": "Do completed work"},
        {"role": "assistant", "content": "Completed answer"},
        {"role": "user", "content": "Current production assignment"},
    ]


def test_history_excludes_incomplete_prior_ui_lifecycle():
    events = [
        _ingress("failed", "Revenue assignment"),
        _outcome("fleet", "Fresh-session greeting"),
    ]

    assert dashboard._ui_model_history(events) == []
