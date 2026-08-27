#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SOMA_PROXY = REPO_ROOT / 'serving/soma_proxy.py'
UI_DRIVE = REPO_ROOT / 'serving/ui_drive.py'
CHAT_SYSTEM = REPO_ROOT / 'serving/TAEY_CHAT_UI_SYSTEM.md'
TARGET_ACTIONS = (
    'activate',
    'click',
    'focus',
    'hover',
    'operate',
    'scroll_to_bottom',
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def module_tree(path: Path) -> tuple[str, ast.Module]:
    source = path.read_text(encoding='utf-8')
    return source, ast.parse(source, filename=str(path))


def function_source(source: str, tree: ast.Module, name: str) -> str:
    candidates = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    require(len(candidates) == 1, f'{name} is not a unique function')
    segment = ast.get_source_segment(source, candidates[0])
    require(segment is not None, f'could not extract {name}')
    return str(segment)


def assignment_value(
    source: str,
    tree: ast.Module,
    name: str,
    namespace: dict[str, Any] | None = None,
) -> Any:
    candidates = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    ]
    require(len(candidates) == 1, f'{name} is not a unique assignment')
    expression = ast.Expression(body=candidates[0].value)
    ast.fix_missing_locations(expression)
    return eval(compile(expression, str(SOMA_PROXY), 'eval'), namespace or {})


class RequestContext:
    value: dict[str, Any] = {}

    @classmethod
    def get(cls) -> dict[str, Any]:
        return cls.value


def turn_context(tool_round: int = 1) -> dict[str, Any]:
    return {
        'seat_id': 'server-binding-seat',
        'process_generation': 'a' * 32,
        'turn_id': 'server-binding-turn',
        'tool_round': tool_round,
        '_ui_sequence': {'observations': {}, 'terminal': None},
        '_tool_profile_state': {'terminal': None},
    }


def observe_payload(mapped: list[dict[str, Any]]) -> SimpleNamespace:
    payload = {
        'ok': True,
        'platform': 'perplexity',
        'result': {
            'current_url': 'https://www.perplexity.ai/search/exact-report',
            'key_preconditions': {},
            'mapped': mapped,
            'scope': 'base',
            'snapshot_revision': 'b' * 64,
            'surface': 'browser',
        },
    }
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(payload),
        stderr='',
    )


def action_payload() -> SimpleNamespace:
    payload = {
        'ok': True,
        'platform': 'perplexity',
        'result': {
            'performed': True,
            'performed_primitive': 'click',
        },
    }
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(payload),
        stderr='',
    )


def nested_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for child in value.values()
            for key in nested_keys(child)
        }
    if isinstance(value, list):
        return {
            key
            for child in value
            for key in nested_keys(child)
        }
    return set()


