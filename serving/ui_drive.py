#!/home/mira/taeys-env-sys/bin/python
"""One-invocation AT-SPI observation and action CLI for raw X displays."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import importlib.util
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


# The AT-SPI primitives live in the PUBLIC palios-taey/taeys-hands repo
# (consultation_v2/: platforms_runtime, primitives, atspi, clipboard, input, interact,
# firefox_chrome.yaml — all tracked there). Point TAEYS_HANDS_ROOT at your clone; the
# default is this operator's checkout. A downloaded Taey sets the env var and this works
# unmodified — no private path is on this dependency chain.
TAEYS_HANDS = os.environ.get("TAEYS_HANDS_ROOT", "/home/mira/taeys-hands")
if TAEYS_HANDS not in sys.path:
    sys.path.insert(0, TAEYS_HANDS)

try:
    from consultation_v2.platforms import routing as platform_routing
    from consultation_v2.platforms_runtime import display_environment
    from consultation_v2.planner import (
        selection_path_operation,
        selection_trigger_operation,
    )
    from consultation_v2.native_dialog_snapshot import build_native_dialog_snapshot
    from consultation_v2.runtime import ConsultationRuntime
    from consultation_v2.snapshot import (
        build_app_root_snapshot,
        build_menu_snapshot,
        build_snapshot,
    )
    from consultation_v2.types import ElementRef, Snapshot
    from consultation_v2.yaml_contract import (
        CHAT_PLATFORMS,
        get_extraction,
        load_platform_yaml,
    )
except ImportError as exc:  # fail LOUD and actionable, never a bare traceback
    sys.stderr.write(
        f"ui_drive: cannot import consultation_v2 from {TAEYS_HANDS!r}: {exc}\n"
        "Set TAEYS_HANDS_ROOT to a clone of https://github.com/palios-taey/taeys-hands\n"
    )
    raise

# --- per-display dispatch lease ---------------------------------------------------------------
# The Hands primitive owns the one physical-display mutex. ui_drive may not substitute another
# key or proceed when that dependency is unavailable. The owner arrives only from soma_proxy's
# validated request context, never from model arguments.
from consultation_v2.primitives import (
    display_lock_record as _display_lock_record,
    _plan_lock_key as _plan_lock_key,
)
from storage.redis_pool import get_client as _lock_redis_client


_MONITOR_TTL_DEFAULT = int(os.environ.get("TAEY_CONSULT_MONITOR_TTL", "10800"))
LOCK_TTL_DEFAULT = int(
    os.environ.get("TAEY_DRIVE_LOCK_TTL", str(_MONITOR_TTL_DEFAULT))
)
if LOCK_TTL_DEFAULT < _MONITOR_TTL_DEFAULT:
    raise RuntimeError(
        "TAEY_DRIVE_LOCK_TTL must be at least TAEY_CONSULT_MONITOR_TTL so display "
        "ownership survives the no-poll consultation wait"
    )


REF_PREFIX = "atspi3."
OBSERVE_SCOPES = ("base", "menu_snapshot", "app_root_snapshot")
OBSERVE_SURFACES = ("browser", "native_dialog")
_LEASE_OWNER_RE = re.compile(r"taey-drive:[A-Za-z0-9._-]{1,64}:[0-9a-f]{32}")
_PROCESS_GENERATION_RE = re.compile(r"[0-9a-f]{32}")
_TRACE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_GENERATION_FENCE_KEY_RE = re.compile(
    r"taey:soma:drive_process_generation:[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
)


class UiDriveError(RuntimeError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UiDriveError(message)


def _requested_option(argv: list[str], option: str) -> str | None:
    try:
        return argv[argv.index(option) + 1]
    except (ValueError, IndexError):
        return None


def _emit(
    *,
    ok: bool,
    action: str,
    display: str | None,
    platform: str | None,
    result: Any,
    error: str | None,
) -> None:
    print(
        json.dumps(
            {
                "ok": ok,
                "action": action,
                "display": display,
                "platform": platform,
                "result": result,
                "error": error,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _configure_display(display: str) -> SimpleNamespace:
    if not re.fullmatch(r":\d+", display or ""):
        raise UiDriveError(f"display must have raw :N form, got {display!r}")

    os.environ.pop("AT_SPI_BUS_ADDRESS", None)
    os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
    env = display_environment(display, base=os.environ)
    bus = env.get("AT_SPI_BUS_ADDRESS")
    if not bus:
        bus_file = Path(f"/tmp/a11y_bus_{display}")
        if bus_file.is_file():
            bus = bus_file.read_text(encoding="utf-8").strip()
            if bus:
                env["AT_SPI_BUS_ADDRESS"] = bus
    if not bus:
        raise UiDriveError(f"no AT-SPI bus resolved for display {display}")
    os.environ.update(env)

    from consultation_v2 import clipboard, input as ui_input, interact

    ui_input.set_display(display)
    clipboard.set_display(display)
    platform = _platform_for_display(env["DISPLAY"])
    return SimpleNamespace(
        display=env["DISPLAY"],
        platform=platform,
        clipboard=clipboard,
        input=ui_input,
        interact=interact,
    )


def _lease_context(*, required: bool = True) -> SimpleNamespace | None:
    owner = os.environ.get("TAEY_DRIVE_LEASE_OWNER", "")
    seat_id = os.environ.get("TAEY_DRIVE_LEASE_SEAT", "")
    turn_id = os.environ.get("TAEY_DRIVE_LEASE_TURN", "")
    process_generation = os.environ.get("TAEY_DRIVE_LEASE_GENERATION", "")
    generation_fence_key = os.environ.get(
        "TAEY_DRIVE_GENERATION_FENCE_KEY", ""
    )
    if not any(
        (owner, seat_id, turn_id, process_generation, generation_fence_key)
    ) and not required:
        return None
    if not _LEASE_OWNER_RE.fullmatch(owner):
        raise UiDriveError("missing or invalid proxy-issued display lease owner")
    if not _TRACE_ID_RE.fullmatch(seat_id):
        raise UiDriveError("missing or invalid proxy-issued display lease seat")
    if not _TRACE_ID_RE.fullmatch(turn_id):
        raise UiDriveError("missing or invalid proxy-issued display lease turn")
    if not _PROCESS_GENERATION_RE.fullmatch(process_generation):
        raise UiDriveError("missing or invalid proxy-issued process generation")
    if not _GENERATION_FENCE_KEY_RE.fullmatch(generation_fence_key):
        raise UiDriveError("missing or invalid proxy-issued generation fence key")
    expected_owner = f"taey-drive:{seat_id}:{process_generation}"
    if owner != expected_owner:
        raise UiDriveError("proxy-issued display lease identity is inconsistent")
    return SimpleNamespace(
        owner=owner,
        seat_id=seat_id,
        turn_id=turn_id,
        process_generation=process_generation,
        generation_fence_key=generation_fence_key,
    )


def _target_fingerprint(item: ElementRef, *, match_count: int) -> str:
    payload = {
        "description": item.description or "",
        "match_count": match_count,
        "name": item.name,
        "role": item.role,
        "text": item.text or "",
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_ref(
    *,
    display: str,
    platform: str,
    scope: str,
    revision: str,
    element: str,
    current_url: str | None,
    target_sha256: str,
    pick: str | None = None,
) -> str:
    descriptor = {
        "v": 6,
        "display": display,
        "platform": platform,
        "surface": "browser",
        "scope": scope,
        "revision": revision,
        "element": element,
        "url": current_url,
        "target_sha256": target_sha256,
    }
    if pick is not None:
        descriptor["pick"] = pick
    payload = json.dumps(
        descriptor,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return REF_PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_ref(value: str) -> dict[str, Any]:
    if not value.startswith(REF_PREFIX):
        raise UiDriveError(f"invalid ref prefix; expected {REF_PREFIX}")
    encoded = value[len(REF_PREFIX) :]
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
    except Exception as exc:
        raise UiDriveError(f"invalid ref encoding: {exc}") from exc
    version = payload.get("v")
    if version in {3, 4}:
        required = {"v", "display", "platform", "surface", "revision", "element"}
        if version == 4:
            required.add("pick")
        if set(payload) != required:
            raise UiDriveError("invalid ref schema")
        payload["scope"] = "base"
    elif version in {5, 6}:
        required = {
            "v", "display", "platform", "surface", "scope", "revision", "element",
        }
        if version == 6:
            required.update({"url", "target_sha256"})
        if "pick" in payload:
            required.add("pick")
        if set(payload) != required:
            raise UiDriveError("invalid ref schema")
    else:
        raise UiDriveError("invalid ref schema")
    if not re.fullmatch(r":\d+", payload.get("display") or ""):
        raise UiDriveError("invalid ref display")
    if payload.get("platform") not in CHAT_PLATFORMS:
        raise UiDriveError("invalid ref platform")
    if payload.get("surface") != "browser":
        raise UiDriveError("invalid ref surface")
    if payload.get("scope") not in OBSERVE_SCOPES:
        raise UiDriveError("invalid ref scope")
    if not re.fullmatch(r"[0-9a-f]{64}", payload.get("revision") or ""):
        raise UiDriveError("invalid ref revision")
    if not isinstance(payload.get("element"), str) or not payload["element"]:
        raise UiDriveError("invalid ref element")
    if version == 6:
        ref_url = payload.get("url")
        if ref_url is not None and (
            not isinstance(ref_url, str) or not ref_url
        ):
            raise UiDriveError("invalid ref URL")
        if not re.fullmatch(r"[0-9a-f]{64}", payload.get("target_sha256") or ""):
            raise UiDriveError("invalid ref target fingerprint")
    if "pick" in payload and payload.get("pick") != "last_by_y":
        raise UiDriveError("invalid ref pick strategy")
    return payload


def _platform_for_display(display: str) -> str:
    matches = sorted(
        platform
        for platform in CHAT_PLATFORMS
        if platform_routing.get_platform_display(platform) == display
    )
    if len(matches) != 1:
        raise UiDriveError(
            f"display {display} resolved to {matches}; expected exactly one Chat platform"
        )
    return matches[0]


def _scope_expected_elements(platform: str, scope: str) -> tuple[str, ...]:
    cfg = load_platform_yaml(platform)
    workflow = cfg.get("workflow") or {}
    selection = workflow.get("selection") or {}
    menus = selection.get("menus") or {}
    expected: set[str] = set()
    if isinstance(menus, dict):
        for menu in menus.values():
            if not isinstance(menu, dict):
                continue
            operate = menu.get("operate") or {}
            if not isinstance(operate, dict) or operate.get("scope") != scope:
                continue
            options = menu.get("options") or {}
            if not isinstance(options, dict):
                continue
            for option in options.values():
                if not isinstance(option, dict):
                    continue
                element = option.get("element")
                if isinstance(element, str) and element:
                    expected.add(element)
                for step in option.get("path") or []:
                    if not isinstance(step, dict):
                        continue
                    path_element = step.get("element")
                    if isinstance(path_element, str) and path_element:
                        expected.add(path_element)
    attachment = workflow.get("attachment") or {}
    if isinstance(attachment, dict) and attachment.get("scope") == scope:
        menu_target = attachment.get("menu_target")
        if isinstance(menu_target, str) and menu_target:
            expected.add(menu_target)
    if not expected:
        raise UiDriveError(
            f"{platform} YAML does not declare observation scope {scope!r}"
        )
    return tuple(sorted(expected))


def _snapshot(deps: SimpleNamespace, *, scope: str = "base") -> Snapshot:
    builders = {
        "base": build_snapshot,
        "menu_snapshot": build_menu_snapshot,
    }
    if scope == "app_root_snapshot":
        expected = _scope_expected_elements(deps.platform, scope)
        snapshot = build_app_root_snapshot(deps.platform)
        snapshot.mapped = {
            key: list(snapshot.mapped.get(key) or [])
            for key in expected
            if snapshot.mapped.get(key)
        }
        snapshot.unknown = []
        snapshot.sidebar = []
        snapshot.menu_items = []
    else:
        builder = builders.get(scope)
        if builder is None:
            raise UiDriveError(
                f"unsupported observation scope {scope!r}; expected one of "
                f"{list(OBSERVE_SCOPES)}"
            )
        _firefox, _document, snapshot = builder(deps.platform)
    if snapshot.platform != deps.platform:
        raise UiDriveError(
            f"snapshot platform {snapshot.platform!r} does not match bound {deps.platform!r}"
        )
    if scope != "base":
        expected = _scope_expected_elements(deps.platform, scope)
        present = [key for key in expected if snapshot.mapped.get(key)]
        if not present:
            raise UiDriveError(
                f"{deps.platform} {scope} contains none of its YAML-declared mapped "
                f"options {list(expected)}; refusing base-scope fallback"
            )
    return snapshot


def _snapshot_revision(snapshot: Snapshot, *, scope: str = "base") -> str:
    mapped: dict[str, Any] = {}
    for element_key in sorted(snapshot.mapped):
        items = list(snapshot.mapped.get(element_key) or [])
        if not items:
            continue
        matches = sorted(
            (
                {
                    "name": item.name,
                    "role": item.role,
                    "x": item.x,
                    "y": item.y,
                    "states": sorted(set(item.states)),
                    "text": item.text,
                    "text_selections": item.raw.get("text_selections") or [],
                }
                for item in items
            ),
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        )
        mapped[element_key] = {
            "match_count": len(items),
            "matches": matches,
        }
    payload = {
        "scope": scope,
        "current_url": snapshot.url,
        "mapped": mapped,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_at_expected_revision(
    args: argparse.Namespace,
    deps: SimpleNamespace,
) -> Snapshot:
    expected = str(getattr(args, "expected_revision", "") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise UiDriveError(
            "mutation requires --expected-revision from the preceding explicit observe"
        )
    scope = str(getattr(args, "expected_scope", "base") or "base")
    if scope not in OBSERVE_SCOPES:
        raise UiDriveError(
            f"unsupported expected snapshot scope {scope!r}; expected one of "
            f"{list(OBSERVE_SCOPES)}"
        )
    snapshot = _snapshot(deps, scope=scope)
    actual = _snapshot_revision(snapshot, scope=scope)
    if actual != expected:
        raise UiDriveError(
            "browser tree changed after the preceding observe; observe again before acting"
        )
    return snapshot


def _snapshot_for_key_or_type(
    args: argparse.Namespace,
    deps: SimpleNamespace,
) -> Snapshot | None:
    native_dialog_revision = str(
        getattr(args, "native_dialog_revision", "") or ""
    )
    if native_dialog_revision:
        if getattr(args, "expected_revision", None):
            raise UiDriveError(
                "native-dialog mutation must not also provide a browser snapshot revision"
            )
        snapshot = build_native_dialog_snapshot(deps.platform)
        snapshot.assert_revision(native_dialog_revision)
        return None
    return _snapshot_at_expected_revision(args, deps)


def _public_element(
    item: ElementRef,
    *,
    category: str,
    element: str | None = None,
    match_count: int = 1,
    ref: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "category": category,
        "element": element,
        "name": item.name,
        "role": item.role,
        "states": list(item.states),
        "match_count": match_count,
    }
    if item.text:
        payload["text"] = item.text
    text_selections = item.raw.get("text_selections") or []
    if text_selections:
        payload["text_selections"] = text_selections
    if item.description:
        payload["description"] = item.description
    if ref is not None:
        payload["ref"] = ref
    return payload


# ---------------------------------------------------------------------------
# YAML-DRIVEN ELEMENT RESOLUTION.
#
# Every platform's UI differs — the attach control is "Add files and more" on
# ChatGPT, "Add files, connectors, and more" on Claude, "Upload & tools" on
# Gemini, "Attach" on Grok. Those names, roles, menu targets and open-methods are
# ALREADY DEFINED, per platform, in consultation_v2/platforms/<p>/<p>.yaml under
# tree.element_map and workflow.*. Hardcoding them into a caller's instructions
# is how a sequence proven on one platform silently fails on the next.
#
# So a caller names an ELEMENT KEY (attach_trigger, composer_input, send_button…)
# and this resolves it from THAT display's platform YAML. One sequence, every
# platform, no hardcoded names anywhere.
# ---------------------------------------------------------------------------
def _platform_config(display: str) -> dict:
    platform = _platform_for_display(display)
    try:
        return load_platform_yaml(platform)
    except Exception as exc:
        raise UiDriveError(f"cannot load strict platform YAML for {platform}: {exc}") from exc


def _yaml_pick_strategy(cfg: dict, element_key: str) -> str | None:
    workflow = cfg.get("workflow") or {}
    declared: set[str] = set()

    full_consult = workflow.get("full_consult") or {}
    attachment_present = (
        full_consult.get("attachment_present")
        if isinstance(full_consult, dict)
        else None
    )
    if (
        isinstance(attachment_present, dict)
        and element_key in (attachment_present.get("elements") or [])
    ):
        pick = attachment_present.get("pick")
        if isinstance(pick, str) and pick:
            declared.add(pick)

    consult_steps = full_consult.get("steps") if isinstance(full_consult, dict) else {}
    if isinstance(consult_steps, dict):
        for step in consult_steps.values():
            if not isinstance(step, dict):
                continue
            elements = step.get("elements") or []
            if element_key not in elements:
                continue
            pick = step.get("pick")
            if isinstance(pick, str) and pick:
                declared.add(pick)

    extract = workflow.get("extract") or {}
    if (
        isinstance(extract, dict)
        and extract.get("primary_key") == element_key
    ):
        strategy = extract.get("strategy")
        if isinstance(strategy, str) and strategy:
            declared.add(strategy)

    if len(declared) > 1:
        raise UiDriveError(
            f"{cfg.get('platform')}: conflicting YAML pick strategies for "
            f"{element_key!r}: {sorted(declared)}"
        )
    if not declared:
        return None
    strategy = next(iter(declared))
    if strategy != "last_by_y":
        raise UiDriveError(
            f"{cfg.get('platform')}: unsupported YAML pick strategy "
            f"{strategy!r} for {element_key!r}"
        )
    return strategy


def _selected_mapped_item(
    cfg: dict,
    element_key: str,
    items: list[ElementRef],
) -> tuple[ElementRef | None, str | None]:
    if not items:
        return None, None
    strategy = _yaml_pick_strategy(cfg, element_key)
    if strategy is None:
        return (items[0], None) if len(items) == 1 else (None, None)
    if any(item.y is None for item in items):
        raise UiDriveError(
            f"{cfg.get('platform')}: YAML {strategy} for {element_key!r} "
            "requires a y coordinate on every exact tree match"
        )
    max_y = max(int(item.y) for item in items if item.y is not None)
    selected = [item for item in items if item.y == max_y]
    if len(selected) != 1:
        raise UiDriveError(
            f"{cfg.get('platform')}: YAML {strategy} for {element_key!r} "
            f"resolved {len(selected)} matches at y={max_y}; expected one"
        )
    return selected[0], strategy


def _attach_grammar(display: str) -> dict:
    """The platform's ATTACH grammar, entirely from its YAML.

    Even the element KEY differs per platform (chatgpt/grok/perplexity use
    attach_trigger; gemini uses upload_menu; claude uses its own), which is why
    workflow.attachment names the keys rather than the caller guessing them.
    Returns resolved specs plus the open-method, so one caller sequence drives
    every platform.
    """
    cfg = _platform_config(display)
    att = ((cfg.get("workflow") or {}).get("attachment") or {})
    emap = ((cfg.get("tree") or {}).get("element_map") or {})
    trig_key = att.get("trigger")
    targ_key = att.get("menu_target")
    if not isinstance(trig_key, str) or not trig_key:
        raise UiDriveError(f"{cfg.get('platform')}: workflow.attachment.trigger is required")
    if not isinstance(targ_key, str) or not targ_key:
        raise UiDriveError(f"{cfg.get('platform')}: workflow.attachment.menu_target is required")
    out = {
        "platform": str(cfg.get("platform") or ""),
        "trigger_key": trig_key,
        "target_key": targ_key,
        "open_method": att.get("open_method"),
        "open_key": att.get("open_key"),
        "typeahead_label": att.get("typeahead_label"),
        "typeahead_submit_keys": att.get("typeahead_submit_keys"),
        "trigger": emap.get(trig_key),
        "target": emap.get(targ_key),
    }
    if not out["trigger"]:
        raise UiDriveError(
            f"{out['platform']}: workflow.attachment.trigger={trig_key!r} has no "
            f"element_map entry. YAML is the source of truth — fix it there, not here.")
    if not out["target"]:
        raise UiDriveError(
            f"{out['platform']}: workflow.attachment.menu_target={targ_key!r} has no "
            f"element_map entry. YAML is the source of truth — fix it there, not here.")
    return out


def _resolve_target(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    if not args.ref:
        raise UiDriveError("target requires --ref from the immediately preceding observe")
    descriptor = _decode_ref(args.ref)
    if descriptor["display"] != deps.display:
        raise UiDriveError(
            f"ref is scoped to display {descriptor['display']}, not {deps.display}"
        )
    if descriptor["platform"] != deps.platform:
        raise UiDriveError(
            f"ref is scoped to platform {descriptor['platform']}, not {deps.platform}"
        )
    scope = descriptor["scope"]
    snapshot = _snapshot(deps, scope=scope)
    revision = _snapshot_revision(snapshot, scope=scope)
    if descriptor["v"] < 6 and descriptor["revision"] != revision:
        raise UiDriveError(
            "stale browser snapshot ref; observe again and decide from the fresh tree"
        )
    element_key = descriptor["element"]
    matches = list(snapshot.mapped.get(element_key) or [])
    cfg = _platform_config(deps.display)
    item, pick = _selected_mapped_item(cfg, element_key, matches)
    if item is None:
        raise UiDriveError(
            f"mapped element {element_key!r} matched {len(matches)} elements on "
            f"{deps.platform} {deps.display} without one YAML-selected target"
        )
    if descriptor.get("pick") != pick:
        raise UiDriveError(
            f"ref pick strategy {descriptor.get('pick')!r} does not match current "
            f"YAML strategy {pick!r} for {element_key!r}"
        )
    target_sha256 = _target_fingerprint(item, match_count=len(matches))
    if descriptor["v"] == 6:
        if descriptor["url"] != snapshot.url:
            raise UiDriveError(
                "browser URL changed after the preceding observe; observe again before acting"
            )
        if descriptor["target_sha256"] != target_sha256:
            raise UiDriveError(
                "mapped browser target changed after the preceding observe; observe again before acting"
            )
    return {
        **dict(item.raw),
        "element": element_key,
        "ref": _encode_ref(
            display=deps.display,
            platform=deps.platform,
            scope=scope,
            revision=revision,
            element=element_key,
            current_url=snapshot.url,
            target_sha256=target_sha256,
            pick=pick,
        ),
    }


def _manual_ui_module(platform: str) -> Any | None:
    module_name = f"consultation_v2.platforms.{platform}.manual"
    try:
        if importlib.util.find_spec(module_name) is None:
            return None
    except ModuleNotFoundError:
        return None
    return importlib.import_module(module_name)


def _declared_operation(
    platform: str,
    element_key: str,
    item: ElementRef | dict[str, Any],
) -> dict[str, Any] | None:
    manual = _manual_ui_module(platform)
    if isinstance(item, ElementRef):
        states = list(item.states)
        context = dict(item.raw or {})
        if item.text is not None:
            context["text"] = item.text
    else:
        states = list(item.get("states") or [])
        context = dict(item)
    manual_declared = None
    if manual is not None:
        manual_declared = manual.element_operation(element_key, states, context)
        if manual_declared is not None and not isinstance(manual_declared, dict):
            raise UiDriveError(
                f"{platform} manual element_operation must return a mapping or null"
            )

    trigger_declared = selection_trigger_operation(
        platform,
        element_key,
        states,
    )
    if manual_declared is not None and trigger_declared is not None:
        comparable = ("method", "primitives", "allowed_now")
        mismatches = [
            field
            for field in comparable
            if manual_declared.get(field) != trigger_declared.get(field)
        ]
        if mismatches:
            raise UiDriveError(
                f"{platform} element {element_key!r} conflicts between "
                f"platform-manual and YAML menu-open operations at {mismatches}"
            )
    declared = manual_declared or trigger_declared
    action = selection_path_operation(platform, element_key)
    if declared is not None and action is not None:
        if action not in (declared.get("primitives") or []):
            raise UiDriveError(
                f"{platform} element {element_key!r} conflicts between declared "
                f"operation {declared.get('method')!r} and YAML selection "
                f"path action {action!r}"
            )
        return declared
    if declared is not None:
        return declared
    if action is None:
        return None
    if action not in {"click", "hover"}:
        raise UiDriveError(
            f"{platform} selection path action {action!r} for {element_key!r} "
            "has no drive_chat primitive"
        )
    return {
        "method": "selection_path",
        "primitives": [action],
        "allowed_now": [action],
        "forbidden": sorted({"activate", "click", "focus", "hover"} - {action}),
    }


def _observe(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    if args.surface == "native_dialog":
        if args.scope != "base":
            raise UiDriveError(
                "native-dialog observe does not accept a browser observation scope"
            )
        snapshot = build_native_dialog_snapshot(deps.platform)
        mapped = [
            {
                "category": "mapped",
                "element": key,
                "name": item.name,
                "role": item.role,
                "states": list(item.states),
                "scope": item.scope,
                "path": item.path,
                **({"text": item.text} if item.text is not None else {}),
                "match_count": len(items),
            }
            for key in sorted(snapshot.mapped)
            for items in (snapshot.mapped[key],)
            for item in items
        ]
        return {
            "platform": snapshot.platform,
            "surface": snapshot.surface,
            "scope": "native_dialog",
            "snapshot_revision": snapshot.revision,
            "contract_sha256": snapshot.contract_sha256,
            "root_key": snapshot.root_key,
            "raw_count": snapshot.raw_count,
            "counts": {"mapped": len(mapped)},
            "mapped": mapped,
        }

    scope = args.scope
    snapshot = _snapshot(deps, scope=scope)
    revision = _snapshot_revision(snapshot, scope=scope)
    expected_scope_elements = (
        list(_scope_expected_elements(deps.platform, scope))
        if scope != "base"
        else []
    )
    cfg = _platform_config(deps.display)
    mapped: list[dict[str, Any]] = []
    for element_key in sorted(snapshot.mapped):
        items = list(snapshot.mapped.get(element_key) or [])
        selected, pick = _selected_mapped_item(cfg, element_key, items)
        for item in items:
            ref = None
            if item is selected:
                ref = _encode_ref(
                    display=deps.display,
                    platform=deps.platform,
                    scope=scope,
                    revision=revision,
                    element=element_key,
                    current_url=snapshot.url,
                    target_sha256=_target_fingerprint(
                        item,
                        match_count=len(items),
                    ),
                    pick=pick,
                )
            public_item = _public_element(
                item,
                category="mapped",
                element=element_key,
                match_count=len(items),
                ref=ref,
            )
            if pick is not None:
                public_item["yaml_selection"] = {
                    "strategy": pick,
                    "selected": item is selected,
                }
            if item is selected:
                declared = _declared_operation(
                    deps.platform, element_key, item
                )
                if declared is not None:
                    public_item["declared_operation"] = declared
            mapped.append(public_item)

    unknown = [
        _public_element(item, category="unknown") for item in snapshot.unknown
    ]
    sidebar = [
        _public_element(item, category="sidebar") for item in snapshot.sidebar
    ]
    menu_items = [
        _public_element(item, category="menu_item") for item in snapshot.menu_items
    ]
    monitor = ((cfg.get("workflow") or {}).get("monitor") or {})
    stop_keys = monitor.get("stop_keys")
    if stop_keys is None:
        stop_key = monitor.get("stop_key")
        stop_keys = [stop_key] if isinstance(stop_key, str) and stop_key else None
    if not isinstance(stop_keys, list) or not all(
        isinstance(key, str) and key for key in stop_keys
    ):
        raise UiDriveError(
            f"{deps.platform}: workflow.monitor requires stop_keys or stop_key"
        )
    fresh_url = ((cfg.get("urls") or {}).get("fresh"))
    if not isinstance(fresh_url, str) or not fresh_url:
        raise UiDriveError(f"{deps.platform}: urls.fresh must be a non-empty string")
    return {
        "platform": deps.platform,
        "surface": "browser",
        "scope": scope,
        "scope_expected_elements": expected_scope_elements,
        "snapshot_revision": revision,
        "current_url": snapshot.url,
        "fresh_url": fresh_url,
        "stop_keys": stop_keys,
        "raw_count": snapshot.raw_count,
        "counts": {
            "mapped": len(mapped),
            "unknown": len(unknown),
            "sidebar": len(sidebar),
            "menu_items": len(menu_items),
        },
        "mapped": mapped,
        "unknown": unknown,
        "sidebar": sidebar,
        "menu_items": menu_items,
    }


def _element_action(
    action: str, args: argparse.Namespace, deps: SimpleNamespace
) -> dict[str, Any]:
    row = _resolve_target(args, deps)
    declared = _declared_operation(
        deps.platform, row["element"], row
    )
    if action == "operate":
        if declared is None:
            raise UiDriveError(
                f"{row['element']} has no YAML-declared operation; use one exact "
                "direct primitive"
            )
        if declared.get("method") == "focus_and_key_open":
            return _focus_and_key_open_operation(row, declared, deps)
        if declared.get("method") == "mapped_pointer_activate":
            return _mapped_pointer_activate_operation(row, declared, deps)
        allowed_now = declared.get("allowed_now")
        if not isinstance(allowed_now, list) or len(allowed_now) != 1:
            raise UiDriveError(
                f"{row['element']} YAML operation is not singular in the fresh "
                f"state (allowed_now={allowed_now!r})"
            )
        performed_primitive = allowed_now[0]
    else:
        if declared is not None:
            raise UiDriveError(
                f"{row['element']} has YAML-declared operation "
                f"{declared['method']!r}; use operate with this exact ref"
            )
        performed_primitive = action
    if not isinstance(performed_primitive, str) or not performed_primitive:
        raise UiDriveError(
            f"{row['element']} YAML operation must be a non-empty string"
        )
    if performed_primitive == "click":
        performed = deps.interact.atspi_click(row)
    elif performed_primitive == "focus":
        performed = deps.interact.atspi_focus(row)
    elif performed_primitive == "activate":
        performed = deps.interact.atspi_activate(row)
    elif performed_primitive == "hover":
        performed = (
            deps.input.hover(int(row["x"]), int(row["y"]))
            if row.get("x") is not None and row.get("y") is not None
            else False
        )
    elif performed_primitive.startswith("key:"):
        key = performed_primitive.partition(":")[2]
        performed = bool(key) and _xdo_key(deps.display, key)
    elif performed_primitive.startswith("paste:"):
        text = performed_primitive.partition(":")[2]
        performed = bool(text) and deps.input.clipboard_paste(text)
    else:
        raise UiDriveError(
            f"{row['element']} YAML operation {performed_primitive!r} has no "
            "drive_chat primitive"
        )
    if not performed:
        raise UiDriveError(f"{performed_primitive} primitive returned false")
    return {
        "performed": True,
        "performed_primitive": performed_primitive,
        "element": {
            "category": "mapped",
            "element": row["element"],
            "name": str(row.get("name") or ""),
            "role": str(row.get("role") or ""),
            "states": list(row.get("states") or []),
            "ref": row["ref"],
        },
    }


def _scroll_to_bottom_action(
    args: argparse.Namespace, deps: SimpleNamespace
) -> dict[str, Any]:
    row = _resolve_target(args, deps)
    workflow = get_extraction(deps.platform, "assistant_text")
    if workflow is None or not workflow.steps:
        raise UiDriveError(
            f"{deps.platform}: extraction.assistant_text has no executable steps"
        )
    step = workflow.steps[0]
    if step.action != "scroll_to_bottom" or step.element != row["element"]:
        raise UiDriveError(
            f"{deps.platform}: extraction.assistant_text first step is not exact "
            f"scroll_to_bottom for {row['element']!r}"
        )
    runtime = ConsultationRuntime(deps.platform)
    anchor = ElementRef(
        key=str(row["element"]),
        name=str(row.get("name") or ""),
        role=str(row.get("role") or ""),
        x=row.get("x"),
        y=row.get("y"),
        states=list(row.get("states") or []),
        text=row.get("text"),
        description=row.get("description"),
        atspi_obj=row.get("atspi_obj"),
        raw=row,
    )
    if not runtime.scroll_to_bottom(anchor):
        raise UiDriveError(
            f"{deps.platform}: scroll_to_bottom primitive returned false"
        )
    return {
        "performed": True,
        "performed_primitive": "scroll_to_bottom",
        "yaml_extraction": {
            "output_type": "assistant_text",
            "step_index": 0,
            "action": step.action,
            "element": step.element,
        },
        "element": {
            "category": "mapped",
            "element": row["element"],
            "name": str(row.get("name") or ""),
            "role": str(row.get("role") or ""),
            "states": list(row.get("states") or []),
            "ref": row["ref"],
        },
    }


def _mapped_pointer_activate_operation(
    row: dict[str, Any],
    declared: dict[str, Any],
    deps: SimpleNamespace,
) -> dict[str, Any]:
    primitives = declared.get("primitives")
    if primitives != ["mapped_pointer_activate"]:
        raise UiDriveError(
            f"{row['element']} mapped_pointer_activate requires exact "
            "['mapped_pointer_activate'] primitives"
        )
    allowed_now = declared.get("allowed_now")
    if allowed_now == []:
        raise UiDriveError(
            f"{row['element']} mapped_pointer_activate is already expanded; refusing toggle"
        )
    if allowed_now != ["mapped_pointer_activate"]:
        raise UiDriveError(
            f"{row['element']} mapped_pointer_activate has unexpected live state "
            f"(allowed_now={allowed_now!r})"
        )

    runtime = ConsultationRuntime(deps.platform)
    evidence = runtime.mapped_pointer_activate(
        ElementRef(
            key=str(row["element"]),
            name=str(row.get("name") or ""),
            role=str(row.get("role") or ""),
            x=None,
            y=None,
            states=list(row.get("states") or []),
            text=row.get("text"),
            description=row.get("description"),
            atspi_obj=row.get("atspi_obj"),
            raw={},
        )
    )
    if evidence.get("ok") is not True:
        raise UiDriveError(
            f"{row['element']} mapped_pointer_activate failed: "
            + json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        )
    return {
        "performed": True,
        "performed_primitive": "mapped_pointer_activate",
        "performed_operation": "mapped_pointer_activate",
        "performed_primitives": list(primitives),
        "operation_evidence": evidence,
        "element": {
            "category": "mapped",
            "element": row["element"],
            "name": str(row.get("name") or ""),
            "role": str(row.get("role") or ""),
            "states": list(row.get("states") or []),
            "ref": row["ref"],
        },
    }


def _focus_and_key_open_operation(
    row: dict[str, Any],
    declared: dict[str, Any],
    deps: SimpleNamespace,
) -> dict[str, Any]:
    primitives = declared.get("primitives")
    if (
        not isinstance(primitives, list)
        or len(primitives) != 2
        or primitives[0] != "focus"
        or not isinstance(primitives[1], str)
        or not primitives[1].startswith("key:")
    ):
        raise UiDriveError(
            f"{row['element']} focus_and_key_open requires exact "
            f"['focus', 'key:<open_key>'] primitives"
        )
    allowed_now = declared.get("allowed_now")
    if allowed_now == []:
        raise UiDriveError(
            f"{row['element']} focus_and_key_open is already expanded; refusing toggle"
        )
    if allowed_now not in (["focus"], [primitives[1]]):
        raise UiDriveError(
            f"{row['element']} focus_and_key_open has unexpected live state "
            f"(allowed_now={allowed_now!r})"
        )
    open_key = primitives[1].partition(":")[2]
    if not open_key:
        raise UiDriveError(
            f"{row['element']} focus_and_key_open has an empty open key"
        )

    runtime = ConsultationRuntime(deps.platform)
    evidence = runtime.focus_and_key_open(
        ElementRef(
            key=str(row["element"]),
            name=str(row.get("name") or ""),
            role=str(row.get("role") or ""),
            x=row.get("x"),
            y=row.get("y"),
            states=list(row.get("states") or []),
            text=row.get("text"),
            description=row.get("description"),
            atspi_obj=row.get("atspi_obj"),
            raw=row,
        ),
        key=open_key,
    )
    if evidence.get("ok") is not True:
        raise UiDriveError(
            f"{row['element']} focus_and_key_open failed: "
            + json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        )
    return {
        "performed": True,
        "performed_primitive": "focus_and_key_open",
        "performed_operation": "focus_and_key_open",
        "performed_primitives": list(primitives),
        "operation_evidence": evidence,
        "element": {
            "category": "mapped",
            "element": row["element"],
            "name": str(row.get("name") or ""),
            "role": str(row.get("role") or ""),
            "states": list(row.get("states") or []),
            "ref": row["ref"],
        },
    }


def _navigate_fresh(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    cfg = _platform_config(deps.display)
    fresh_url = ((cfg.get("urls") or {}).get("fresh"))
    if not isinstance(fresh_url, str) or not fresh_url:
        raise UiDriveError(f"{deps.platform}: urls.fresh must be a non-empty string")
    if args.url != fresh_url:
        raise UiDriveError(
            f"{deps.platform}: navigate accepts only the exact YAML urls.fresh "
            f"value {fresh_url!r}"
        )
    before = _snapshot(deps)
    before_revision = _snapshot_revision(before)
    runtime = ConsultationRuntime(deps.platform)
    if not runtime.navigate(fresh_url, verify_change=True):
        raise UiDriveError(
            f"{deps.platform}: verified navigation to YAML urls.fresh failed"
        )
    after = _snapshot(deps)
    mapped_count = sum(len(items) for items in (after.mapped or {}).values())
    if int(after.raw_count or 0) < 1 or mapped_count < 1:
        raise UiDriveError(
            f"{deps.platform}: navigation reached no populated canonical tree"
        )
    return {
        "navigated": True,
        "target_url": fresh_url,
        "current_url": after.url,
        "before_snapshot_revision": before_revision,
        "after_snapshot_revision": _snapshot_revision(after),
        "after_raw_count": int(after.raw_count or 0),
        "after_mapped_count": mapped_count,
        "tree_ready": True,
    }


# GTK file-chooser titles Firefox uses, in the order worth trying.
_FILE_DIALOG_TITLES = ("File Upload", "Open File", "Open", "Choose File", "Select File")


def _type_text(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Type into whatever currently holds focus. NEVER grabs focus itself.

    This used to call input.focus_firefox() first, which runs `xdotool
    windowactivate` on the Firefox window. That is destructive in two ways proven
    live on 2026-08-13:
      * It CLOSES AN OPEN MENU. The attach flow opens the tools menu and types a
        type-ahead label; the focus grab dismissed the menu and the text went into
        the PAGE instead — the window title became "Add photos — Mozilla Firefox".
      * It STEALS FOCUS BACK FROM THE GTK FILE DIALOG, so a file path typed after
        focus-dialog landed in Firefox. A 15-step attach ran rc=0 on every single
        action and attached nothing.

    The correct shape is simpler: the CALLER establishes focus deliberately — by
    clicking the composer, focusing an element, or focus-dialog — and then types.
    One action per call, focus included. A type that re-grabs focus is a second,
    hidden action, which is exactly what the step-by-step discipline forbids.
    """
    if not args.text:
        raise UiDriveError("type text must not be empty")
    snapshot = _snapshot_for_key_or_type(args, deps)
    if snapshot is not None:
        manual = _manual_ui_module(deps.platform)
        validate_type = getattr(manual, "validate_type_action", None) if manual else None
        if validate_type is not None:
            validate_type(args.text, snapshot)
    if not deps.input.type_text(args.text):
        raise UiDriveError("type_text returned false")
    return {"typed_chars": len(args.text)}


