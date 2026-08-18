#!/home/mira/taeys-env-sys/bin/python
"""One-invocation AT-SPI observation and action CLI for raw X displays."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
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
    from consultation_v2.snapshot import build_snapshot
    from consultation_v2.types import ElementRef, Snapshot
    from consultation_v2.yaml_contract import CHAT_PLATFORMS, load_platform_yaml
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
    acquire_display_lock as _acquire_display_lock,
    display_lock_record as _display_lock_record,
    _plan_lock_key as _plan_lock_key,
)
from storage.redis_pool import get_client as _lock_redis_client
from redis.exceptions import WatchError as _LockWatchError


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
_LEASE_OWNER_RE = re.compile(r"taey-drive:[A-Za-z0-9._-]{1,64}:[0-9a-f]{32}")
_TRACE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")


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
    if not any((owner, seat_id, turn_id)) and not required:
        return None
    if not _LEASE_OWNER_RE.fullmatch(owner):
        raise UiDriveError("missing or invalid proxy-issued display lease owner")
    if not _TRACE_ID_RE.fullmatch(seat_id):
        raise UiDriveError("missing or invalid proxy-issued display lease seat")
    if not _TRACE_ID_RE.fullmatch(turn_id):
        raise UiDriveError("missing or invalid proxy-issued display lease turn")
    return SimpleNamespace(owner=owner, seat_id=seat_id, turn_id=turn_id)


def _encode_ref(
    *,
    display: str,
    platform: str,
    revision: str,
    element: str,
) -> str:
    payload = json.dumps(
        {
            "v": 3,
            "display": display,
            "platform": platform,
            "surface": "browser",
            "revision": revision,
            "element": element,
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
    required = {"v", "display", "platform", "surface", "revision", "element"}
    if set(payload) != required or payload.get("v") != 3:
        raise UiDriveError("invalid ref schema")
    if not re.fullmatch(r":\d+", payload.get("display") or ""):
        raise UiDriveError("invalid ref display")
    if payload.get("platform") not in CHAT_PLATFORMS:
        raise UiDriveError("invalid ref platform")
    if payload.get("surface") != "browser":
        raise UiDriveError("invalid ref surface")
    if not re.fullmatch(r"[0-9a-f]{64}", payload.get("revision") or ""):
        raise UiDriveError("invalid ref revision")
    if not isinstance(payload.get("element"), str) or not payload["element"]:
        raise UiDriveError("invalid ref element")
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


def _snapshot(deps: SimpleNamespace) -> Snapshot:
    _firefox, _document, snapshot = build_snapshot(deps.platform)
    if snapshot.platform != deps.platform:
        raise UiDriveError(
            f"snapshot platform {snapshot.platform!r} does not match bound {deps.platform!r}"
        )
    return snapshot


def _snapshot_revision(snapshot: Snapshot) -> str:
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
                    "states": sorted(set(item.states)),
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
    snapshot = _snapshot(deps)
    revision = _snapshot_revision(snapshot)
    if descriptor["revision"] != revision:
        raise UiDriveError(
            "stale browser snapshot ref; observe again and decide from the fresh tree"
        )
    element_key = descriptor["element"]
    matches = list(snapshot.mapped.get(element_key) or [])
    if len(matches) != 1:
        raise UiDriveError(
            f"mapped element {element_key!r} matched {len(matches)} elements on "
            f"{deps.platform} {deps.display}; expected exactly one"
        )
    item = matches[0]
    return {
        **dict(item.raw),
        "element": element_key,
        "ref": _encode_ref(
            display=deps.display,
            platform=deps.platform,
            revision=revision,
            element=element_key,
        ),
    }


def _observe(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    snapshot = _snapshot(deps)
    revision = _snapshot_revision(snapshot)
    cfg = _platform_config(deps.display)
    mapped: list[dict[str, Any]] = []
    for element_key in sorted(snapshot.mapped):
        items = list(snapshot.mapped.get(element_key) or [])
        for item in items:
            ref = None
            if len(items) == 1:
                ref = _encode_ref(
                    display=deps.display,
                    platform=deps.platform,
                    revision=revision,
                    element=element_key,
                )
            mapped.append(
                _public_element(
                    item,
                    category="mapped",
                    element=element_key,
                    match_count=len(items),
                    ref=ref,
                )
            )

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
    primitive = {
        "click": deps.interact.atspi_click,
        "focus": deps.interact.atspi_focus,
        "activate": deps.interact.atspi_activate,
    }[action]
    if not primitive(row):
        raise UiDriveError(f"{action} primitive returned false")
    return {
        "performed": True,
        "element": {
            "category": "mapped",
            "element": row["element"],
            "name": str(row.get("name") or ""),
            "role": str(row.get("role") or ""),
            "states": list(row.get("states") or []),
            "ref": row["ref"],
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
# read-only: it renews only the exact owner and never creates a lease for a bystander.
_LOCK_ACTION_OPS = {
    "click", "focus", "activate", "type", "paste", "key",
    "focus-dialog", "read-clipboard", "extract",
}


def _renew_if_owner(
    display: str,
    lease: SimpleNamespace,
    ttl: int,
) -> bool:
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
                        raise UiDriveError(
                            f"display {display} lease record is not valid JSON"
                        )
                    if not isinstance(record, dict):
                        raise UiDriveError(
                            f"display {display} lease record is not an object"
                        )
                    if record.get("owner_token") != lease.owner:
                        pipe.unwatch()
                        return False
                    record["seat_id"] = lease.seat_id
                    record["last_turn_id"] = lease.turn_id
                    pipe.multi()
                    pipe.set(key, json.dumps(record), ex=ttl)
                    pipe.execute()
                    return True
                except _LockWatchError:
                    continue
    except UiDriveError:
        raise
    except Exception as exc:
        raise UiDriveError(
            f"display {display} lease renewal unavailable; refusing action: {exc}"
        ) from exc


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
        owned = record.get("owner_token") == lease.owner
        if owned and not _renew_if_owner(display, lease, ttl):
            raise UiDriveError(f"display {display} lease disappeared during renewal")
        refreshed = _display_lock_record(display) or {}
        return {
            "state": "owned" if owned else "held_by_other",
            "owned": owned,
            "ttl_seconds": refreshed.get("ttl_seconds", record.get("ttl_seconds")),
            "expires_by_ttl": True,
        }
    except Exception as exc:
        return {
            "state": "unavailable",
            "owned": False,
            "expires_by_ttl": True,
            "error": str(exc),
        }


def _guard_action(
    display: str,
    lease: SimpleNamespace,
    ttl: int,
) -> dict[str, Any]:
    try:
        token = _acquire_display_lock(
            payload={
                "owner_token": lease.owner,
                "seat_id": lease.seat_id,
                "last_turn_id": lease.turn_id,
                "actor_type": "taey-drive_chat",
            },
            ttl=ttl,
            display=display,
        )
    except Exception as exc:
        raise UiDriveError(
            f"display {display} lease acquisition unavailable; refusing action: {exc}"
        ) from exc
    if token is not None:
        return {
            "state": "acquired",
            "owned": True,
            "ttl_seconds": ttl,
            "expires_by_ttl": True,
        }
    try:
        record = _display_lock_record(display) or {}
    except Exception as exc:
        raise UiDriveError(
            f"display {display} lease record unavailable; refusing action: {exc}"
        ) from exc
    owner = record.get("owner_token")
    if owner == lease.owner:
        if not _renew_if_owner(display, lease, ttl):
            raise UiDriveError(
                f"display {display} lease disappeared during renewal; refusing action"
            )
        return {
            "state": "renewed",
            "owned": True,
            "ttl_seconds": ttl,
            "expires_by_ttl": True,
        }
    raise UiDriveError(
        f"display {display} is held by another driver; refusing action "
        f"(observe remains available)"
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
    elif args.action in {"click", "focus", "activate"}:
        result = _element_action(args.action, args, deps)
    elif args.action == "type":
        result = _type_text(args, deps)
    elif args.action == "paste":
        result = _paste(args, deps)
    elif args.action == "key":
        result = _key(args, deps)
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
