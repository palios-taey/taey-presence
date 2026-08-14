#!/home/mira/taeys-env-sys/bin/python
"""One-invocation AT-SPI observation and action CLI for raw X displays."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
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

    from consultation_v2 import clipboard, input as ui_input, interact, snapshot, tree
    from consultation_v2.platforms import routing
    from consultation_v2.runtime import ConsultationRuntime
    from consultation_v2.seat_actions import SeatActions

    ui_input.set_display(display)
    clipboard.set_display(display)
    return SimpleNamespace(
        display=env["DISPLAY"],
        clipboard=clipboard,
        input=ui_input,
        interact=interact,
        routing=routing,
        snapshot=snapshot,
        tree=tree,
        ConsultationRuntime=ConsultationRuntime,
        SeatActions=SeatActions,
    )


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


def _platform_config(display: str) -> dict:
    from consultation_v2.runtime import ConsultationRuntime

    platform = _DISPLAY_PLATFORM.get(display)
    if not platform:
        raise UiDriveError(
            f"no platform mapping for display {display}; "
            f"known: {sorted(_DISPLAY_PLATFORM)}")
    return ConsultationRuntime(platform).cfg


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
    platform = _DISPLAY_PLATFORM.get(deps.display)
    firefox = deps.routing.find_firefox_for_platform(platform) if platform else None
    if firefox is None:
        raise UiDriveError(f"Firefox not found for {platform or deps.display}")
    rows = deps.tree.find_elements(firefox, fence_after=[])
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


def _attach_file(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Attach a file through the platform's own driver, in ONE process.

    Attach is a MENU sequence: open the trigger, pick the upload item, then the
    GTK chooser. Decomposed into one drive_chat call per step it cannot work —
    each call is a separate process, and the menu is gone by the next one. The
    per-platform driver already does the whole sequence internally (e.g.
    gemini/driver.py attach_files: Upload & tools -> menu_snap -> upload item ->
    focus_file_dialog -> path), and drive_chat_adapter.attach exposes it. Call
    that; never re-decompose it here.
    """
    from consultation_v2 import drive_chat_adapter

    platform = _DISPLAY_PLATFORM.get(deps.display)
    if not platform:
        raise UiDriveError(f"no platform is mapped for display {deps.display}")
    path = os.path.abspath(args.path)
    if not os.path.isfile(path):
        raise UiDriveError(f"attach: no such file: {path}")
    return drive_chat_adapter.attach(platform, path)


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
    """Report whether the exact platform-YAML element is on screen."""
    platform = _DISPLAY_PLATFORM.get(deps.display)
    firefox = deps.routing.find_firefox_for_platform(platform) if platform else None
    if firefox is None:
        raise UiDriveError(f"Firefox not found for {platform or deps.display}")
    spec = _element_spec(deps.display, args.element)
    rows = deps.tree.find_elements(firefox, fence_after=[])
    matches = [row for row in rows if deps.snapshot.matches_spec(row, spec)]
    expect = getattr(args, "expect", None) or "present"
    present = bool(matches)
    return {
        "element": args.element,
        "name": spec.get("name"),
        "role": spec.get("role"),
        "present": present,
        "count": len(matches),
        "states": sorted({str(state) for row in matches for state in (row.get("states") or [])}),
        "expected": expect,
        "satisfied": (not present) if expect == "absent" else present,
    }


def _observe(args: argparse.Namespace, deps: SimpleNamespace) -> list[dict[str, Any]]:
    platform = _DISPLAY_PLATFORM.get(deps.display)
    firefox = deps.routing.find_firefox_for_platform(platform) if platform else None
    if firefox is None:
        raise UiDriveError(f"Firefox not found for {platform or deps.display}")
    rows = deps.tree.find_elements(firefox, fence_after=[])
    element_map = ((_platform_config(deps.display).get("tree") or {}).get("element_map") or {})
    # Every row carries `nth`: its occurrence within its own (role, name) group.
    # Without it an element that has no YAML key is UNADDRESSABLE — observe can
    # see it and nothing can act on it. That is not hypothetical: Perplexity's
    # composer is a NAMELESS entry (its YAML says so outright), so role+name
    # alone is ambiguous and there is no key to fall back on. role+name+nth is
    # still exact matching — no substring, no guessing — it just says WHICH of
    # the identical matches you mean.
    occurrences: dict[tuple[str, str], int] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        role = row.get("role") or ""
        name = row.get("name") or ""
        nth = occurrences.get((role, name), 0)
        occurrences[(role, name)] = nth + 1
        out.append({
            "element": next(
                (key for key, spec in element_map.items()
                 if deps.snapshot.matches_spec(row, spec)),
                None,
            ),
            "name": name,
            "role": role,
            "nth": nth,
            "text": row.get("text") or "",
            "states": list(row.get("states") or []),
        })
    return out