def _focus_dialog(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Activate the GTK file-chooser at the X11 WINDOW level.

    THE trap this exists for: keystrokes go to the ACTIVE X11 window. The file
    chooser is a SEPARATE window from Firefox, so without activating it, ctrl+l
    lands in Firefox's ADDRESS BAR instead of the dialog's location bar — which is
    why attach never worked. AT-SPI focus is not enough; this must be a window
    activation. Mirrors consultation_v2/runtime.py focus_file_dialog.
    """
    import subprocess
    _snapshot_at_expected_revision(args, deps)
    env = dict(os.environ)
    env["DISPLAY"] = args.display

    def _xdo(*a: str) -> str:
        return subprocess.run(("xdotool",) + a, capture_output=True, text=True,
                              env=env, timeout=10).stdout.strip()

    wid = ""
    matched = ""
    for title in _FILE_DIALOG_TITLES:
        out = _xdo("search", "--name", title)
        if out:
            wid = out.splitlines()[0].strip()
            matched = title
            break
    if not wid:
        raise UiDriveError(
            "no GTK file dialog window found on this display "
            f"(tried titles {list(_FILE_DIALOG_TITLES)}). Open the upload dialog first.")
    _xdo("windowactivate", wid)
    time.sleep(0.5)
    active = _xdo("getactivewindow")
    if active != wid:
        raise UiDriveError(
            f"file dialog {wid} did not take X11 focus (active={active!r}); "
            "typing now would leak to Firefox — refusing")
    return {"window_id": wid, "matched_title": matched,
            "window_name": _xdo("getwindowname", wid), "focused": True}


def _paste(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    text = args.text
    source = "text"
    text_file = getattr(args, "text_file", None)
    if text_file:
        try:
            with open(text_file, "r", encoding="utf-8") as fh:
                text = fh.read()
        except Exception as exc:  # fail loud, exact reason
            raise UiDriveError(f"paste --text-file could not read {text_file!r}: {exc}")
        source = "file"
    if text is None or text == "":
        raise UiDriveError("paste needs non-empty --text or a readable --text-file")
    snapshot = _snapshot_at_expected_revision(args, deps)
    manual = _manual_ui_module(deps.platform)
    validate_paste = getattr(manual, "validate_paste_action", None) if manual else None
    if validate_paste is not None:
        validate_paste(text, snapshot)
    if not deps.input.clipboard_paste(text):
        raise UiDriveError("clipboard_paste returned false")
    return {"pasted_chars": len(text), "source": source}


def _xdo_key(display: str, key: str) -> bool:
    """Send a key with --clearmodifiers, to the ACTIVE window.

    consultation_v2.input.press_key runs `xdotool key <k>` WITHOUT
    --clearmodifiers. Any modifier still physically/logically held from a previous
    action mangles the next combo — measured 2026-08-13: ctrl+l sent this way did
    NOT open the GTK file chooser's location bar, so a typed path went into the
    file-list type-ahead instead and Return dismissed the dialog with nothing
    selected. The by-hand sequence that DID work used --clearmodifiers throughout.
    We do not modify the shared primitive; ui_drive sends its own keys.
    """
    import subprocess
    env = dict(os.environ); env["DISPLAY"] = display
    r = subprocess.run(("xdotool", "key", "--clearmodifiers", key),
                       capture_output=True, text=True, env=env, timeout=15)
    return r.returncode == 0


def _key(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    if not args.key:
        raise UiDriveError("key must not be empty")
    snapshot = _snapshot_for_key_or_type(args, deps)
    if snapshot is not None:
        manual = _manual_ui_module(deps.platform)
        validate_key = getattr(manual, "validate_key_action", None) if manual else None
        if validate_key is not None:
            validate_key(args.key, snapshot)
        elif manual is not None and manual.key_requires_state(args.key):
            manual.validate_key_state(args.key, snapshot)
    if not _xdo_key(args.display, args.key):
        raise UiDriveError(f"xdotool key --clearmodifiers {args.key} failed")
    return {"key": args.key, "clearmodifiers": True}


def _write_output_artifact(text: str, output_file: str) -> dict[str, Any]:
    if not isinstance(output_file, str) or not output_file:
        raise UiDriveError("output_file must be a non-empty string")
    path = Path(output_file)
    if not path.is_absolute():
        raise UiDriveError(f"output_file must be an absolute path: {output_file!r}")
    if path.is_symlink():
        raise UiDriveError(f"output_file must not be a symlink: {path}")
    if os.path.lexists(path):
        raise UiDriveError(f"output_file already exists; refusing overwrite: {path}")
    if not path.parent.is_dir():
        raise UiDriveError(f"output_file parent directory does not exist: {path.parent}")
    if not isinstance(text, str):
        raise UiDriveError("artifact content must be a string")

    payload = text.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise UiDriveError(f"output_file already exists; refusing overwrite: {path}") from exc
    except OSError as exc:
        raise UiDriveError(f"could not create output_file {path}: {exc}") from exc

    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            written = handle.write(payload)
            if written != len(payload):
                raise UiDriveError(
                    f"short write to output_file {path}: {written} of {len(payload)} bytes"
                )
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if fd >= 0:
            os.close(fd)
        raise

    return {
        "output_file": str(path),
        "bytes": len(payload),
        "chars": len(text),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _read_clipboard(
    deps: SimpleNamespace, output_file: str | None = None
) -> dict[str, Any]:
    lock = deps.clipboard.acquire_clipboard_lock()
    try:
        text = deps.clipboard.read()
    finally:
        deps.clipboard.release_clipboard_lock(lock)
    if text is None:
        raise UiDriveError("clipboard read returned no text")
    if output_file is not None:
        return _write_output_artifact(text, output_file)
    return {"text": text}


def _extract_response(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Return the mapped platform driver's extraction result.

    consultation_v2.drive_chat_adapter.extract() owns scrolling, locating and
    activating the platform-specific extraction control. Its response_text is
    the answer. Do not clear the clipboard, construct an ElementRef, click the
    returned dict, or independently reinterpret the extraction result here.
    """
    from consultation_v2 import drive_chat_adapter

    platform = deps.platform

    result = drive_chat_adapter.extract(platform)
    text = str(result.get("response_text") or "")
    if not text.strip():
        raise UiDriveError(f"{platform}: mapped extraction returned empty response_text")

    sent_path = getattr(args, "sent_file", None)
    if sent_path:
        path = Path(sent_path).expanduser().resolve()
        if not path.is_file():
            raise UiDriveError(f"sent file is not a file: {path}")
        sent = path.read_text(encoding="utf-8").strip()
        extracted = text.strip()
        if extracted == sent or (
            len(sent) > 200 and extracted.startswith(sent[:200])
        ):
            raise UiDriveError(
                f"{platform}: extracted text matches the sent artifact "
                f"(prompt echo): {path}"
            )

    output_file = getattr(args, "output_file", None)
    if output_file is None:
        return result

    receipt = _write_output_artifact(text, output_file)
    return {**{key: value for key, value in result.items() if key != "response_text"}, **receipt}


def _add_display(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--display", required=True, help="raw X display in :N form")


def _add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ref",
        required=True,
        help="atspi3 ref from the immediately preceding canonical browser observe",
    )


def _add_expected_revision(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expected-revision",
        required=True,
        help="snapshot revision returned by the preceding explicit observe",
    )


def _add_key_or_type_surface(parser: argparse.ArgumentParser) -> None:
    surface = parser.add_mutually_exclusive_group(required=True)
    surface.add_argument(
        "--expected-revision",
        help="snapshot revision returned by the preceding explicit browser observe",
    )
    surface.add_argument(
        "--native-dialog-revision",
        help="revision returned by the preceding canonical native-dialog observe",
    )
    parser.add_argument(
        "--expected-scope",
        choices=OBSERVE_SCOPES,
        default="base",
        help="scope used to produce the preceding browser snapshot revision",
    )



def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="ui_drive.py")
    commands = parser.add_subparsers(dest="action", required=True)

    observe = commands.add_parser("observe")
    _add_display(observe)
    observe.add_argument("--surface", choices=OBSERVE_SURFACES, default="browser")
    observe.add_argument("--scope", choices=OBSERVE_SCOPES, default="base")

    for action in ("click", "focus", "activate", "hover", "operate"):
        target = commands.add_parser(action)
        _add_display(target)
        _add_target(target)

    scroll_to_bottom = commands.add_parser("scroll_to_bottom")
    _add_display(scroll_to_bottom)
    _add_target(scroll_to_bottom)

    type_parser = commands.add_parser("type")
    _add_display(type_parser)
    _add_key_or_type_surface(type_parser)
    type_parser.add_argument("--text", required=True)

    paste = commands.add_parser("paste")
    _add_display(paste)
    _add_expected_revision(paste)
    paste.add_argument("--text")
    # Paste EXACT bytes from a file instead of regenerated inline text. A large
    # packet as --text forces the model to regenerate every character token-by-token
    # (a 13K packet = ~20 min on a Jetson AND risks drift). --text-file lets the model
    # pass a PATH; the tool reads and pastes the exact file bytes: instant + faithful.
    paste.add_argument("--text-file", dest="text_file")

    key = commands.add_parser("key")
    _add_display(key)
    _add_key_or_type_surface(key)
    key.add_argument("--key", required=True)

    navigate = commands.add_parser("navigate")
    _add_display(navigate)
    navigate.add_argument("--url", required=True)

    focus_dialog = commands.add_parser("focus-dialog")
    _add_display(focus_dialog)
    _add_expected_revision(focus_dialog)

    grammar = commands.add_parser("attach-grammar")
    _add_display(grammar)

    read_clipboard = commands.add_parser("read-clipboard")
    _add_display(read_clipboard)
    read_clipboard.add_argument("--output-file", dest="output_file")

    extract = commands.add_parser("extract")
    _add_display(extract)
    extract.add_argument(
        "--sent-file",
        dest="sent_file",
        help="exact sent artifact; reject extraction if the answer is a prompt echo",
    )
    extract.add_argument("--output-file", dest="output_file")

    return parser


