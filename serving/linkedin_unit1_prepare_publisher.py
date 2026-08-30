from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


BOOTSTRAP_SCHEMA = "taey_linkedin_unit1_prepare_bootstrap_v1"
FINAL_BUNDLE_SCHEMA = "taey_linkedin_unit1_private_bundle_v1"
PREPARATION_ENVELOPE_SCHEMA = "linkedin_unit1_preparation_envelope_v1"
DRAFT_GATE_SCHEMA = "taey_linkedin_unit1_draft_gate_receipt_v1"
NOTIFICATION_INVENTORY_SCHEMA = "linkedin_notification_inventory_v1"
NOTIFICATION_DECISION_INVENTORY_SCHEMA = (
    "linkedin_notification_decision_inventory_v1"
)
PRIVATE_SELECTION_DECISION_SCHEMA = "linkedin_unit1_private_selection_decision_v1"
NOTIFICATION_EXCLUSIONS_SCHEMA = "linkedin_notification_inventory_exclusions_v1"
SELECTED_SOURCE_SCHEMA = "linkedin_selected_post_thread_source_v1"
EXCLUSION_REASON_CODES = frozenset({
    "already_used",
    "author_cooloff",
    "event_announcement",
    "hostile_or_irrelevant",
    "off_target",
    "pitch_or_promotion",
    "self_authored",
    "stale",
})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ACTIVITY = re.compile(r"^[0-9]+$")
_BOOTSTRAP_KEYS = frozenset({
    "correlation_id", "draft_policy", "event_id", "expected_author_name",
    "identity_context", "like_authorized", "preparation", "schema", "seat_id",
    "selection_policy",
})
_PREPARATION_KEYS = frozenset({
    "cycle_id", "display", "operation", "policy_sha256", "schema", "selection",
    "transaction_id",
})