def _verify_composer(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Does the composer hold EXACTLY the artifact we intend to send?

    Sending is the one action that leaves this machine, and until now nothing
    compared what is about to be sent against what was supposed to be sent. A
    substituted, partial, or failed paste all look identical from the outside:
    the paste call returns ok either way. This is the check that makes the
    difference visible BEFORE send rather than after.
    """
    platform = _DISPLAY_PLATFORM.get(deps.display)
    firefox = deps.routing.find_firefox_for_platform(platform) if platform else None
    if firefox is None:
        raise UiDriveError(f"Firefox not found for {platform or deps.display}")
    spec = _element_spec(deps.display, args.element or "input")
    rows = deps.tree.find_elements(firefox, fence_after=[])
    matches = [row for row in rows if deps.snapshot.matches_spec(row, spec)]
    if len(matches) != 1:
        raise UiDriveError(
            f"composer element {args.element!r} matched {len(matches)} elements; expected exactly one")
    try:
        # Atspi.Accessible.get_text() takes only self in this binding; the text
        # interface is reached through Atspi.Text with the node as first argument.
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        node = matches[0]["atspi_obj"]
        count = int(Atspi.Text.get_character_count(node))
        actual = (Atspi.Text.get_text(node, 0, count) if count > 0 else "").strip()
    except Exception as exc:
        raise UiDriveError(f"composer text read failed: {exc}") from exc
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


def _walk_all(deps: SimpleNamespace, root: Any) -> list[dict[str, Any]]:
    """Every showing element, INCLUDING ones with no on-screen extents.

    consultation_v2/tree.py:204-210 only admits an element when it has a
    component interface AND non-negative extents. Plenty of React controls
    report no extents — ChatGPT's "Copy response" among them — so they are
    dropped before any caller sees them, and the control looks absent when it is
    simply unmeasured. Extents are needed to POINT at something, not for it to
    exist or to accept an AT-SPI action.

    So: match against everything, and require coordinates only when a pointer
    click is actually the thing being attempted.
    """
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    out: list[dict[str, Any]] = []

    def visit(node: Any, depth: int) -> None:
        if depth > 60:
            return
        try:
            states = node.get_state_set()
            if not (states.contains(Atspi.StateType.SHOWING)
                    or states.contains(Atspi.StateType.VISIBLE)):
                return
            x = y = None
            try:
                comp = node.get_component_iface()
                if comp:
                    rect = comp.get_extents(Atspi.CoordType.SCREEN)
                    if rect and rect.x >= 0 and rect.y >= 0:
                        x = rect.x + (rect.width // 2 if rect.width else 0)
                        y = rect.y + (rect.height // 2 if rect.height else 0)
            except Exception:
                pass
            out.append({
                "name": str(node.get_name() or ""),
                "role": str(node.get_role_name() or ""),
                "x": x, "y": y, "atspi_obj": node,
                "states": [s.value_nick for s in (
                    Atspi.StateType.SHOWING, Atspi.StateType.VISIBLE,
                    Atspi.StateType.ENABLED, Atspi.StateType.FOCUSABLE,
                    Atspi.StateType.FOCUSED, Atspi.StateType.EDITABLE,
                    Atspi.StateType.SENSITIVE) if states.contains(s)],
            })
            for i in range(node.get_child_count()):
                child = node.get_child_at_index(i)
                if child is not None:
                    visit(child, depth + 1)
        except Exception:
            return

    visit(root, 0)
    return out


def _row_by_role_name_nth(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Resolve a target by EXACT role + name + occurrence.

    For elements the platform YAML does not key — and for the ones it keys
    structurally, which a per-element matcher cannot resolve — this is the only
    way to act at all. Name is matched exactly, including the empty string.
    """
    platform = _DISPLAY_PLATFORM.get(deps.display)
    firefox = deps.routing.find_firefox_for_platform(platform) if platform else None
    if firefox is None:
        raise UiDriveError(f"Firefox not found for {platform or deps.display}")
    want_role = args.role or ""
    want_name = args.name if args.name is not None else ""
    nth = int(args.nth or 0)
    matches = [
        row for row in _walk_all(deps, firefox)
        if (row.get("role") or "") == want_role and (row.get("name") or "") == want_name
    ]
    if not matches:
        raise UiDriveError(
            f"no element with role={want_role!r} name={want_name!r} on {deps.display}")
    if nth >= len(matches):
        raise UiDriveError(
            f"role={want_role!r} name={want_name!r} has {len(matches)} matches; "
            f"occurrence {nth} does not exist")
    return matches[nth]


def _element_action(
    action: str, args: argparse.Namespace, deps: SimpleNamespace
) -> dict[str, Any]:
    platform = _DISPLAY_PLATFORM.get(deps.display)
    firefox = deps.routing.find_firefox_for_platform(platform) if platform else None
    if firefox is None:
        raise UiDriveError(f"Firefox not found for {platform or deps.display}")
    if getattr(args, "element", None):
        spec = _element_spec(deps.display, args.element)
        rows = deps.tree.find_elements(firefox, fence_after=[])
        matches = [row for row in rows if deps.snapshot.matches_spec(row, spec)]
        if len(matches) != 1:
            raise UiDriveError(
                f"element {args.element!r} matched {len(matches)} elements; expected exactly one")
        row = matches[0]
        by_key = True
    elif getattr(args, "role", None) is not None:
        row = _row_by_role_name_nth(args, deps)
        by_key = False
    else:
        raise UiDriveError(
            f"{action} needs a target: --element <YAML key>, or --role/--name/--nth "
            f"for an element the YAML does not key")
    actions = deps.SeatActions(deps.display, deps.ConsultationRuntime(platform))
    if not by_key:
        # Resolved positionally: act on THIS row. Re-finding by name would be
        # ambiguous, and for a nameless element meaningless.
        if action == "focus":
            performed, via = deps.interact.atspi_focus(row), "atspi"
        else:
            performed, via = deps.interact.atspi_click(row), "atspi"
            if not performed and row.get("x") is not None and row.get("y") is not None:
                performed = deps.input.click_at(int(row["x"]), int(row["y"]))
                via = "pointer"
        if not performed:
            raise UiDriveError(
                f"{action} on role={args.role!r} name={args.name!r} nth={args.nth} did not fire")
        return {"performed": True, "via": via,
                "element": {"name": row.get("name") or "", "role": row.get("role") or "",
                            "nth": int(args.nth or 0)}}
    if action == "click":
        performed = actions.click(str(row.get("name") or ""), row.get("role"))
        via = "seat_actions"
    elif action == "activate":
        performed = actions.do(str(row.get("name") or ""), row.get("role"))
        via = "seat_actions"
    else:
        performed = deps.interact.atspi_focus(row)
        via = "atspi"
    if not performed:
        raise UiDriveError(f"{action} failed for element {args.element!r}")
    return {
        "performed": True,
        "via": via,
        "element": {
            "key": args.element,
            "name": row.get("name") or "",
            "role": row.get("role") or "",
            "states": list(row.get("states") or []),
        },
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


def _read_clipboard_to_file(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    """Write the clipboard straight to a file.

    Round-tripping a harvested answer through the model — read it, then hand it
    back for write_file — makes the model REGENERATE every character. That is
    slow, it drifts, and it is the same boundary that produced a fabricated
    packet on 2026-08-13. The tool has the bytes; the tool writes them.
    """
    text = deps.clipboard.read() or ""
    if not text.strip():
        raise UiDriveError("clipboard is empty — nothing to write")
    path = Path(os.path.abspath(args.path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    import hashlib
    return {"path": str(path), "chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "head": text[:120], "tail": text[-120:]}


def _read_clipboard(deps: SimpleNamespace) -> dict[str, Any]:
    platform = _DISPLAY_PLATFORM.get(deps.display)
    if not platform:
        raise UiDriveError(f"no platform is mapped for display {deps.display}")
    text = deps.ConsultationRuntime(platform).read_clipboard()
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
    parser.add_argument(
        "--element",
        help="platform-YAML element key (e.g. attach_trigger, input, send_button). "
             "Preferred. Omit it only for an element the YAML does not key.",
    )
    # For elements the YAML does not key — and the ones it keys STRUCTURALLY,
    # which a per-element matcher cannot resolve — exact role+name+nth is the
    # only way to act. Perplexity's composer is a nameless entry, so --name ""
    # is a legitimate, exact target.
    parser.add_argument("--role", help="exact AT-SPI role, used with --name/--nth")
    parser.add_argument("--name", help="exact accessible name; may be the empty string")
    parser.add_argument("--nth", type=int, default=0,
                        help="which occurrence within that exact role+name group (default 0)")



def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="ui_drive.py")
    commands = parser.add_subparsers(dest="action", required=True)

    observe = commands.add_parser("observe")
    _add_display(observe)

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

    attach_p = commands.add_parser("attach")
    _add_display(attach_p)
    attach_p.add_argument("--path", required=True, help="absolute path of the file to attach")

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

    verify_element = commands.add_parser("verify")
    _add_display(verify_element)
    verify_element.add_argument("--element", required=True, help="platform-YAML element key to check for on screen")
    verify_element.add_argument("--expect", choices=("present", "absent"), default="present")

    read_clipboard = commands.add_parser("read-clipboard")
    _add_display(read_clipboard)
    read_clipboard.add_argument("--path", help="write the clipboard to this file instead of returning it, so the text is never regenerated by a model")

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
        if getattr(args, "path", None):
            return _read_clipboard_to_file(args, deps)
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
    if args.action == "attach":
        _guard_action(deps.display, LOCK_TTL_DEFAULT)
        return _attach_file(args, deps)
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
