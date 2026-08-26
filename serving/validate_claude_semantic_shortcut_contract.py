#!/usr/bin/env python3
from __future__ import annotations

import inspect
import os
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from serving import soma_proxy, ui_drive  # noqa: E402
from consultation_v2.platforms.claude import manual  # noqa: E402
from consultation_v2.types import ElementRef, Snapshot  # noqa: E402


def _element(
    key: str,
    name: str,
    role: str,
    *,
    states: list[str] | None = None,
) -> ElementRef:
    return ElementRef(
        key=key,
        name=name,
        role=role,
        x=10,
        y=20,
        states=list(states or []),
    )


def _snapshot() -> Snapshot:
    return Snapshot(
        platform='claude',
        url='https://claude.ai/new',
        raw_count=20,
        mapped={
            'input': [
                _element(
                    'input',
                    'Write your prompt to Claude',
                    'entry',
                    states=['focused', 'showing'],
                )
            ],
            'model_selector': [
                _element('model_selector', 'Model: Opus 5 Extra', 'push button')
            ],
            'toggle_menu': [
                _element(
                    'toggle_menu',
                    'Add files, connectors, and more',
                    'push button',
                )
            ],
        },
    )


def _args(token: str) -> SimpleNamespace:
    return SimpleNamespace(
        display=':3',
        native_dialog_revision='',
        expected_revision='a' * 64,
        expected_scope='base',
        expected_key_precondition=token,
        key='ctrl+u',
    )


def main() -> int:
    assert os.environ.get('TAEYS_HANDS_ROOT') == ui_drive.TAEYS_HANDS
    observed = _snapshot()
    token = manual.key_preconditions(observed, scope='base')['ctrl+u']

    optional_drift = _snapshot()
    optional_drift.mapped['settings_button'] = [
        _element('settings_button', 'Settings', 'push button')
    ]
    optional_drift.mapped['toggle_menu'][0].x = 900
    deps = SimpleNamespace(platform='claude')
    with (
        patch.object(ui_drive, '_snapshot', return_value=optional_drift),
        patch.object(
            ui_drive,
            '_snapshot_at_expected_revision',
            side_effect=AssertionError('whole-tree revision path was used'),
        ),
    ):
        assert ui_drive._snapshot_for_key_or_type(_args(token), deps) is optional_drift

    with (
        patch.object(ui_drive, '_snapshot', return_value=optional_drift),
        patch.object(ui_drive, '_xdo_key', return_value=True) as xdo_key,
    ):
        assert ui_drive._key(_args(token), deps)['key'] == 'ctrl+u'
        xdo_key.assert_called_once_with(':3', 'ctrl+u')

    changed = _snapshot()
    changed.mapped['model_selector'][0].name = 'Model: Sonnet 4.6'
    with (
        patch.object(ui_drive, '_snapshot', return_value=changed),
        patch.object(ui_drive, '_xdo_key', return_value=True) as xdo_key,
    ):
        try:
            ui_drive._key(_args(token), deps)
        except ui_drive.UiDriveError as exc:
            assert 'semantic state changed' in str(exc)
        else:
            raise AssertionError('changed semantic state was accepted')
        xdo_key.assert_not_called()

    fallback_snapshot = _snapshot()
    fallback_args = _args('')
    with patch.object(
        ui_drive,
        '_snapshot_at_expected_revision',
        return_value=fallback_snapshot,
    ) as whole_tree:
        assert (
            ui_drive._snapshot_for_key_or_type(fallback_args, deps)
            is fallback_snapshot
        )
        whole_tree.assert_called_once_with(fallback_args, deps)

    for platform in ('chatgpt', 'gemini', 'grok', 'perplexity'):
        platform_deps = SimpleNamespace(platform=platform)
        platform_args = _args('')
        with patch.object(
            ui_drive,
            '_snapshot_at_expected_revision',
            return_value=fallback_snapshot,
        ) as whole_tree:
            assert (
                ui_drive._snapshot_for_key_or_type(platform_args, platform_deps)
                is fallback_snapshot
            )
            whole_tree.assert_called_once_with(platform_args, platform_deps)

    parser = ui_drive._parser()
    parsed = parser.parse_args(
        [
            'key',
            '--display',
            ':3',
            '--expected-revision',
            'a' * 64,
            '--expected-scope',
            'base',
            '--key',
            'ctrl+u',
            '--expected-key-precondition',
            token,
        ]
    )
    assert parsed.expected_key_precondition == token

    proxy_source = inspect.getsource(soma_proxy._do_drive_chat)
    assert '--expected-key-precondition' in proxy_source
    assert 'key_preconditions' in proxy_source
    assert 'consumed_key_precondition_sha256' in proxy_source
    assert 'claude' not in proxy_source.casefold()
    assert 'expected_key_precondition' not in soma_proxy._DRIVE_ACTION_ARGUMENTS['key']
    print('PASS: Presence carries only an opaque platform-manual key precondition')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