class LinkedInUnit1PreparePublisherError(ValueError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def preparation_transaction_sha256(preparation: Mapping[str, Any]) -> str:
    return canonical_sha256({
        key: preparation[key]
        for key in (
            "cycle_id", "display", "operation", "policy_sha256", "schema",
            "transaction_id",
        )
    })


def validate_bootstrap(
    value: Mapping[str, Any], *, seat_id: str, event_id: str, correlation_id: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != _BOOTSTRAP_KEYS:
        raise LinkedInUnit1PreparePublisherError("preparation bootstrap fields are invalid")
    bootstrap = dict(value)
    if (
        bootstrap["schema"] != BOOTSTRAP_SCHEMA
        or bootstrap["seat_id"] != seat_id
        or bootstrap["event_id"] != event_id
        or bootstrap["correlation_id"] != correlation_id
        or not isinstance(bootstrap["like_authorized"], bool)
    ):
        raise LinkedInUnit1PreparePublisherError("preparation bootstrap identity is invalid")
    for field in ("identity_context", "selection_policy", "draft_policy"):
        text = bootstrap[field]
        if not isinstance(text, str) or not text.strip() or "\x00" in text or len(text) > 65536:
            raise LinkedInUnit1PreparePublisherError(f"{field} is invalid")
    author = bootstrap["expected_author_name"]
    if (
        not isinstance(author, str) or not author or author != author.strip()
        or len(author) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in author)
    ):
        raise LinkedInUnit1PreparePublisherError("expected_author_name is invalid")
    preparation = bootstrap["preparation"]
    if (
        not isinstance(preparation, Mapping)
        or frozenset(preparation) != _PREPARATION_KEYS
        or preparation.get("schema") != PREPARATION_ENVELOPE_SCHEMA
        or preparation.get("operation") != "comment_from_notifications_prepare"
        or preparation.get("selection") is not None
        or not isinstance(preparation.get("cycle_id"), str)
        or _PUBLIC_ID.fullmatch(preparation["cycle_id"]) is None
        or not isinstance(preparation.get("transaction_id"), str)
        or _PUBLIC_ID.fullmatch(preparation["transaction_id"]) is None
        or not isinstance(preparation.get("display"), str)
        or re.fullmatch(r":[1-9][0-9]{0,2}", preparation["display"]) is None
    ):
        raise LinkedInUnit1PreparePublisherError("preparation envelope is invalid")
    policy_material = {
        key: bootstrap[key]
        for key in (
            "draft_policy", "expected_author_name", "identity_context",
            "like_authorized", "selection_policy",
        )
    }
    if preparation.get("policy_sha256") != canonical_sha256(policy_material):
        raise LinkedInUnit1PreparePublisherError("private policy digest is invalid")
    bootstrap["preparation"] = dict(preparation)
    return bootstrap


def build_selection(
    selection_input: Mapping[str, Any], arguments: Mapping[str, Any],
    preparation: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_arguments = {
        "action", "author_cooloff_passed", "dedup_passed", "display",
        "selected_notification_ordinal", "target_passed",
    }
    if set(arguments) != expected_arguments or any(
        arguments[field] is not True
        for field in ("target_passed", "dedup_passed", "author_cooloff_passed")
    ):
        raise LinkedInUnit1PreparePublisherError("selection verdicts are incomplete")
    ordinal = arguments["selected_notification_ordinal"]
    inventory = selection_input.get("notification_inventory")
    if (
        selection_input.get("schema") != "linkedin_unit1_private_selection_input_v1"
        or selection_input.get("policy_sha256") != preparation["policy_sha256"]
        or selection_input.get("transaction_sha256")
        != preparation_transaction_sha256(preparation)
        or isinstance(ordinal, bool) or not isinstance(ordinal, int)
        or not isinstance(inventory, Mapping)
        or not isinstance(inventory.get("rows"), list)
        or not isinstance(inventory.get("actionable_links"), list)
        or not _SHA256.fullmatch(
            str(inventory.get("decision_inventory_sha256") or "")
        )
        or not _SHA256.fullmatch(str(inventory.get("inventory_sha256") or ""))
    ):
        raise LinkedInUnit1PreparePublisherError("selection input is invalid")
    links = [row for row in inventory["actionable_links"] if row.get("ordinal") == ordinal]
    if len(links) != 1:
        raise LinkedInUnit1PreparePublisherError("selected ordinal is not exact")
    if not isinstance(ordinal, int) or not 1 <= ordinal <= len(inventory["rows"]):
        raise LinkedInUnit1PreparePublisherError("selected ordinal is invalid")
    activity = links[0].get("activity")
    if not isinstance(activity, str) or _ACTIVITY.fullmatch(activity) is None:
        raise LinkedInUnit1PreparePublisherError("selected activity binding is invalid")
    row = inventory["rows"][ordinal - 1]
    text = row.get("notification_text")
    if (
        row.get("activity") != activity or row.get("actionable") is not True
        or row.get("ordinal") != ordinal or row.get("age_seconds") != links[0].get("age_seconds")
        or not isinstance(text, str)
        or hashlib.sha256(text.encode()).hexdigest() != row.get("notification_text_sha256")
    ):
        raise LinkedInUnit1PreparePublisherError("selected notification binding is invalid")
    selection = {
        "notification_inventory_sha256": inventory["inventory_sha256"],
        "selected_activity": activity,
        "selected_age_seconds": row["age_seconds"],
        "selected_notification_ordinal": ordinal,
        "selected_notification_text": text,
        "selected_notification_text_sha256": row["notification_text_sha256"],
        "target_passed": True, "dedup_passed": True, "author_cooloff_passed": True,
        "transaction_sha256": preparation_transaction_sha256(preparation),
    }
    selection["selection_sha256"] = canonical_sha256(selection)
    return selection, dict(inventory)


def selection_decision_input(selection_input: Mapping[str, Any]) -> dict[str, Any]:
    inventory = selection_input.get("notification_inventory")
    decision = selection_input.get("decision_input")
    if (
        set(selection_input)
        != {
            "schema", "policy_sha256", "transaction_sha256",
            "notification_inventory", "decision_input", "continuation_available",
        }
        or selection_input.get("schema")
        != "linkedin_unit1_private_selection_input_v1"
        or not isinstance(inventory, Mapping)
        or not isinstance(inventory.get("rows"), list)
        or not isinstance(inventory.get("actionable_links"), list)
        or not isinstance(decision, Mapping)
    ):
        raise LinkedInUnit1PreparePublisherError(
            "private selection decision input is invalid"
        )
    expected_candidates: list[dict[str, Any]] = []
    for link in inventory["actionable_links"]:
        ordinal = link.get("ordinal") if isinstance(link, Mapping) else None
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or not 1 <= ordinal <= len(inventory["rows"])
        ):
            raise LinkedInUnit1PreparePublisherError(
                "private selection decision ordinal is invalid"
            )
        row = inventory["rows"][ordinal - 1]
        if (
            not isinstance(row, Mapping)
            or row.get("actionable") is not True
            or row.get("activity") != link.get("activity")
            or row.get("ordinal") != ordinal
            or row.get("age_seconds") != link.get("age_seconds")
            or not isinstance(row.get("notification_text"), str)
            or hashlib.sha256(row["notification_text"].encode()).hexdigest()
            != row.get("notification_text_sha256")
        ):
            raise LinkedInUnit1PreparePublisherError(
                "private selection decision candidate binding is invalid"
            )
        expected_candidates.append({
            "activity": link["activity"],
            "notification_text": row["notification_text"],
            "notification_text_sha256": row["notification_text_sha256"],
            "age_seconds": row["age_seconds"],
            "age_token": row["age_token"],
            "ordinal": ordinal,
            "element": link["element"],
            "element_sha256": link["element_sha256"],
            "uri": link["uri"],
            "uri_sha256": link["uri_sha256"],
        })
    expected = {
        "schema": PRIVATE_SELECTION_DECISION_SCHEMA,
        "policy_sha256": selection_input["policy_sha256"],
        "transaction_sha256": selection_input["transaction_sha256"],
        "continuation_available": selection_input["continuation_available"],
        "decision_inventory_sha256": inventory["decision_inventory_sha256"],
        "inventory_sha256": inventory["inventory_sha256"],
        "mounted_article_count": inventory["mounted_article_count"],
        "actionable_candidates": expected_candidates,
    }
    if dict(decision) != expected:
        raise LinkedInUnit1PreparePublisherError(
            "private selection decision projection does not bind full inventory"
        )
    return expected


def build_exclusions(
    selection_input: Mapping[str, Any], arguments: Mapping[str, Any],
    preparation: Mapping[str, Any],
) -> dict[str, Any]:
    if set(arguments) != {"action", "display", "excluded_candidates"}:
        raise LinkedInUnit1PreparePublisherError(
            "exclusion decision fields are incomplete or unknown"
        )
    inventory = selection_input.get("notification_inventory")
    rows = arguments["excluded_candidates"]
    if (
        selection_input.get("schema")
        != "linkedin_unit1_private_selection_input_v1"
        or selection_input.get("continuation_available") is not True
        or selection_input.get("policy_sha256") != preparation["policy_sha256"]
        or selection_input.get("transaction_sha256")
        != preparation_transaction_sha256(preparation)
        or not isinstance(inventory, Mapping)
        or not isinstance(inventory.get("actionable_links"), list)
        or not _SHA256.fullmatch(str(inventory.get("inventory_sha256") or ""))
        or not isinstance(rows, list)
    ):
        raise LinkedInUnit1PreparePublisherError("exclusion input is invalid")
    expected_ordinals: list[int] = []
    activities_by_ordinal: dict[int, str] = {}
    for link in inventory["actionable_links"]:
        activity = link.get("activity") if isinstance(link, Mapping) else None
        ordinal = link.get("ordinal") if isinstance(link, Mapping) else None
        if (
            not isinstance(activity, str)
            or _ACTIVITY.fullmatch(activity) is None
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 1
            or ordinal in activities_by_ordinal
        ):
            raise LinkedInUnit1PreparePublisherError(
                "exact actionable inventory ordinals are invalid"
            )
        expected_ordinals.append(ordinal)
        activities_by_ordinal[ordinal] = activity
    excluded_candidates: list[dict[str, Any]] = []
    submitted_ordinals: list[int] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {
            "notification_ordinal", "reason_codes",
        }:
            raise LinkedInUnit1PreparePublisherError(
                "excluded candidate fields are incomplete or unknown"
            )
        ordinal = row["notification_ordinal"]
        reason_codes = row["reason_codes"]
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal not in activities_by_ordinal
            or not isinstance(reason_codes, list)
            or not reason_codes
            or reason_codes != sorted(set(reason_codes))
            or any(
                not isinstance(reason, str)
                or reason not in EXCLUSION_REASON_CODES
                for reason in reason_codes
            )
        ):
            raise LinkedInUnit1PreparePublisherError(
                "excluded candidate evidence is invalid"
            )
        submitted_ordinals.append(ordinal)
        excluded_candidates.append({
            "activity": activities_by_ordinal[ordinal],
            "reason_codes": list(reason_codes),
        })
    if submitted_ordinals != expected_ordinals:
        raise LinkedInUnit1PreparePublisherError(
            "exclusions do not cover the exact actionable inventory"
        )
    decision = {
        "schema": NOTIFICATION_EXCLUSIONS_SCHEMA,
        "decision_inventory_sha256": inventory["decision_inventory_sha256"],
        "notification_inventory_sha256": inventory["inventory_sha256"],
        "policy_sha256": preparation["policy_sha256"],
        "transaction_sha256": preparation_transaction_sha256(preparation),
        "excluded_candidates": excluded_candidates,
    }
    decision["exclusions_sha256"] = canonical_sha256(decision)
    return decision


