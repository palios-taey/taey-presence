import hashlib
import json
import re
from typing import Any
DECLARED_EFFECTS = {"activate": "page", "mapped_pointer_activate": "page", "scroll_into_view": "viewport",
                    "paste_frozen_text": "draft", "activate_optional_like": "outward", "submit_frozen_comment": "outward"}
SEMANTIC_OUTWARD = frozenset({"activate_optional_like", "submit_frozen_comment"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SCROLL_TARGET = re.compile(r"[a-z][a-z0-9_]{0,63}")
def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
def operation_card(*, element: str, ref: str, declared: dict[str, Any]) -> dict[str, Any]:
    method, effect = declared.get("method"), declared.get("effect_class")
    if (not element or not ref or method not in DECLARED_EFFECTS or effect != DECLARED_EFFECTS[method]
            or declared.get("primitives") != [method] or declared.get("allowed_now") != [method]):
        raise ValueError("revenue UI operation declaration is not exact")
    card = {"schema": "taey_revenue_ui_operation_card_v1", "element": element, "ref": ref, "method": method, "effect_class": effect}
    if method == "paste_frozen_text":
        maximum = declared.get("max_text_chars")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise ValueError("paste operation has no positive YAML maximum")
        card["max_text_chars"] = maximum
    if method == "scroll_into_view":
        target = declared.get("scroll_target")
        source = declared.get("scroll_target_source")
        alignment = declared.get("scroll_alignment")
        if (
            not isinstance(target, str)
            or _SCROLL_TARGET.fullmatch(target) is None
            or source not in {"self", "mapped_context"}
            or alignment not in {"anywhere", "top_edge"}
        ):
            raise ValueError("scroll operation has no exact target and alignment")
        card.update(
            scroll_target=target,
            scroll_target_source=source,
            scroll_alignment=alignment,
        )
    if method in SEMANTIC_OUTWARD or method == "paste_frozen_text":
        activity = post.get("activity") if isinstance((post := declared.get("postcondition")), dict) else None
        body = post.get("body_sha256") if isinstance(post, dict) else None
        kind = post.get("kind") if isinstance(post, dict) else None
        if not isinstance(activity, str) or not activity.isdigit() or not _SHA256.fullmatch(str(body)) or not isinstance(kind, str) or not kind:
            raise ValueError("semantic operation has no exact activity/body")
        card.update(selected_activity=activity, selected_post_body_sha256=body, postcondition_kind=kind)
    if method == "submit_frozen_comment":
        draft = pre.get("draft_sha256") if isinstance((pre := declared.get("precondition")), dict) else None
        kind = pre.get("kind") if isinstance(pre, dict) else None
        if not _SHA256.fullmatch(str(draft)) or not isinstance(kind, str) or not kind:
            raise ValueError("submit operation has no exact draft hash")
        card.update(draft_sha256=draft, precondition_kind=kind)
    card["card_sha256"] = canonical_sha256(card)
    return card
def validate_operation_card(card: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in card.items() if key != "card_sha256"}
    method = card.get("method")
    keys = {"schema", "element", "ref", "method", "effect_class", "card_sha256"}
    keys |= ({"max_text_chars"} if method == "paste_frozen_text" else set()) | ({"scroll_target", "scroll_target_source", "scroll_alignment"} if method == "scroll_into_view" else set()) | ({"draft_sha256", "precondition_kind"} if method == "submit_frozen_comment" else set()) | ({"selected_activity", "selected_post_body_sha256", "postcondition_kind"} if method in SEMANTIC_OUTWARD or method == "paste_frozen_text" else set())
    if (set(card) != keys or card.get("schema") != "taey_revenue_ui_operation_card_v1"
            or method not in DECLARED_EFFECTS or card.get("effect_class") != DECLARED_EFFECTS[method]
            or (method == "scroll_into_view" and (
                _SCROLL_TARGET.fullmatch(str(card.get("scroll_target") or "")) is None
                or card.get("scroll_target_source") not in {"self", "mapped_context"}
                or card.get("scroll_alignment") not in {"anywhere", "top_edge"}
            ))
            or card.get("card_sha256") != canonical_sha256(payload)):
        raise ValueError("revenue UI operation card hash is not exact")
    return card
def semantic_input(manifest: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    validate_operation_card(card)
    if card["method"] not in SEMANTIC_OUTWARD:
        raise ValueError("private semantic input requires an outward operation")
    return {
        "schema": "taey_revenue_ui_semantic_input_v1", "manifest_sha256": manifest["transaction_sha256"],
        "card_sha256": card["card_sha256"], "operation": card["method"], "display": manifest["display"],
        "selected_activity": manifest["selected_activity"], "selected_post_body_sha256": manifest["selected_post_body_sha256"],
        "like_authorized": manifest["like_authorized"], "expected_text": manifest["text"],
        "expected_text_sha256": manifest["text_sha256"], "expected_author_name": manifest["expected_author_name"],
    }
def parse_semantic_input(raw: bytes, digest: str, *, card: dict[str, Any], display: str) -> dict[str, Any]:
    if (not raw or len(raw) > 1024 * 1024 or not _SHA256.fullmatch(digest) or hashlib.sha256(raw).hexdigest() != digest):
        raise ValueError("semantic input is not bounded and hash-bound")
    def exact(pairs: list[tuple[str, object]]) -> dict[str, object]:
        if len(pairs) != len({key for key, _ in pairs}):
            raise ValueError("duplicate semantic input key")
        return dict(pairs)
    value = json.loads(raw.decode(), object_pairs_hook=exact)
    keys = {"schema", "manifest_sha256", "card_sha256", "operation", "display", "selected_activity",
            "selected_post_body_sha256", "like_authorized", "expected_text", "expected_text_sha256", "expected_author_name"}
    text, author = ((value.get("expected_text"), value.get("expected_author_name")) if isinstance(value, dict) else (None, None))
    if (not isinstance(value, dict) or set(value) != keys
            or value.get("schema") != "taey_revenue_ui_semantic_input_v1"
            or value.get("operation") != card.get("method") or any(value.get(key) != card.get(key) for key in ("card_sha256", "selected_activity", "selected_post_body_sha256"))
            or value.get("display") != display or not _SHA256.fullmatch(str(value.get("manifest_sha256")))
            or not isinstance(value.get("like_authorized"), bool)
            or not isinstance(text, str) or not text or "\x00" in text
            or hashlib.sha256(text.encode()).hexdigest() != value.get("expected_text_sha256")
            or not isinstance(author, str) or not author or author != author.strip()):
        raise ValueError("semantic input does not match the fresh operation card")
    return value
def validate_operation_evidence(*, card: dict[str, Any], manifest: dict[str, Any], precondition: dict[str, Any] | None, postcondition: dict[str, Any], precondition_sha256: str | None, postcondition_sha256: str) -> None:
    validate_operation_card(card)
    if (len(canonical_json_bytes(precondition)) > 65536 or len(canonical_json_bytes(postcondition)) > 65536
            or precondition_sha256 != (canonical_sha256(precondition) if precondition else None)
            or postcondition_sha256 != canonical_sha256(postcondition)):
        raise ValueError("operation evidence is not bounded and hash-bound")
    method = card["method"]
    text = manifest.get("expected_text", manifest.get("text"))
    text_sha = manifest.get("expected_text_sha256", manifest.get("text_sha256"))
    common = {"element_key": card["element"], "operation": method, "effect_class": card["effect_class"],
              "postcondition": card["postcondition_kind"], "route_exact": True, "activity_exact": True,
              "selected_post_body_sha256": card["selected_post_body_sha256"]}
    specific = ({"editor_text_sha256": text_sha, "editor_text_chars": len(text)} if method == "paste_frozen_text"
                else {"reaction_state": "liked"} if method == "activate_optional_like"
                else {"editor_empty": True, "exact_own_comment_count": 1, "comment_text_sha256": text_sha, "comment_text_chars": len(text)})
    expected = common | specific
    if (method == "activate_optional_like" and manifest.get("like_authorized") is not True
            or set(postcondition) != set(expected) | {"activity_sources", "observed_url"}
            or any(postcondition.get(key) != value for key, value in expected.items())
            or not isinstance(postcondition.get("observed_url"), str)
            or not isinstance(postcondition.get("activity_sources"), list)
            or not all(isinstance(item, str) for item in postcondition["activity_sources"])):
        raise ValueError("postcondition evidence is not exact")
    if method != "submit_frozen_comment":
        if precondition is not None or precondition_sha256 is not None:
            raise ValueError("operation has unexpected precondition evidence")
        return
    pre_expected = {"element_key": card["element"], "operation": method, "effect_class": "outward",
                    "precondition": card["precondition_kind"], "route_exact": True, "activity_exact": True,
                    "body_sha256_exact": True, "draft_sha256": text_sha, "draft_chars": len(text),
                    "existing_exact_own_comment_count": 0}
    if (not isinstance(precondition, dict) or set(precondition) != set(pre_expected) | {"own_comment_control_sha256"}
            or any(precondition.get(key) != value for key, value in pre_expected.items())
            or not _SHA256.fullmatch(str(precondition.get("own_comment_control_sha256")))):
        raise ValueError("submit precondition evidence is not exact")
def semantic_receipt(*, card: dict[str, Any], private: dict[str, Any], barrier: dict[str, Any], postcondition: dict[str, Any], precondition: dict[str, Any] | None) -> dict[str, Any]:
    submit = card["method"] == "submit_frozen_comment"
    pre_sha = canonical_sha256(precondition) if precondition else None
    post_sha = canonical_sha256(postcondition)
    validate_operation_evidence(card=card, manifest=private, precondition=precondition, postcondition=postcondition,
                                precondition_sha256=pre_sha, postcondition_sha256=post_sha)
    flags = {"next_mutation_authorized": not submit, "observe_required_before_next_mutation": not submit,
             "terminal_delivery_verified": submit}
    if barrier.get("result") != "PASS" or any(barrier.get(key) is not value for key, value in flags.items()):
        raise ValueError("semantic barrier flags are not exact")
    return {"schema": "taey_revenue_ui_semantic_receipt_v1", "card_sha256": card["card_sha256"],
            "manifest_sha256": private["manifest_sha256"], "operation": card["method"], "performed_primitive": "atspi_activate",
            **flags, "text_sha256": private["expected_text_sha256"],
            "author_name_sha256": hashlib.sha256(private["expected_author_name"].encode()).hexdigest(),
            "precondition": precondition, "postcondition": postcondition,
            "precondition_sha256": pre_sha, "postcondition_sha256": post_sha}
def validate_semantic_receipt(receipt: dict[str, Any], *, card: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    submit = card["method"] == "submit_frozen_comment"
    expected = {"schema": "taey_revenue_ui_semantic_receipt_v1", "card_sha256": card.get("card_sha256"),
                "manifest_sha256": manifest.get("transaction_sha256"), "operation": card.get("method"),
                "performed_primitive": "atspi_activate", "next_mutation_authorized": not submit,
                "observe_required_before_next_mutation": not submit, "terminal_delivery_verified": submit,
                "text_sha256": manifest.get("text_sha256"), "author_name_sha256": manifest.get("expected_author_name_sha256")}
    evidence_keys = {"precondition", "postcondition", "precondition_sha256", "postcondition_sha256"}
    if (not isinstance(receipt, dict) or set(receipt) != set(expected) | evidence_keys
            or any(receipt.get(key) != value for key, value in expected.items())):
        raise ValueError("semantic receipt is not bound to the card and manifest")
    validate_operation_evidence(card=card, manifest=manifest, precondition=receipt["precondition"],
                                postcondition=receipt["postcondition"], precondition_sha256=receipt["precondition_sha256"],
                                postcondition_sha256=receipt["postcondition_sha256"])
    return receipt
