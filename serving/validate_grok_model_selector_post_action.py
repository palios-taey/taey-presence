#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_DRIVE = REPO_ROOT / 'serving/ui_drive.py'
FUNCTIONS = (
    '_grok_model_selector_post_action_policy',
    '_grok_model_selector_post_action_observation',
    '_mapped_pointer_activate_operation',
)
ELEMENTS = ('model_auto', 'model_fast', 'model_expert', 'model_heavy')
BLOCKERS = ('grok_bot_dialog', 'grok_bot_dismiss', 'grok_bot_get')


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def function_source(source: str, tree: ast.Module, name: str) -> str:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    require(len(matches) == 1, f'{name} is not a unique function')
    segment = ast.get_source_segment(source, matches[0])
    require(segment is not None, f'could not extract {name}')
    return str(segment)


def config(timeout_ms: int = 8000) -> dict[str, Any]:
    names = {
        'model_auto': 'Auto Chooses Fast or Expert',
        'model_fast': 'Fast Quick responses · Grok 4.5',
        'model_expert': 'Expert Thinks hard · Grok 4.5',
        'model_heavy': 'Heavy Team of Experts · Grok 4.5',
    }
    element_map = {
        element: {
            'name': name,
            'role': 'menu item',
            'scope': 'app_root_snapshot',
        }
        for element, name in names.items()
    }
    element_map.update({
        'model_selector': {'name': 'Model select', 'role': 'push button'},
        'grok_bot_dialog': {'name': 'Meet Grok Bot', 'role': 'dialog'},
        'grok_bot_dismiss': {'name': 'Dismiss', 'role': 'push button'},
        'grok_bot_get': {'name': 'Get Grok Bot', 'role': 'push button'},
    })
    return {
        'tree': {'element_map': element_map},
        'workflow': {
            'selection': {
                'menus': {
                    'model': {
                        'operate': {
                            'trigger': 'model_selector',
                            'scope': 'app_root_snapshot',
                            'open_method': 'mapped_pointer_activate',
                        },
                        'options': {
                            'auto': {'element': 'model_auto'},
                            'fast': {'element': 'model_fast'},
                            'expert': {'element': 'model_expert'},
                            'heavy': {'element': 'model_heavy'},
                        },
                    },
                },
            },
            'model_selector_post_action': {
                'trigger': 'model_selector',
                'scope': 'app_root_snapshot',
                'refresh_policy': 'live_reacquire_no_clear',
                'exact_singletons': list(ELEMENTS),
                'required_states': ['showing', 'focusable', 'enabled'],
                'absent': list(BLOCKERS),
                'stable_cycles': 2,
                'interval_ms': 250,
                'timeout_ms': timeout_ms,
            },
        },
    }


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeSnapshot:
    def __init__(self, mapped: dict[str, list[SimpleNamespace]]) -> None:
        self.platform = 'grok'
        self.url = 'https://grok.com/'
        self.mapped = mapped


def element(key: str, *, complete: bool = True) -> SimpleNamespace:
    names = {
        'model_auto': 'Auto Chooses Fast or Expert',
        'model_fast': 'Fast Quick responses · Grok 4.5',
        'model_expert': 'Expert Thinks hard · Grok 4.5',
        'model_heavy': 'Heavy Team of Experts · Grok 4.5',
        'grok_bot_dialog': 'Meet Grok Bot',
    }
    return SimpleNamespace(
        name=names.get(key, key),
        role='menu item' if key.startswith('model_') else 'dialog',
        states=(
            ['showing', 'focusable', 'enabled']
            if complete
            else ['showing', 'enabled']
        ),
    )


def exact_snapshot(*, duplicate: bool = False, blocker: bool = False) -> FakeSnapshot:
    mapped = {
        key: [element(key), *( [element(key)] if duplicate and key == 'model_heavy' else [])]
        for key in ELEMENTS
    }
    mapped['model_build'] = [element('model_build')]
    if blocker:
        mapped['grok_bot_dialog'] = [element('grok_bot_dialog')]
    return FakeSnapshot(mapped)


class FakeRuntime:
    def __init__(self, snapshots: list[FakeSnapshot]) -> None:
        self.snapshots = list(snapshots)
        self.reads = 0
        self.mutations = 0

    def mapped_pointer_activate(self, _element: object) -> dict[str, Any]:
        self.mutations += 1
        return {'ok': True, 'primitive': 'mapped_pointer_activate'}

    def app_root_snapshot(self) -> FakeSnapshot:
        self.reads += 1
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