def build_final_bundle(
    *, bootstrap: Mapping[str, Any], selection: Mapping[str, Any],
    inventory: Mapping[str, Any], draft_input: Mapping[str, Any], text: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(text, str) or not text or len(text) > 1800 or "\x00" in text:
        raise LinkedInUnit1PreparePublisherError("draft must be 1-1800 non-NUL characters")
    source = draft_input.get("source")
    thread = source.get("thread") if isinstance(source, Mapping) else None
    post = source.get("post") if isinstance(source, Mapping) else None
    if (
        draft_input.get("schema") != "linkedin_unit1_private_draft_input_v1"
        or draft_input.get("policy_sha256") != bootstrap["preparation"]["policy_sha256"]
        or draft_input.get("transaction_sha256") != selection["transaction_sha256"]
        or draft_input.get("selection_sha256") != selection["selection_sha256"]
        or not isinstance(source, Mapping)
        or source.get("selected_activity") != selection["selected_activity"]
        or source.get("selection_sha256") != selection["selection_sha256"]
        or source.get("transaction_sha256") != selection["transaction_sha256"]
        or not _SHA256.fullmatch(str(source.get("source_sha256") or ""))
        or not isinstance(thread, Mapping) or thread.get("read_complete") is not True
        or thread.get("exact_comment_count") != len(thread.get("typed_rows") or [])
        or not isinstance(post, Mapping) or not _SHA256.fullmatch(str(post.get("body_sha256") or ""))
    ):
        raise LinkedInUnit1PreparePublisherError("draft source binding is invalid")
    links = sorted(inventory["actionable_links"], key=lambda row: row["ordinal"])
    activities = [row["activity"] for row in links]
    if [row["ordinal"] for row in links] != list(range(1, len(links) + 1)):
        raise LinkedInUnit1PreparePublisherError("notification stream order is incomplete")
    text_sha256 = hashlib.sha256(text.encode()).hexdigest()
    preparation = bootstrap["preparation"]
    private_input = {
        "schema": "linkedin_unit1_private_input_v1",
        "operation": "comment_from_notifications",
        "cycle_id": preparation["cycle_id"], "transaction_id": preparation["transaction_id"],
        "display": preparation["display"], "policy_sha256": preparation["policy_sha256"],
        "notification_stream_sha256": hashlib.sha256("\n".join(activities).encode()).hexdigest(),
        "selected_activity": selection["selected_activity"],
        "selected_age_seconds": selection["selected_age_seconds"],
        "freshness_max_hours": 72, "target_passed": True, "dedup_passed": True,
        "author_cooloff_passed": True, "selected_post_body_sha256": post["body_sha256"],
        "thread_evidence_sha256": source["source_sha256"],
        "like_authorized": bootstrap["like_authorized"], "text": text,
        "text_sha256": text_sha256, "expected_author_name": bootstrap["expected_author_name"],
    }
    bundle = {
        "schema": FINAL_BUNDLE_SCHEMA, "seat_id": bootstrap["seat_id"],
        "event_id": bootstrap["event_id"], "correlation_id": bootstrap["correlation_id"],
        "private_input": private_input, "receipts": [],
    }
    gate = {
        "schema": DRAFT_GATE_SCHEMA, "verdict": "PASS",
        "transaction_sha256": selection["transaction_sha256"],
        "selection_sha256": selection["selection_sha256"],
        "source_sha256": source["source_sha256"], "text_sha256": text_sha256,
        "private_input_sha256": canonical_sha256(private_input),
    }
    gate["receipt_sha256"] = canonical_sha256(gate)
    return bundle, gate
