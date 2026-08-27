#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from types import SimpleNamespace


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
    require(
        '("paste_frozen_text", "draft")' in observe,
        'revenue observe does not expose the declared editor paste transition',
    )
    for required in (
        '(declared_method, declared_effect) == ("observe", "observation")',
        'declared.get("primitives") == []',
        'declared.get("allowed_now") == []',
        'declared_method == "paste_frozen_text"',
        'max_text_chars <= 0',
    ):
        require(required in observe, f'revenue observe missing {required!r}')

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

    paste = function_source(UI_DRIVE, '_revenue_paste')
    for required in (
        '_resolve_revenue_target(args, deps)',
        'declared.get("method") != "paste_frozen_text"',
        'declared.get("effect_class") != "draft"',
        'declared.get("max_text_chars") != consumed_max_text_chars',
        'len(text) > consumed_max_text_chars',
        'deps.input.clipboard_paste(text)',
        'stable_post_action_observation',
        'postcondition.get("element_key") != row["element"]',
        'postcondition.get("editor_text_sha256") != expected_text_sha256',
        'postcondition.get("editor_text_chars") != len(text)',
        '"observe_required_before_next_mutation": True',
    ):
        require(required in paste, f'revenue paste transition missing {required!r}')
    require(
        paste.count('deps.input.clipboard_paste(text)') == 1,
        'revenue paste transition must invoke exactly one clipboard paste primitive',
    )
    class FakeSnapshot:
        pass

    observed_snapshot = FakeSnapshot()
    post_snapshot = FakeSnapshot()
    exact_text = 'Exact private comment\n'
    declared_max_text_chars = len(exact_text)
    exact_text_sha256 = hashlib.sha256(exact_text.encode('utf-8')).hexdigest()
    clipboard_calls: list[str] = []

    def stable_observation(
        element_key: str,
        operation: str,
        _deadline: float,
        *,
        expected_text: str | None = None,
    ):
        require(element_key == 'selected_post_editor', 'paste used the wrong editor')
        require(operation == 'paste_frozen_text', 'paste used the wrong operation')
        require(expected_text == exact_text, 'paste barrier received changed text')
        return post_snapshot, {
            'result': 'PASS',
            'next_mutation_authorized': True,
            'observe_required_before_next_mutation': True,
            'postcondition_receipt': {
                'element_key': element_key,
                'operation': operation,
                'effect_class': 'draft',
                'route_exact': True,
                'activity_exact': True,
                'editor_text_sha256': exact_text_sha256,
                'editor_text_chars': len(exact_text),
            },
        }

    paste_namespace = {
        'LOCK_TTL_DEFAULT': 100,
        'Snapshot': FakeSnapshot,
        'SimpleNamespace': SimpleNamespace,
        'UiDriveError': RuntimeError,
        '_manual_ui_module': lambda _platform: SimpleNamespace(
            stable_post_action_observation=stable_observation,
        ),
        '_resolve_revenue_target': lambda _args, _deps: ({
            'element': 'selected_post_editor',
            'name': 'Text editor for creating comment',
            'role': 'entry',
            'states': ['editable', 'showing'],
            'ref': 'atspi3.fake',
        }, observed_snapshot),
        '_revenue_declared_operation': lambda _platform, _element, _row: {
            'method': 'paste_frozen_text',
            'effect_class': 'draft',
            'primitives': ['paste_frozen_text'],
            'allowed_now': ['paste_frozen_text'],
            'max_text_chars': declared_max_text_chars,
        },
        '_snapshot_revision': lambda snapshot, scope='base': (
            'a' * 64 if snapshot is observed_snapshot else 'b' * 64
        ),
        'hashlib': hashlib,
        're': re,
        'sys': SimpleNamespace(
            stdin=SimpleNamespace(buffer=io.BytesIO(exact_text.encode('utf-8'))),
        ),
        'time': SimpleNamespace(monotonic=lambda: 1.0),
    }
    exec(paste, paste_namespace)
    paste_result = paste_namespace['_revenue_paste'](
        SimpleNamespace(
            text_sha256=exact_text_sha256,
            max_text_chars=declared_max_text_chars,
        ),
        SimpleNamespace(
            platform='linkedin',
            input=SimpleNamespace(
                clipboard_paste=lambda text: clipboard_calls.append(text) is None,
            ),
        ),
    )
    require(
        clipboard_calls == [exact_text]
        and paste_result['text_sha256'] == exact_text_sha256
        and paste_result['performed_primitive'] == 'paste_frozen_text',
        'revenue paste did not execute one exact hash-bound primitive',
    )
    oversized_text = exact_text + 'x'
    paste_namespace['sys'].stdin.buffer = io.BytesIO(oversized_text.encode('utf-8'))
    try:
        paste_namespace['_revenue_paste'](
            SimpleNamespace(
                text_sha256=hashlib.sha256(oversized_text.encode('utf-8')).hexdigest(),
                max_text_chars=declared_max_text_chars,
            ),
            SimpleNamespace(
                platform='linkedin',
                input=SimpleNamespace(
                    clipboard_paste=lambda text: clipboard_calls.append(text) is None,
                ),
            ),
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError('revenue paste accepted text over observed max_text_chars')
    require(
        clipboard_calls == [exact_text],
        'over-limit revenue text reached the clipboard paste side effect',
    )
    parser = function_source(UI_DRIVE, '_parser')
    paste_parser = parser.split(
        'commands.add_parser("ui-paste")',
        1,
    )[1].split('for action in', 1)[0]
    require(
        '--text-sha256' in paste_parser
        and '--max-text-chars' in paste_parser
        and '--text"' not in paste_parser
        and '--text-file' not in paste_parser,
        'ui-paste must accept a hash but no model-supplied text or path',
    )

    proxy = function_source(SOMA_PROXY, '_do_ui_action')
    for required in (
        '{"observe", "scroll_into_view", "activate", "paste"}',
        '"scroll_into_view": "ui-scroll-into-view"',
        '"paste": "ui-paste"',
        'expected_primitive != "scroll_into_view"',
        'expected_primitive != "paste_frozen_text"',
        'private_text_chars > expected_max_text_chars',
        '"canonical_max_text_chars": canonical_max_text_chars',
        '"--max-text-chars", str(expected_max_text_chars)',
        '_resolve_revenue_ui_private_paste(context)',
        'input=paste_input["text_bytes"]',
        '"state": "draft_transition_complete"',
        'postcondition.get("editor_text_sha256") != expected_text_sha256',
        'result.get("consumed_max_text_chars") != expected_max_text_chars',
        '"state": "viewport_transition_complete"',
        'postcondition.get("live_extent_in_viewport") is not True',
    ):
        require(required in proxy, f'proxy scroll binding missing {required!r}')
    soma_source = SOMA_PROXY.read_text(encoding='utf-8')
    ui_action_schema = soma_source.split('"name": "ui_action"', 1)[1].split(
        '"name": "consult_chat"',
        1,
    )[0]
    require(
        '"enum": ["observe", "scroll_into_view", "activate", "paste"]'
        in ui_action_schema
        and '"text": {' not in ui_action_schema
        and '"text_file": {' not in ui_action_schema,
        'revenue ui_action schema must expose paste without model text or path',
    )

    private_resolver_source = function_source(
        SOMA_PROXY,
        '_resolve_revenue_ui_private_paste',
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        private_root = Path(temp_dir) / 'private'
        transaction_parent = private_root / 'transactions' / 'taey-revenue-1'
        transaction_parent.mkdir(parents=True)
        os.chmod(private_root, 0o700)
        os.chmod(private_root / 'transactions', 0o700)
        os.chmod(transaction_parent, 0o700)
        text = 'Exact private comment\n'
        text_sha256 = hashlib.sha256(text.encode('utf-8')).hexdigest()
        transaction = {
            'schema': 'taey_revenue_ui_private_paste_v1',
            'operation': 'paste',
            'seat_id': 'taey-revenue-1',
            'event_id': 'event-001',
            'correlation_id': 'correlation-001',
            'text': text,
            'text_sha256': text_sha256,
        }
        transaction_path = transaction_parent / 'correlation-001.json'
        transaction_path.write_text(
            json.dumps(transaction, separators=(',', ':'), sort_keys=True),
            encoding='utf-8',
        )
        os.chmod(transaction_path, 0o400)
        namespace = {
            'Path': Path,
            'REVENUE_UI_PRIVATE_ROOT': str(private_root),
            '_SEAT_ID_RE': re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}'),
            '_TRACE_ID_RE': re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}'),
            'hashlib': hashlib,
            'os': os,
            're': re,
            'stat': stat,
        }
        exec(private_resolver_source, namespace)
        resolve_private = namespace['_resolve_revenue_ui_private_paste']
        resolved = resolve_private({
            'seat_id': 'taey-revenue-1',
            'event_id': 'event-001',
            'correlation_id': 'correlation-001',
        })
        require(
            resolved['text_bytes'] == text.encode('utf-8')
            and resolved['text_chars'] == len(text)
            and resolved['text_sha256'] == text_sha256,
            'private paste resolver did not return the exact immutable bytes and hash',
        )
        try:
            resolve_private({
                'seat_id': 'taey-revenue-1',
                'event_id': 'wrong-event',
                'correlation_id': 'correlation-001',
            })
        except RuntimeError:
            pass
        else:
            raise AssertionError('private paste resolver accepted the wrong event identity')
        os.chmod(transaction_path, 0o600)
        duplicate_transaction = transaction_path.read_text(encoding='utf-8').replace(
            '"schema":',
            '"schema":"duplicate","schema":',
            1,
        )
        transaction_path.write_text(duplicate_transaction, encoding='utf-8')
        os.chmod(transaction_path, 0o400)
        try:
            resolve_private({
                'seat_id': 'taey-revenue-1',
                'event_id': 'event-001',
                'correlation_id': 'correlation-001',
            })
        except RuntimeError:
            pass
        else:
            raise AssertionError('private paste resolver accepted duplicate JSON keys')

    prompt = SYSTEM_PROMPT.read_text(encoding='utf-8')
    normalized_prompt = ' '.join(prompt.split())
    require(
        'action="scroll_into_view" only for method scroll_into_view' in normalized_prompt
        and 'action="paste" only for method paste_frozen_text' in normalized_prompt
        and 'Never supply or reconstruct text or a file path.' in normalized_prompt
        and 'enforces the fresh YAML-owned max_text_chars before paste' in normalized_prompt
        and 'Observe again before any later action.' in normalized_prompt,
        'Taey revenue prompt does not preserve the separate observed one-action sequence',
    )
    print('revenue UI LinkedIn scroll and private paste contracts: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
