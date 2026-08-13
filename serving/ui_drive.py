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


def _resolve_target(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
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


def _element_action(
    action: str, args: argparse.Namespace, deps: SimpleNamespace
) -> dict[str, Any]:
    row = _resolve_target(args, deps)
    primitive = {
        "click": deps.interact.atspi_click,
        "focus": deps.interact.atspi_focus,
        "activate": deps.interact.atspi_activate,
    }[action]
    if not primitive(row):
        raise UiDriveError(f"{action} primitive returned false")
    return {"performed": True, "element": _public_element(row)}


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