def revision(snapshot: FakeSnapshot, *, scope: str = 'base') -> str:
    payload = {
        key: len(items)
        for key, items in sorted(snapshot.mapped.items())
    }
    return hashlib.sha256(
        json.dumps([scope, snapshot.url, payload], sort_keys=True).encode()
    ).hexdigest()


def main() -> int:
    source = UI_DRIVE.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(UI_DRIVE))
    extracted = '\n\n'.join(function_source(source, tree, name) for name in FUNCTIONS)
    fake_time = FakeTime()
    active_runtime: list[FakeRuntime] = []
    namespace: dict[str, Any] = {
        'Any': Any,
        'ConsultationRuntime': lambda _platform: active_runtime[0],
        'ElementRef': SimpleNamespace,
        'Snapshot': FakeSnapshot,
        'SimpleNamespace': SimpleNamespace,
        'UiDriveError': RuntimeError,
        '_snapshot_revision': revision,
        'hashlib': hashlib,
        'json': json,
        'load_platform_yaml': lambda _platform: config(),
        'time': fake_time,
    }
    exec(extracted, namespace)
    policy = namespace['_grok_model_selector_post_action_policy']
    observe = namespace['_grok_model_selector_post_action_observation']
    operate = namespace['_mapped_pointer_activate_operation']

    selector = {
        'element': 'model_selector',
        'name': 'Model select',
        'role': 'push button',
        'states': ['showing', 'focusable', 'enabled'],
        'ref': 'atspi3.selector',
    }
    declared = {
        'primitives': ['mapped_pointer_activate'],
        'allowed_now': ['mapped_pointer_activate'],
    }
    grok = SimpleNamespace(platform='grok', display=':5')
    require(
        policy(selector, SimpleNamespace(platform='perplexity', display=':6')) is None,
        'non-Grok platform entered the Grok policy',
    )
    require(
        policy({**selector, 'element': 'attach_trigger'}, grok) is None,
        'non-selector Grok operation entered the model barrier',
    )

    partial = FakeSnapshot({key: [element(key)] for key in ELEMENTS[:-1]})
    success_runtime = FakeRuntime([partial, exact_snapshot(), exact_snapshot()])
    active_runtime[:] = [success_runtime]
    result = operate(selector, declared, grok)
    barrier = result['post_action_observation']['barrier']
    require(success_runtime.mutations == 1, 'selector mutation was repeated')
    require(success_runtime.reads == 3, 'read-only settling samples changed')
    require(barrier['result'] == 'PASS', 'exact consecutive projection did not pass')
    require(barrier['display'] == ':5', 'barrier lost exact display binding')
    require(barrier['stable_cycles_observed'] == 2, 'stable-cycle evidence drifted')
    require(barrier['next_mutation_authorized'] is True, 'passed barrier did not authorize')
    require(
        all(sample['current_url'] == 'https://grok.com/' for sample in barrier['samples']),
        'sample receipts lost current URL evidence',
    )

    namespace['load_platform_yaml'] = lambda _platform: config(timeout_ms=500)
    for bad_snapshot, label in (
        (exact_snapshot(duplicate=True), 'duplicate'),
        (exact_snapshot(blocker=True), 'blocker'),
    ):
        fake_time.now = 0.0
        bad_runtime = FakeRuntime([bad_snapshot])
        try:
            observe(bad_runtime, selector, grok)
        except RuntimeError as exc:
            message = str(exc)
            require('"result": "TIMEOUT"' in message, f'{label} did not timeout')
            require(
                '"next_mutation_authorized": false' in message,
                f'{label} timeout retained mutation authority',
            )
        else:
            raise AssertionError(f'{label} postcondition incorrectly passed')
        require(bad_runtime.mutations == 0, f'{label} observation mutated the UI')

    non_grok_runtime = FakeRuntime([exact_snapshot()])
    active_runtime[:] = [non_grok_runtime]
    non_grok_result = operate(
        {**selector, 'element': 'other_selector'},
        declared,
        SimpleNamespace(platform='perplexity', display=':6'),
    )
    require(non_grok_runtime.mutations == 1, 'existing non-Grok pointer path did not run once')
    require(non_grok_runtime.reads == 0, 'Grok barrier spilled into another platform')
    require(
        'post_action_observation' not in non_grok_result,
        'Grok receipt spilled into another platform',
    )
    print('grok model-selector post-action barrier: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