# Display mutations and clipboard extraction must hold the per-display lease. Observe remains
# read-only: it reports lease state without creating, renewing, or transferring ownership.
_LOCK_ACTION_OPS = {
    "click", "focus", "activate", "hover", "operate", "type", "paste", "key", "navigate",
    "focus-dialog", "read-clipboard", "extract", "scroll_to_bottom",
}


def _observe_lease(
    display: str,
    lease: SimpleNamespace | None,
    ttl: int,
) -> dict[str, Any]:
    if lease is None:
        return {
            "state": "unattributed_observation",
            "owned": False,
            "expires_by_ttl": True,
        }
    try:
        record = _display_lock_record(display) or {}
        if not record:
            return {
                "state": "unheld",
                "owned": False,
                "expires_by_ttl": True,
            }
        owner_matches = record.get("owner_token") == lease.owner
        turn_matches = record.get("last_turn_id") == lease.turn_id
        owned = owner_matches and turn_matches
        return {
            "state": (
                "owned"
                if owned
                else "same_generation_other_turn"
                if owner_matches
                else "held_by_other"
            ),
            "owned": owned,
            "turn_matches": turn_matches,
            "ttl_seconds": record.get("ttl_seconds"),
            "expires_by_ttl": True,
        }
    except Exception as exc:
        return {
            "state": "unavailable",
            "owned": False,
            "expires_by_ttl": True,
            "error": str(exc),
        }