def main() -> int:
    source, tree = module_tree(SOMA_PROXY)
    tools = assignment_value(source, tree, 'TOOLS')
    drive_tools = [
        tool
        for tool in tools
        if tool.get('function', {}).get('name') == 'drive_chat'
    ]
    require(len(drive_tools) == 1, 'drive_chat tool schema is not unique')
    properties = drive_tools[0]['function']['parameters']['properties']
    require('element' in properties, 'drive_chat schema lost readable element targeting')
    require('ref' not in properties, 'drive_chat schema still exposes opaque ref transcription')

    action_arguments = assignment_value(
        source,
        tree,
        '_DRIVE_ACTION_ARGUMENTS',
        {'frozenset': frozenset},
    )
    for action in TARGET_ACTIONS:
        require(
            action_arguments[action] == frozenset({'action', 'display', 'element'}),
            f'{action} still accepts a model-supplied ref or lost element targeting',
        )

    drive_source = function_source(source, tree, '_do_drive_chat')
    for required in (
        'observed.get("canonical_refs")',
        'canonical_refs.get(element)',
        'cmd += ["--ref", ref]',
        'item.pop("ref", None)',
    ):
        require(required in drive_source, f'drive_chat server binding lost {required!r}')
    require(
        'arguments.get("ref")' not in drive_source,
        'drive_chat still reads an opaque ref from model arguments',
    )

    ui_source, ui_tree = module_tree(UI_DRIVE)
    resolve_source = function_source(ui_source, ui_tree, '_resolve_target')
    for required in (
        '_decode_ref(args.ref)',
        'descriptor["display"] != deps.display',
        'descriptor["platform"] != deps.platform',
        'descriptor["url"] != snapshot.url',
        'descriptor["target_sha256"] != target_sha256',
    ):
        require(required in resolve_source, f'downstream stale/ref validation lost {required!r}')

    namespace: dict[str, Any] = {
        'CHAT_DISPLAYS': (':6',),
        'UI_DRIVE_PYTHON': '/public/python',
        'UI_DRIVE_SCRIPT': '/public/ui_drive.py',
        '_DRIVE_ACTIONS': frozenset(action_arguments),
        '_DRIVE_ACTION_ARGUMENTS': action_arguments,
        '_DRIVE_GENERATION_FENCE_KEY': 'generation-fence',
        '_DRIVE_MUTATIONS': frozenset({
            'activate', 'click', 'focus', 'focus_dialog', 'hover', 'key', 'navigate',
            'operate', 'paste', 'scroll_to_bottom', 'type',
        }),
        '_PASTE_INLINE_MAX_CHARS': 800,
        '_SEAT_ID_RE': re.compile(r'^[a-z][a-z0-9-]+$'),
        '_TRACE_ID_RE': re.compile(r'^[a-z][a-z0-9-]+$'),
        '_request_context': RequestContext,
        '_audit': lambda *_args, **_kwargs: None,
        '_monitor_touch': lambda *_args, **_kwargs: None,
        'os': os,
        're': re,
    }
    exec(drive_source, namespace)
    drive_chat = namespace['_do_drive_chat']
    canonical_ref = 'atspi3.' + ('c' * 80)
    mapped = [{
        'element': 'artifact_report_entry',
        'match_count': 1,
        'name': 'Exact report',
        'ref': canonical_ref,
        'role': 'push button',
        'states': ['enabled', 'showing'],
    }]
    commands: list[list[str]] = []

    def successful_run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        commands.append(command)
        return observe_payload(mapped) if command[2] == 'observe' else action_payload()

    RequestContext.value = turn_context()
    with patch('subprocess.run', side_effect=successful_run):
        observed = json.loads(drive_chat({
            'action': 'observe',
            'display': ':6',
            'scope': 'base',
        }))
        require(observed['ok'] is True, 'fresh observe did not succeed')
        require(
            'ref' not in nested_keys(observed['result']),
            'model-facing observe still exposes an opaque ref',
        )
        retained = RequestContext.value['_ui_sequence']['observations'][':6']
        require(
            retained['canonical_refs'] == {'artifact_report_entry': canonical_ref},
            'Presence did not retain the unique canonical ref server-side',
        )
        RequestContext.value['tool_round'] = 2
        acted = json.loads(drive_chat({
            'action': 'click',
            'display': ':6',
            'element': 'artifact_report_entry',
        }))
    require(acted['ok'] is True, 'element-only action did not succeed')
    require(
        commands[1].count('--ref') == 1
        and commands[1][commands[1].index('--ref') + 1] == canonical_ref,
        'Presence did not pass the retained canonical ref to ui_drive exactly once',
    )

    RequestContext.value = turn_context(tool_round=2)
    RequestContext.value['_ui_sequence']['observations'][':6'] = {
        'canonical_refs': {'artifact_report_entry': canonical_ref},
        'key_preconditions': {},
        'snapshot_revision': 'b' * 64,
        'snapshot_scope': 'base',
        'surface': 'browser',
        'tool_round': 1,
    }
    with patch('subprocess.run', side_effect=AssertionError('mutation reached ui_drive')):
        supplied_ref = json.loads(drive_chat({
            'action': 'click',
            'display': ':6',
            'element': 'artifact_report_entry',
            'ref': canonical_ref,
        }))
    require(supplied_ref['ok'] is False, 'model-supplied ref was not refused')
    require(
        "unsupported argument(s) ['ref']" in supplied_ref['error'],
        'model-supplied ref refusal was not exact',
    )

    duplicate_mapped = [
        {**mapped[0], 'ref': 'atspi3.first'},
        {**mapped[0], 'ref': 'atspi3.second'},
    ]
    RequestContext.value = turn_context()
    with patch('subprocess.run', return_value=observe_payload(duplicate_mapped)):
        duplicate_observe = json.loads(drive_chat({
            'action': 'observe',
            'display': ':6',
            'scope': 'base',
        }))
    require(
        'ref' not in nested_keys(duplicate_observe['result']),
        'duplicate observation exposed opaque refs',
    )
    RequestContext.value['tool_round'] = 2
    with patch('subprocess.run', side_effect=AssertionError('ambiguous mutation reached ui_drive')):
        duplicate_action = json.loads(drive_chat({
            'action': 'click',
            'display': ':6',
            'element': 'artifact_report_entry',
        }))
    require(duplicate_action['ok'] is False, 'duplicate canonical target was accepted')
    require(
        'did not map exactly one canonical' in duplicate_action['error'],
        'duplicate canonical target did not fail at the binding boundary',
    )

    prompt = ' '.join(CHAT_SYSTEM.read_text(encoding='utf-8').split())
    require(
        'Never supply or transcribe an opaque ref' in prompt
        and 'Presence binds the element' in prompt,
        'manual-chat system prompt still assigns opaque-ref transcription to Taey',
    )
    print('drive_chat server-side canonical ref binding: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
