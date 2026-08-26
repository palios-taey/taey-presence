#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_DRIVE = REPO_ROOT / 'serving/ui_drive.py'
SOMA_PROXY = REPO_ROOT / 'serving/soma_proxy.py'
SYSTEM_PROMPT = REPO_ROOT / 'serving/TAEY_REVENUE_UI_SYSTEM.md'


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    candidates = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    require(len(candidates) == 1, f'{path.name}:{name} is not unique')
    segment = ast.get_source_segment(source, candidates[0])
    require(segment is not None, f'could not read {path.name}:{name}')
    return str(segment)


def main() -> int:
    observe = function_source(UI_DRIVE, '_observe_revenue')
    require(
        '("scroll_into_view", "viewport")' in observe,
        'revenue observe does not expose the declared viewport transition',
    )

    scroll = function_source(UI_DRIVE, '_revenue_scroll_into_view')
    for required in (
        '_resolve_revenue_target(args, deps)',
        'declared.get("method") != "scroll_into_view"',
        'runtime.scroll_element_into_view(element)',
        'stable_scroll_post_action_observation',
        'postcondition.get("element_key") != row["element"]',
        'postcondition.get("live_extent_in_viewport") is not True',
        '"observe_required_before_next_mutation": True',
    ):
        require(required in scroll, f'revenue scroll transition missing {required!r}')
    require(
        scroll.count('runtime.scroll_element_into_view(element)') == 1,
        'revenue scroll transition must invoke exactly one scroll primitive',
    )

    pointer = function_source(UI_DRIVE, '_mapped_pointer_activate_operation')
    require(
        'scroll_element_into_view' not in pointer,
        'mapped pointer operation must not fold in a scroll',
    )

    proxy = function_source(SOMA_PROXY, '_do_ui_action')
    for required in (
        '{"observe", "scroll_into_view", "activate"}',
        '"scroll_into_view": "ui-scroll-into-view"',
        'expected_primitive != "scroll_into_view"',
        '"state": "viewport_transition_complete"',
        'postcondition.get("live_extent_in_viewport") is not True',
    ):
        require(required in proxy, f'proxy scroll binding missing {required!r}')

    prompt = SYSTEM_PROMPT.read_text(encoding='utf-8')
    require(
        'action="scroll_into_view" only for method scroll_into_view' in prompt
        and 'Observe again before any activation.' in prompt,
        'Taey revenue prompt does not preserve the separate scroll/observe/activate sequence',
    )
    print('revenue UI LinkedIn scroll contract: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