_FENCED_DISPLAY_LEASE_LUA = """
local authoritative_generation = redis.call('GET', KEYS[2])
if authoritative_generation ~= ARGV[1] then
    return {'refused_generation_fence', tostring(authoritative_generation or '')}
end

local turn_deadline = tonumber(redis.call('ZSCORE', KEYS[4], ARGV[4]) or '0')
if turn_deadline == 0 then
    return {'refused_missing_turn_lease', ''}
end
if turn_deadline <= tonumber(ARGV[9]) then
    return {'refused_expired_turn_lease', ''}
end

local raw_context = redis.call('HGET', KEYS[3], ARGV[4])
if not raw_context then
    return {'refused_missing_turn', ''}
end
local context_ok, context = pcall(cjson.decode, raw_context)
if not context_ok or type(context) ~= 'table' then
    return {'refused_invalid_turn', ''}
end
if tostring(context['turn_id'] or '') ~= ARGV[4]
    or tostring(context['seat_id'] or '') ~= ARGV[3]
    or tostring(context['process_generation'] or '') ~= ARGV[1] then
    return {'refused_turn_mismatch', ''}
end

local raw_lock = redis.call('GET', KEYS[1])
if not raw_lock then
    local record = {
        owner_token=ARGV[2],
        seat_id=ARGV[3],
        last_turn_id=ARGV[4],
        generation_fence_key=KEYS[2],
        actor_type='taey-drive_chat',
        holder_pid=tonumber(ARGV[6]),
        holder_starttime=ARGV[7],
        locked_at=ARGV[8]
    }
    redis.call('SET', KEYS[1], cjson.encode(record), 'EX', tonumber(ARGV[5]))
    return {'acquired', ''}
end

local lock_ok, record = pcall(cjson.decode, raw_lock)
if not lock_ok or type(record) ~= 'table' then
    return {'refused_invalid_lock', ''}
end
local previous_owner = tostring(record['owner_token'] or '')
if tostring(record['actor_type'] or '') ~= 'taey-drive_chat' then
    return {'refused_actor', previous_owner}
end
if tostring(record['generation_fence_key'] or '') ~= KEYS[2] then
    return {'refused_other_process_namespace', previous_owner}
end

if previous_owner == ARGV[2] then
    record['last_turn_id'] = ARGV[4]
    record['holder_pid'] = tonumber(ARGV[6])
    record['holder_starttime'] = ARGV[7]
    record['generation_fence_key'] = KEYS[2]
    redis.call('SET', KEYS[1], cjson.encode(record), 'EX', tonumber(ARGV[5]))
    return {'renewed', ''}
end

local previous_seat = tostring(record['seat_id'] or '')
local owner_prefix = 'taey-drive:' .. previous_seat .. ':'
if string.sub(previous_owner, 1, string.len(owner_prefix)) ~= owner_prefix then
    return {'refused_owner_shape', previous_owner}
end
local previous_generation = string.sub(previous_owner, string.len(owner_prefix) + 1)
if string.len(previous_generation) ~= 32
    or not string.match(previous_generation, '^[0-9a-f]+$') then
    return {'refused_owner_shape', previous_owner}
end
if previous_generation == ARGV[1] then
    if previous_seat ~= ARGV[3] then
        return {'refused_other_seat', previous_owner}
    end
    return {'refused_current_generation_owner', previous_owner}
end

record['previous_owner_token'] = previous_owner
record['previous_seat_id'] = previous_seat
record['owner_token'] = ARGV[2]
record['seat_id'] = ARGV[3]
record['last_turn_id'] = ARGV[4]
record['generation_fence_key'] = KEYS[2]
record['actor_type'] = 'taey-drive_chat'
record['holder_pid'] = tonumber(ARGV[6])
record['holder_starttime'] = ARGV[7]
record['generation_takeover_at'] = ARGV[8]
redis.call('SET', KEYS[1], cjson.encode(record), 'EX', tonumber(ARGV[5]))
return {'generation_takeover', previous_owner}
"""


