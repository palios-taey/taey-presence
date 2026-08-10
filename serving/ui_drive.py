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


TAEYS_HANDS = "/home/mira/taeys-hands"
if TAEYS_HANDS not in sys.path:
    sys.path.insert(0, TAEYS_HANDS)

from consultation_v2.platforms_runtime import display_environment


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


def _type_text(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    if not args.text:
        raise UiDriveError("type text must not be empty")
    if not deps.input.focus_firefox():
        raise UiDriveError("focus_firefox returned false")
    if not deps.input.type_text(args.text):
        raise UiDriveError("type_text returned false")
    return {"typed_chars": len(args.text)}


def _paste(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    if not args.text:
        raise UiDriveError("paste text must not be empty")
    if not deps.input.clipboard_paste(args.text):
        raise UiDriveError("clipboard_paste returned false")
    return {"pasted_chars": len(args.text)}


def _key(args: argparse.Namespace, deps: SimpleNamespace) -> dict[str, Any]:
    if not args.key:
        raise UiDriveError("key must not be empty")
    if not deps.input.press_key(args.key):
        raise UiDriveError("press_key returned false")
    return {"key": args.key}


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
    paste.add_argument("--text", required=True)

    key = commands.add_parser("key")
    _add_display(key)
    key.add_argument("--key", required=True)

    read_clipboard = commands.add_parser("read-clipboard")
    _add_display(read_clipboard)

    navigate = commands.add_parser("navigate")
    _add_display(navigate)
    navigate.add_argument("--url", required=True)

    return parser


def _dispatch(args: argparse.Namespace, deps: SimpleNamespace) -> Any:
    if args.action == "observe":
        return _observe(args, deps)
    if args.action in {"click", "focus", "activate"}:
        return _element_action(args.action, args, deps)
    if args.action == "type":
        return _type_text(args, deps)
    if args.action == "paste":
        return _paste(args, deps)
    if args.action == "key":
        return _key(args, deps)
    if args.action == "read-clipboard":
        return _read_clipboard(deps)
    if args.action == "navigate":
        return _navigate(args, deps)
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
