#!/home/mira/taeys-env-sys/bin/python
"""One-invocation AT-SPI observation and action CLI for raw X displays."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse


# The AT-SPI primitives live in the PUBLIC palios-taey/taeys-hands repo
# (consultation_v2/: platforms_runtime, primitives, atspi, clipboard, input, interact,
# firefox_chrome.yaml — all tracked there). Point TAEYS_HANDS_ROOT at your clone; the
# default is this operator's checkout. A downloaded Taey sets the env var and this works
# unmodified — no private path is on this dependency chain.
TAEYS_HANDS = os.environ.get("TAEYS_HANDS_ROOT", "/home/mira/taeys-hands")
if TAEYS_HANDS not in sys.path:
    sys.path.insert(0, TAEYS_HANDS)

try:
    from consultation_v2.platforms_runtime import display_environment
except ImportError as exc:  # fail LOUD and actionable, never a bare traceback
    sys.stderr.write(
        f"ui_drive: cannot import consultation_v2 from {TAEYS_HANDS!r}: {exc}\n"
        "Set TAEYS_HANDS_ROOT to a clone of https://github.com/palios-taey/taeys-hands\n"
    )
    raise

# --- per-display dispatch-lock ----------------------------------------------------------------
# Reuse the taeys-hands lock primitives so drive_chat and the taeys-hands side share ONE key
# (taey:plan_active::N) and one NX/EX discipline by construction. A fixed owner token means every
# drive_chat call is ONE owner (distinct from a by-hand driver's token), so first-writer-wins +
# refuse-if-a-different-owner-holds works both ways. FAIL-OPEN: an advisory lock must never block
# Taey from driving a display, so every lock op is wrapped and proceeds on error.
LOCK_OWNER = "taey-drive_chat"
# TTL must exceed the gap between a session's owned-observes, or the lease drops mid-run and another
# driver can grab the display. 600s covers deep-mode (deep_research / extended) poll gaps with
# headroom; a well-behaved wait-for-completion loop observes far more often than that. Env-overridable
# so a very sparse-poll op can raise it without a code change. (v1.1: an explicit release on
# session-end will free a finished display before the TTL — noted with taeys-hands.)
LOCK_TTL_DEFAULT = int(os.environ.get("TAEY_DRIVE_LOCK_TTL", "600"))
try:
    from consultation_v2.primitives import (
        acquire_display_lock as _acquire_display_lock,
        display_lock_record as _display_lock_record,
        _plan_lock_key as _plan_lock_key,
    )
    from storage.redis_pool import get_client as _lock_redis_client
    from redis.exceptions import WatchError as _LockWatchError
    _LOCK_AVAILABLE = True
except Exception:  # fail-open if the lock stack cannot import
    _LOCK_AVAILABLE = False


REF_PREFIX = "atspi1."
CHROME_POLICY = Path(TAEYS_HANDS) / "consultation_v2" / "firefox_chrome.yaml"
OUTPUT_ROLES = {
    "article",
    "check box",
    "combo box",
    "entry",
    "heading",
    "link",
    "list item",
    "menu item",
    "paragraph",
    "push button",
    "radio button",
    "section",
    "spin button",
    "static",
    "text",
    "toggle button",
}
STATE_NAMES = (
    "editable",
    "focusable",
    "focused",
    "showing",
    "visible",
    "sensitive",
    "enabled",
    "checked",
    "pressed",
    "selected",
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


def _emit(*, ok: bool, action: str, display: str | None, result: Any, error: str | None) -> None:
    print(
        json.dumps(
            {
                "ok": ok,
                "action": action,
                "display": display,
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

    from consultation_v2 import atspi, clipboard, input as ui_input, interact
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    ui_input.set_display(display)
    clipboard.set_display(display)
    return SimpleNamespace(
        display=env["DISPLAY"],
        Atspi=Atspi,
        atspi=atspi,
        clipboard=clipboard,
        input=ui_input,
        interact=interact,
    )


def _chrome_policy() -> tuple[set[str], set[tuple[str, str]], set[str]]:
    import yaml

    if not CHROME_POLICY.is_file():
        raise UiDriveError(f"Firefox chrome policy is missing: {CHROME_POLICY}")
    data = (yaml.safe_load(CHROME_POLICY.read_text(encoding="utf-8")) or {}).get(
        "firefox_chrome", {}
    )
    subtree_roles = set(data.get("subtree_roles") or [])
    portal_roles = set(data.get("portal_container_roles") or [])
    exact: set[tuple[str, str]] = set()
    for spec in data.get("exact_elements") or []:
        names = spec.get("names_any_of") or ([spec["name"]] if "name" in spec else [])
        for name in names:
            exact.add((str(name), str(spec.get("role") or "")))
    return subtree_roles, exact, portal_roles


def _states(node: Any, Atspi: Any) -> list[str]:
    found: list[str] = []
    try:
        state_set = node.get_state_set()
    except Exception:
        return found
    for name in STATE_NAMES:
        try:
            if state_set.contains(getattr(Atspi.StateType, name.upper())):
                found.append(name)
        except Exception:
            continue
    return found


def _text(node: Any, Atspi: Any, max_chars: int = 700) -> str:
    try:
        iface = node.get_text_iface()
        if iface:
            count = int(iface.get_character_count())
            if 0 < count <= max_chars:
                return (iface.get_text(0, count) or "").strip()
    except Exception:
        pass
    try:
        count = int(Atspi.Text.get_character_count(node))
        if 0 < count <= max_chars:
            return (Atspi.Text.get_text(node, 0, count) or "").strip()
    except Exception:
        pass
    return ""


def _is_showing(node: Any, Atspi: Any) -> bool:
    try:
        return bool(node.get_state_set().contains(Atspi.StateType.SHOWING))
    except Exception:
        return False


def _is_onscreen(node: Any, Atspi: Any) -> bool:
    try:
        component = node.get_component_iface()
        if component is None:
            return False
        rect = component.get_extents(Atspi.CoordType.SCREEN)
        return bool(
            rect
            and rect.x >= 0
            and rect.y >= 0
            and rect.width > 0
            and rect.height > 0
        )
    except Exception:
        return False


def _ancestor_set(node: Any) -> set[Any]:
    ancestors: set[Any] = set()
    current = node
    for _ in range(50):
        try:
            parent = current.get_parent()
        except Exception:
            break
        if parent is None:
            break
        ancestors.add(parent)
        current = parent
    return ancestors


def _portal_roots(
    firefox: Any,
    document: Any,
    *,
    Atspi: Any,
    subtree_roles: set[str],
    portal_roles: set[str],
    max_depth: int = 10,
) -> list[Any]:
    document_ancestors = _ancestor_set(document)
    roots: list[Any] = []

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            role = str(node.get_role_name() or "")
            if node == document or role == "document web" or role in subtree_roles:
                return
            if (
                node not in document_ancestors
                and role in portal_roles
                and _is_onscreen(node, Atspi)
            ):
                roots.append(node)
                return
            for index in range(node.get_child_count()):
                child = node.get_child_at_index(index)
                if child is not None:
                    walk(child, depth + 1)
        except Exception:
            return

    walk(firefox, 0)
    return roots


def _encode_ref(
    *,
    display: str,
    role: str,
    name: str,
    nth: int,
    count: int,
    max_depth: int,
) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "display": display,
            "role": role,
            "name": name,
            "nth": nth,
            "count": count,
            "max_depth": max_depth,
        },
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
    required = {"v", "display", "role", "name", "nth", "count", "max_depth"}
    if set(payload) != required or payload.get("v") != 1:
        raise UiDriveError("invalid ref schema")
    if not re.fullmatch(r":\d+", payload.get("display") or ""):
        raise UiDriveError("invalid ref display")
    if not isinstance(payload["role"], str) or not isinstance(payload["name"], str):
        raise UiDriveError("invalid ref role/name")
    for key in ("nth", "count", "max_depth"):
        if not isinstance(payload[key], int):
            raise UiDriveError(f"invalid ref {key}")
    if payload["nth"] < 0 or payload["count"] < 1:
        raise UiDriveError("invalid ref occurrence metadata")
    _validate_max_depth(payload["max_depth"])
    return payload


def _validate_max_depth(value: int) -> None:
    if not 1 <= value <= 80:
        raise UiDriveError(f"max depth must be between 1 and 80, got {value}")


def _snapshot(deps: SimpleNamespace, *, max_depth: int) -> list[dict[str, Any]]:
    _validate_max_depth(max_depth)
    firefox = deps.atspi.find_firefox()
    if firefox is None:
        raise UiDriveError(f"Firefox not found on display {deps.display}")

    documents = deps.atspi.document_web_elements(firefox, max_depth=10)
    showing_documents = [doc for doc in documents if _is_showing(doc, deps.Atspi)]
    if len(showing_documents) != 1:
        raise UiDriveError(
            f"expected exactly one SHOWING document web on {deps.display}, found {len(showing_documents)}"
        )

    subtree_roles, exact_chrome, portal_roles = _chrome_policy()
    roots = [showing_documents[0]]
    roots.extend(
        _portal_roots(
            firefox,
            showing_documents[0],
            Atspi=deps.Atspi,
            subtree_roles=subtree_roles,
            portal_roles=portal_roles,
        )
    )
    rows: list[dict[str, Any]] = []

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            role = str(node.get_role_name() or "")
            if role in subtree_roles:
                return
            if role == "document web" and not _is_showing(node, deps.Atspi):
                return
            name = str(node.get_name() or "").strip()
            states = _states(node, deps.Atspi)
            text = _text(node, deps.Atspi)
            if (
                "showing" in states
                and role in OUTPUT_ROLES
                and (name or text)
                and (name, role) not in exact_chrome
            ):
                rows.append(
                    {
                        "role": role,
                        "name": name,
                        "text": text,
                        "states": states,
                        "atspi_obj": node,
                    }
                )
            for index in range(node.get_child_count()):
                child = node.get_child_at_index(index)
                if child is not None:
                    walk(child, depth + 1)
        except Exception:
            return

    for root in roots:
        walk(root, 0)

    totals = Counter((row["role"], row["name"]) for row in rows)
    occurrences: Counter[tuple[str, str]] = Counter()
    for row in rows:
        key = (row["role"], row["name"])
        nth = occurrences[key]
        occurrences[key] += 1
        row["nth"] = nth
        row["match_count"] = totals[key]
        row["ref"] = _encode_ref(
            display=deps.display,
            role=row["role"],
            name=row["name"],
            nth=nth,
            count=totals[key],
            max_depth=max_depth,
        )
    return rows


def _public_element(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": row["ref"],
        "role": row["role"],
        "name": row["name"],
        "text": row["text"],
        "states": row["states"],
    }


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
_DISPLAY_PLATFORM = {
    ":2": "chatgpt", ":3": "claude", ":4": "gemini", ":5": "grok", ":6": "perplexity",
    ":21": "claude", ":22": "gemini", ":23": "grok", ":24": "perplexity",
}
_YAML_ROOT = os.path.join(TAEYS_HANDS, "consultation_v2", "platforms")
_yaml_cache: dict[str, dict] = {}


def _platform_config(display: str) -> dict:
    platform = _DISPLAY_PLATFORM.get(display)
    if not platform:
        raise UiDriveError(
            f"no platform mapping for display {display}; "
            f"known: {sorted(_DISPLAY_PLATFORM)}")
    if platform not in _yaml_cache:
        path = os.path.join(_YAML_ROOT, platform, f"{platform}.yaml")
        try:
            import yaml as _yaml
            with open(path, "r", encoding="utf-8") as fh:
                _yaml_cache[platform] = _yaml.safe_load(fh) or {}
        except Exception as exc:
            raise UiDriveError(f"cannot load platform YAML {path}: {exc}") from exc
    return _yaml_cache[platform]


def _attachment_name_matches(display_name: str, filename: str) -> bool:
    """The engine's proven chip-name rule (chatgpt/driver.py::_attachment_name_matches).

    Chips do NOT render as the plain filename: they carry suffixes like (7), and
    long names are ELIDED with '...' in the middle. Matching on a literal filename
    is why a verify can fail while the attachment is actually present — which is
    exactly what happened on 2026-08-13: the chip was there, the check missed it,
    and the caller "recovered" by navigating, destroying the attachment.
    """
    expected_path = os.path.abspath(filename)
    expected_name = os.path.basename(filename)
    displayed = display_name.split()[0] if display_name else ""
    for expected in (expected_path, expected_name):
        if display_name == expected or displayed == expected:
            return True
        if "..." in displayed:
            prefix, suffix = displayed.split("...", 1)
            if expected.startswith(prefix) and expected.endswith(suffix):
                return True
        # chips also render as name(N).ext for repeat uploads
        stem, dot, ext = expected_name.rpartition(".")
        if stem and displayed.startswith(stem) and displayed.endswith(ext):
            return True
    return False


def _verify_attachment(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Is FILE attached? Chip in the composer, roles push button/panel, and the
    file dialog closed — the engine's two-part verification, not a name guess."""
    rows = _snapshot(deps, max_depth=getattr(args, "max_depth", None) or 20)
    chips = [r for r in rows
             if r.get("role") in ("push button", "panel")
             and _attachment_name_matches(r.get("name") or r.get("text") or "", args.file)]
    all_chips = [(r.get("role"), (r.get("name") or r.get("text") or "")[:60])
                 for r in rows
                 if r.get("role") in ("push button", "panel")
                 and "." in (r.get("name") or "")[-6:]]
    dialog_open = False
    try:
        _focus_dialog(argparse.Namespace(display=args.display), deps)
        dialog_open = True
    except Exception:
        dialog_open = False
    return {"file": args.file, "attached": bool(chips) and not dialog_open,
            "chip": (chips[0].get("name") if chips else None),
            "dialog_still_open": dialog_open,
            "all_attachment_chips": all_chips}


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
    trig_key = att.get("trigger") or "attach_trigger"
    targ_key = att.get("menu_target") or "tool_upload"
    out = {
        "platform": _DISPLAY_PLATFORM.get(display),
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
    return out


def _element_spec(display: str, key: str) -> dict:
    cfg = _platform_config(display)
    emap = ((cfg.get("tree") or {}).get("element_map") or {})
    spec = emap.get(key)
    if not spec:
        raise UiDriveError(
            f"element {key!r} is not defined for {_DISPLAY_PLATFORM.get(display)} "
            f"in its platform YAML. Defined keys include: "
            f"{sorted(list(emap)[:24])}")
    if not spec.get("name") or not spec.get("role"):
        raise UiDriveError(f"element {key!r} lacks name/role in the platform YAML: {spec}")
    return spec


def _scroll_document(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Scroll the conversation DOCUMENT to the bottom via the mapped primitive.

    Clicking a "Scroll to bottom" control is not the same thing and is not
    enough — the response's Copy button only enters the AT-SPI tree once it is
    actually on-screen.
    """
    from consultation_v2 import drive_chat_adapter

    platform = _DISPLAY_PLATFORM.get(deps.display)
    if not platform:
        raise UiDriveError(f"no platform is mapped for display {deps.display}")
    return drive_chat_adapter.scroll(platform)


def _extract_response(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Copy the latest answer out through the platform's MAPPED extraction control.

    The read is consultation_v2's, not ours. drive_chat_adapter.extract scrolls
    the document, rescans at FULL depth (no cap) and resolves the platform's own
    workflow.extract.primary_key, taking the lowest match — the newest turn.
    ui_drive's own snapshot capped depth and pruned that control, which is the
    whole reason extraction never worked from this side: on a real Claude answer
    a depth-30 read saw 136 rows and zero Copy controls where the full-depth scan
    saw 3687 and found it.

    The tail mirrors platforms/claude/driver.py extract_primary: blank the
    clipboard first so a copy that never fires is detectable rather than
    returning stale text, scroll the control into view, click, settle, read.
    """
    from consultation_v2 import drive_chat_adapter
    from consultation_v2.interact import atspi_click
    from consultation_v2.runtime import ConsultationRuntime
    from consultation_v2.types import ElementRef

    platform = _DISPLAY_PLATFORM.get(deps.display)
    if not platform:
        raise UiDriveError(f"no platform is mapped for display {deps.display}")

    target = drive_chat_adapter.extract(platform)
    runtime = ConsultationRuntime(platform)

    runtime.write_clipboard("")
    time.sleep(0.3)
    runtime.scroll_element_into_view(ElementRef(
        key=None,
        name=str(target.get("name") or ""),
        role=str(target.get("role") or ""),
        x=target.get("x"),
        y=target.get("y"),
        states=list(target.get("states") or []),
        atspi_obj=target.get("atspi_obj"),
    ))
    time.sleep(0.3)
    if not atspi_click(target):
        raise UiDriveError(
            f"{platform}: the mapped extraction control was found but the click returned false")
    time.sleep(2.5)
    text = (runtime.read_clipboard() or "").strip()
    if not text:
        raise UiDriveError(
            f"{platform}: clipboard is empty after the copy — the control was clicked but the "
            f"copy did not fire. This is a real failure, not an empty answer.")

    # An echo of what we sent reads exactly like a successful extraction.
    sent_path = getattr(args, "sent_file", None)
    if sent_path:
        sent = Path(sent_path).read_text(encoding="utf-8").strip()
        if text == sent or (len(sent) > 200 and text.startswith(sent[:200])):
            raise UiDriveError(
                f"{platform}: the clipboard holds the text we SENT, not the answer — prompt echo. "
                f"{len(text)} chars matching {sent_path}")

    return {
        "platform": platform,
        "element_key": target.get("element_key"),
        "match_count": target.get("match_count"),
        "chars": len(text),
        "text": text,
    }


def _verify_element(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Did the previous step land? Resolve a YAML element key to its exact
    {name, role} and report whether that control is on screen.

    Exact match only — a substring hit on a control element is not evidence the
    control is there. _snapshot collects only SHOWING nodes, so `present` means
    present-and-on-screen and its absence means the control is not on screen.
    """
    spec = _element_spec(deps.display, args.element)
    rows = _snapshot(deps, max_depth=getattr(args, "max_depth", None) or 20)
    matches = [
        row for row in rows
        if row["role"] == spec["role"] and row["name"] == spec["name"]
    ]
    expect = getattr(args, "expect", None) or "present"
    present = bool(matches)
    return {
        "element": args.element,
        "name": spec["name"],
        "role": spec["role"],
        "present": present,
        "count": len(matches),
        "states": sorted({str(state) for row in matches for state in (row.get("states") or [])}),
        "expected": expect,
        "satisfied": (not present) if expect == "absent" else present,
    }


def _resolve_target(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    element_key = getattr(args, "element", None)
    if element_key:
        if args.ref:
            raise UiDriveError("--element cannot be combined with --ref")
        spec = _element_spec(deps.display, element_key)
        args = argparse.Namespace(**{**vars(args),
                                     "role": spec["role"], "name": spec["name"],
                                     "nth": args.nth, "ref": None})
    if args.ref:
        if args.role is not None or args.name is not None or args.nth is not None:
            raise UiDriveError("--ref cannot be combined with --role, --name, or --nth")
        descriptor = _decode_ref(args.ref)
        if descriptor["display"] != deps.display:
            raise UiDriveError(
                f"ref is scoped to display {descriptor['display']}, not {deps.display}"
            )
        role = descriptor["role"]
        name = descriptor["name"]
        nth = descriptor["nth"]
        max_depth = descriptor["max_depth"]
        expected_count = descriptor["count"]
    else:
        if args.role is None or args.name is None:
            raise UiDriveError("target requires --ref or both --role and --name")
        role = args.role
        name = args.name
        nth = args.nth
        max_depth = args.max_depth
        expected_count = None

    rows = _snapshot(deps, max_depth=max_depth)
    matches = [row for row in rows if row["role"] == role and row["name"] == name]
    if expected_count is not None and len(matches) != expected_count:
        raise UiDriveError(
            f"ref tree shape changed: expected {expected_count} exact role/name matches, found {len(matches)}"
        )
    if nth is None:
        if len(matches) != 1:
            raise UiDriveError(
                f"target descriptor matched {len(matches)} elements; expected exactly one or supply --nth"
            )
        return matches[0]
    if nth < 0:
        raise UiDriveError("--nth must be zero or greater")
    if nth >= len(matches):
        raise UiDriveError(
            f"target descriptor matched {len(matches)} elements; occurrence {nth} does not exist"
        )
    return matches[nth]


def _observe(args: argparse.Namespace, deps: SimpleNamespace) -> list[dict[str, Any]]:
    rows = _snapshot(deps, max_depth=args.max_depth)
    if args.filter:
        needle = args.filter.casefold()
        rows = [
            row
            for row in rows
            if needle in row["role"].casefold()
            or needle in row["name"].casefold()
            or needle in row["text"].casefold()
        ]
    return [_public_element(row) for row in rows]


def _full_text(node: Any) -> str:
    """The element's COMPLETE text. _text() caps at 700 chars and returns ''
    past that, which is fine for labels and useless for a composer holding a
    prompt — a comparison against a truncated read would pass or fail for the
    wrong reason."""
    try:
        iface = node.get_text_iface()
        if not iface:
            return ""
        count = iface.get_character_count()
        if count <= 0:
            return ""
        return iface.get_text(0, count) or ""
    except Exception:
        return ""


def _verify_composer(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Does the composer hold EXACTLY the artifact we intend to send?

    Sending is the one action that leaves this machine, and until now nothing
    compared what is about to be sent against what was supposed to be sent. A
    substituted, partial, or failed paste all look identical from the outside:
    the paste call returns ok either way. This is the check that makes the
    difference visible BEFORE send rather than after.
    """
    spec = _element_spec(deps.display, args.element or "input")
    rows = _snapshot(deps, max_depth=getattr(args, "max_depth", None) or 20)
    matches = [r for r in rows
               if r["role"] == spec["role"] and r["name"] == spec["name"]]
    if len(matches) != 1:
        raise UiDriveError(
            f"composer {spec['name']!r} matched {len(matches)} elements; expected exactly one")
    actual = _full_text(matches[0]["atspi_obj"]).strip()
    expected = Path(args.file).read_text(encoding="utf-8").strip()
    if actual == expected:
        return {"match": True, "chars": len(actual), "file": args.file}
    diverge = next((i for i in range(min(len(actual), len(expected)))
                    if actual[i] != expected[i]), min(len(actual), len(expected)))
    return {
        "match": False,
        "file": args.file,
        "composer_chars": len(actual),
        "file_chars": len(expected),
        "first_divergence_at": diverge,
        "composer_around_divergence": actual[max(0, diverge - 40):diverge + 80],
        "file_around_divergence": expected[max(0, diverge - 40):diverge + 80],
    }


def _element_centre(row: dict[str, Any], deps: SimpleNamespace) -> tuple[int, int] | None:
    """Screen centre of an element, computed the way the engine does it
    (consultation_v2/tree.py:204-209)."""
    obj = row.get("atspi_obj")
    if obj is None:
        return None
    try:
        comp = obj.get_component_iface()
        if not comp:
            return None
        rect = comp.get_extents(deps.Atspi.CoordType.SCREEN)
    except Exception:
        return None
    if not rect or rect.x < 0 or rect.y < 0:
        return None
    return (
        rect.x + (rect.width // 2 if rect.width else 0),
        rect.y + (rect.height // 2 if rect.height else 0),
    )


def _element_action(
    action: str, args: argparse.Namespace, deps: SimpleNamespace
) -> dict[str, Any]:
    row = _resolve_target(args, deps)
    primitive = {
        "click": deps.interact.atspi_click,
        "focus": deps.interact.atspi_focus,
        "activate": deps.interact.atspi_activate,
    }[action]
    if primitive(row):
        return {"performed": True, "via": "atspi", "element": _public_element(row)}
    if action != "click":
        raise UiDriveError(f"{action} primitive returned false")
    # atspi_click documents "No fallback — caller decides alternatives", so the
    # second path belongs here. Many web controls expose no AT-SPI action at all
    # and legitimately return false; the canonical seat then clicks the element's
    # centre with the pointer (consultation_v2/seat_actions.py click(), described
    # in SEAT_SELFCONTAIN_MAPPING.md as "the same 2-path strategy act.py uses").
    # One path was never the whole click.
    centre = _element_centre(row, deps)
    if centre is None:
        raise UiDriveError(
            f"click: {row['name']!r} exposes no AT-SPI action and has no on-screen "
            f"bounds to click")
    if not deps.input.click_at(int(centre[0]), int(centre[1])):
        raise UiDriveError(
            f"click: {row['name']!r} exposes no AT-SPI action and the pointer click "
            f"at {centre} failed")
    return {"performed": True, "via": "pointer", "at": list(centre),
            "element": _public_element(row)}


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
    if not deps.input.type_text(args.text):
        raise UiDriveError("type_text returned false")
    return {"typed_chars": len(args.text)}


def _file_dialog_is_active(display: str) -> bool:
    """True when the ACTIVE X11 window is a GTK file chooser. Read-only probe;
    never raises — a focus question must not break the action."""
    import subprocess
    try:
        env = dict(os.environ); env["DISPLAY"] = display
        active = subprocess.run(("xdotool", "getactivewindow"), capture_output=True,
                                text=True, env=env, timeout=5).stdout.strip()
        if not active:
            return False
        name = subprocess.run(("xdotool", "getwindowname", active), capture_output=True,
                              text=True, env=env, timeout=5).stdout.strip()
        return any(title.lower() in name.lower() for title in _FILE_DIALOG_TITLES)
    except Exception:
        return False


def _focus_dialog(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Activate the GTK file-chooser at the X11 WINDOW level.

    THE trap this exists for: keystrokes go to the ACTIVE X11 window. The file
    chooser is a SEPARATE window from Firefox, so without activating it, ctrl+l
    lands in Firefox's ADDRESS BAR instead of the dialog's location bar — which is
    why attach never worked. AT-SPI focus is not enough; this must be a window
    activation. Mirrors consultation_v2/runtime.py focus_file_dialog.
    """
    import subprocess
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
    if not _xdo_key(args.display, args.key):
        raise UiDriveError(f"xdotool key --clearmodifiers {args.key} failed")
    return {"key": args.key, "clearmodifiers": True}


def _read_clipboard(deps: SimpleNamespace) -> dict[str, Any]:
    lock = deps.clipboard.acquire_clipboard_lock()
    try:
        text = deps.clipboard.read()
    finally:
        deps.clipboard.release_clipboard_lock(lock)
    if text is None:
        raise UiDriveError("clipboard read returned no text")
    return {"text": text}


def _navigate(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    parsed = urlparse(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UiDriveError("navigate URL must be absolute http(s)")
    if not deps.input.focus_firefox():
        raise UiDriveError("focus_firefox returned false")
    if not deps.input.press_key("ctrl+l"):
        raise UiDriveError("address-bar key returned false")
    time.sleep(0.2)
    if not deps.input.type_text(args.url):
        raise UiDriveError("URL type_text returned false")
    if not deps.input.press_key("Return"):
        raise UiDriveError("navigation Return returned false")
    return {"url": args.url}


def _add_display(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--display", required=True, help="raw X display in :N form")


def _add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ref")
    parser.add_argument("--role")
    parser.add_argument("--name")
    parser.add_argument("--nth", type=int)
    parser.add_argument("--max-depth", type=int, default=40)
    parser.add_argument("--element", help="platform-YAML element key (e.g. attach_trigger, composer_input, send_button) — resolved from that display's platform YAML")



def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="ui_drive.py")
    commands = parser.add_subparsers(dest="action", required=True)

    observe = commands.add_parser("observe")
    _add_display(observe)
    observe.add_argument("--max-depth", type=int, default=12)
    observe.add_argument("--filter")

    for action in ("click", "focus", "activate"):
        target = commands.add_parser(action)
        _add_display(target)
        _add_target(target)

    type_parser = commands.add_parser("type")
    _add_display(type_parser)
    type_parser.add_argument("--text", required=True)

    paste = commands.add_parser("paste")
    _add_display(paste)
    paste.add_argument("--text")
    # Paste EXACT bytes from a file instead of regenerated inline text. A large
    # packet as --text forces the model to regenerate every character token-by-token
    # (a 13K packet = ~20 min on a Jetson AND risks drift). --text-file lets the model
    # pass a PATH; the tool reads and pastes the exact file bytes: instant + faithful.
    paste.add_argument("--text-file", dest="text_file")

    key = commands.add_parser("key")
    _add_display(key)
    key.add_argument("--key", required=True)

    focus_dialog = commands.add_parser("focus-dialog")
    _add_display(focus_dialog)

    grammar = commands.add_parser("attach-grammar")
    _add_display(grammar)

    verify = commands.add_parser("verify-attachment")
    _add_display(verify)
    verify.add_argument("--file", required=True, help="absolute path of the file that should be attached")

    scroll_p = commands.add_parser("scroll")
    _add_display(scroll_p)

    extract_p = commands.add_parser("extract")
    _add_display(extract_p)
    extract_p.add_argument("--sent-file", dest="sent_file",
                           help="path of the artifact that was SENT; the extraction is refused if the clipboard matches it (prompt echo)")

    verify_composer = commands.add_parser("verify-composer")
    _add_display(verify_composer)
    verify_composer.add_argument("--file", required=True, help="absolute path of the artifact the composer must hold, exactly")
    verify_composer.add_argument("--element", default="input", help="composer element key in the platform YAML (default: input)")
    verify_composer.add_argument("--max-depth", type=int, default=20)

    verify_element = commands.add_parser("verify")
    _add_display(verify_element)
    verify_element.add_argument("--element", required=True, help="platform-YAML element key to check for on screen")
    verify_element.add_argument("--expect", choices=("present", "absent"), default="present")
    verify_element.add_argument("--max-depth", type=int, default=20)

    read_clipboard = commands.add_parser("read-clipboard")
    _add_display(read_clipboard)

    navigate = commands.add_parser("navigate")
    _add_display(navigate)
    navigate.add_argument("--url", required=True)

    return parser


# Action ops mutate the display and must HOLD the per-display lock; observe/read-clipboard are
# reads that only RENEW the lease when we already own it — a non-owner read never locks or renews.
_LOCK_ACTION_OPS = {"click", "focus", "activate", "type", "paste", "key", "navigate", "focus-dialog"}


def _renew_if_owner(display: str, ttl: int) -> bool:
    """Atomically extend the lease IFF we own it (WATCH/MULTI, mirroring release_display_lock).
    Never renews another owner's lock, never raises out — fail-open."""
    if not _LOCK_AVAILABLE:
        return False
    try:
        client = _lock_redis_client()
        key = _plan_lock_key(display)
        while True:
            with client.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    if not raw:
                        pipe.unwatch()
                        return False
                    try:
                        record = json.loads(raw)
                    except Exception:
                        record = {}
                    if record.get("owner_token") != LOCK_OWNER:
                        pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.expire(key, ttl)
                    pipe.execute()
                    return True
                except _LockWatchError:
                    continue
    except Exception as exc:  # fail-open
        sys.stderr.write(f"[ui_drive] lock renew failed on {display} ({exc}); ignoring\n")
        return False


def _guard_action(display: str, ttl: int) -> None:
    """Acquire-or-renew-or-REFUSE before an action op. Refusal raises UiDriveError (surfaces as
    ok:false). FAIL-OPEN: any lock error logs to stderr and proceeds — the lock never blocks driving."""
    if not _LOCK_AVAILABLE:
        return
    try:
        token = _acquire_display_lock(
            payload={"owner_token": LOCK_OWNER}, ttl=ttl, display=display
        )
    except Exception as exc:  # Redis unreachable etc. -> fail-open
        sys.stderr.write(f"[ui_drive] lock acquire failed on {display} ({exc}); proceeding\n")
        return
    if token is not None:
        return  # freshly acquired -> we hold it
    # Held. Ours -> renew and proceed; a DIFFERENT owner -> refuse.
    try:
        record = _display_lock_record(display) or {}
    except Exception as exc:  # fail-open on read error
        sys.stderr.write(f"[ui_drive] lock record read failed on {display} ({exc}); proceeding\n")
        return
    owner = record.get("owner_token")
    if owner == LOCK_OWNER:
        _renew_if_owner(display, ttl)
        return
    raise UiDriveError(
        f"display {display} is held by another driver ({owner or 'unknown'}); not acting "
        f"(observe is still free)"
    )


def _dispatch(args: argparse.Namespace, deps: SimpleNamespace) -> Any:
    if args.action == "observe":
        _renew_if_owner(deps.display, LOCK_TTL_DEFAULT)  # owner-only; a bystander read never locks
        return _observe(args, deps)
    if args.action == "read-clipboard":
        _renew_if_owner(deps.display, LOCK_TTL_DEFAULT)
        return _read_clipboard(deps)
    if args.action == "verify":
        _renew_if_owner(deps.display, LOCK_TTL_DEFAULT)  # a read, like observe — never guarded
        return _verify_element(args, deps)
    if args.action == "verify-composer":
        _renew_if_owner(deps.display, LOCK_TTL_DEFAULT)
        return _verify_composer(args, deps)
    if args.action == "extract":
        _guard_action(deps.display, LOCK_TTL_DEFAULT)   # clicks a control: a real action
        return _extract_response(args, deps)
    if args.action == "scroll":
        _guard_action(deps.display, LOCK_TTL_DEFAULT)
        return _scroll_document(args, deps)
    if args.action in _LOCK_ACTION_OPS:
        _guard_action(deps.display, LOCK_TTL_DEFAULT)
    if args.action in {"click", "focus", "activate"}:
        return _element_action(args.action, args, deps)
    if args.action == "type":
        return _type_text(args, deps)
    if args.action == "paste":
        return _paste(args, deps)
    if args.action == "key":
        return _key(args, deps)
    if args.action == "navigate":
        return _navigate(args, deps)
    if args.action == "focus-dialog":
        return _focus_dialog(args, deps)
    if args.action == "attach-grammar":
        return _attach_grammar(deps.display)
    if args.action == "verify-attachment":
        return _verify_attachment(args, deps)
    raise UiDriveError(f"unsupported action: {args.action}")


def main(argv: list[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    action = actual_argv[0] if actual_argv else "parse"
    display = _requested_option(actual_argv, "--display")
    try:
        args = _parser().parse_args(actual_argv)
        action = args.action
        display = args.display
        deps = _configure_display(display)
        display = deps.display
        result = _dispatch(args, deps)
        _emit(ok=True, action=action, display=display, result=result, error=None)
        return 0
    except Exception as exc:
        _emit(ok=False, action=action, display=display, result=None, error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