def _process_starttime() -> str:
    try:
        stat = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
    except OSError as exc:
        raise UiDriveError("cannot establish display-lease process identity") from exc
    end = stat.rfind(")")
    fields = stat[end + 2:].split() if end >= 0 else []
    if len(fields) < 20:
        raise UiDriveError("cannot parse display-lease process identity")
    return fields[19]


def _guard_action(
    display: str,
    lease: SimpleNamespace,
    ttl: int,
) -> dict[str, Any]:
    try:
        client = _lock_redis_client()
        result = client.eval(
            _FENCED_DISPLAY_LEASE_LUA,
            4,
            _plan_lock_key(display),
            lease.generation_fence_key,
            f"taey:{lease.seat_id}:turn_context",
            f"taey:{lease.seat_id}:active_turns",
            lease.process_generation,
            lease.owner,
            lease.seat_id,
            lease.turn_id,
            ttl,
            os.getpid(),
            _process_starttime(),
            datetime.now(timezone.utc).isoformat(),
            time.time(),
        )
    except Exception as exc:
        raise UiDriveError(
            f"display {display} fenced lease unavailable; refusing action: {exc}"
        ) from exc
    if not isinstance(result, (list, tuple)) or not result:
        raise UiDriveError(
            f"display {display} fenced lease returned an invalid receipt; refusing action"
        )
    state = str(result[0])
    previous_owner = str(result[1]) if len(result) > 1 and result[1] else ""
    if state in {"acquired", "renewed", "generation_takeover"}:
        receipt = {
            "state": state,
            "owned": True,
            "ttl_seconds": ttl,
            "expires_by_ttl": True,
        }
        if previous_owner:
            receipt["previous_owner_token"] = previous_owner
        return receipt
    detail = f" ({previous_owner})" if previous_owner else ""
    raise UiDriveError(
        f"display {display} fenced lease refused action: {state}{detail}"
    )


