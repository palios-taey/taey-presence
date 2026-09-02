#!/usr/bin/env python3
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_APP = ROOT / "dashboard" / "app.py"
HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put"}
ROUTE_PARAMETER = re.compile(r"\{([^{}]+)\}")
SESSION_GLOBAL_PREFIXES = ("_session", "chat_session", "TAEY_SESSION")


def _normalized_fastapi_route_shape(path: str) -> str:
    def replace_parameter(match: re.Match[str]) -> str:
        _, separator, converter = match.group(1).partition(":")
        return "{:" + (converter if separator else "str") + "}"

    return ROUTE_PARAMETER.sub(replace_parameter, path)


def _top_level_bound_names(node: ast.AST) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [target.id for target in targets if isinstance(target, ast.Name)]
    return []


def validate_dashboard_route_uniqueness() -> None:
    tree = ast.parse(
        DASHBOARD_APP.read_text(encoding="utf-8"),
        filename=str(DASHBOARD_APP),
    )
    routes: dict[tuple[str, str], list[str]] = defaultdict(list)
    session_globals: dict[str, list[int]] = defaultdict(list)

    for node in tree.body:
        for name in _top_level_bound_names(node):
            if name.startswith(SESSION_GLOBAL_PREFIXES):
                session_globals[name].append(node.lineno)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                not isinstance(decorator, ast.Call)
                or not isinstance(decorator.func, ast.Attribute)
                or not isinstance(decorator.func.value, ast.Name)
                or decorator.func.value.id != "app"
                or decorator.func.attr not in HTTP_METHODS
            ):
                continue
            if not decorator.args:
                raise RuntimeError(
                    f"{DASHBOARD_APP}:{node.lineno}: route has no literal path"
                )
            try:
                path = ast.literal_eval(decorator.args[0])
            except (ValueError, TypeError, SyntaxError) as exc:
                raise RuntimeError(
                    f"{DASHBOARD_APP}:{node.lineno}: route path is not static"
                ) from exc
            if not isinstance(path, str):
                raise RuntimeError(
                    f"{DASHBOARD_APP}:{node.lineno}: route path is not a string"
                )
            key = (
                decorator.func.attr.upper(),
                _normalized_fastapi_route_shape(path),
            )
            routes[key].append(f"{node.name}:{node.lineno}:{path}")

    duplicate_routes = {
        key: values for key, values in routes.items() if len(values) != 1
    }
    duplicate_globals = {
        name: lines for name, lines in session_globals.items() if len(lines) != 1
    }
    if duplicate_routes or duplicate_globals:
        details = []
        for key, values in sorted(duplicate_routes.items()):
            details.append(f"duplicate route {key}: {', '.join(values)}")
        for name, lines in sorted(duplicate_globals.items()):
            details.append(f"duplicate session global {name}: lines {lines}")
        raise RuntimeError("\n".join(details))

    print(
        "dashboard route uniqueness: PASS "
        f"({len(routes)} method/shape pairs; {len(session_globals)} session globals)"
    )


if __name__ == "__main__":
    validate_dashboard_route_uniqueness()