def _dispatch(args: argparse.Namespace, deps: SimpleNamespace) -> Any:
    if args.action == "observe":
        result = _observe(args, deps)
        result["lease"] = _observe_lease(
            deps.display,
            _lease_context(required=False),
            LOCK_TTL_DEFAULT,
        )
        return result
    lease_receipt = None
    if args.action in _LOCK_ACTION_OPS:
        lease = _lease_context()
        assert lease is not None
        lease_receipt = _guard_action(deps.display, lease, LOCK_TTL_DEFAULT)
    if args.action == "extract":
        result = _extract_response(args, deps)
    elif args.action == "scroll_to_bottom":
        result = _scroll_to_bottom_action(args, deps)
    elif args.action in {"click", "focus", "activate", "hover", "operate"}:
        result = _element_action(args.action, args, deps)
    elif args.action == "type":
        result = _type_text(args, deps)
    elif args.action == "paste":
        result = _paste(args, deps)
    elif args.action == "key":
        result = _key(args, deps)
    elif args.action == "navigate":
        result = _navigate_fresh(args, deps)
    elif args.action == "focus-dialog":
        result = _focus_dialog(args, deps)
    elif args.action == "read-clipboard":
        result = _read_clipboard(deps, getattr(args, "output_file", None))
    elif args.action == "attach-grammar":
        return _attach_grammar(deps.display)
    else:
        raise UiDriveError(f"unsupported action: {args.action}")
    if lease_receipt is not None:
        result = {**result, "lease": lease_receipt}
    return result


def main(argv: list[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    action = actual_argv[0] if actual_argv else "parse"
    display = _requested_option(actual_argv, "--display")
    platform = None
    try:
        args = _parser().parse_args(actual_argv)
        action = args.action
        display = args.display
        deps = _configure_display(display)
        display = deps.display
        platform = deps.platform
        result = _dispatch(args, deps)
        _emit(
            ok=True,
            action=action,
            display=display,
            platform=platform,
            result=result,
            error=None,
        )
        return 0
    except Exception as exc:
        _emit(
            ok=False,
            action=action,
            display=display,
            platform=platform,
            result=None,
            error=str(exc),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
