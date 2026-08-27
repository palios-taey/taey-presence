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

import revenue_ui_contract as contract

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
    require('value.get("operation") != card.get("method")' in function_source(REPO_ROOT / 'serving/revenue_ui_contract.py', 'parse_semantic_input'), 'semantic envelope operation is not bound to card method')
    observe = function_source(UI_DRIVE, '_observe_revenue')
    require(
        'declared_method not in DECLARED_EFFECTS' in observe,
        'revenue observe does not expose the declared viewport transition',
    )
    require(
        'public_item["operation_card"] = operation_card(' in observe,
        'revenue observe does not expose the declared editor paste transition',
    )
    for required in (
        '== ("observe", "observation")',
        'declared.get("primitives") == []',
        'declared.get("allowed_now") == []',
        'unavailable_declared',
        'operation_card(',
    ):
        require(required in observe, f'revenue observe missing {required!r}')

    class FakeObserveSnapshot:
        mapped = {
            'disabled_notification': [{'name': 'Unavailable notification'}],
            'selected_comment_submit': [{'name': 'Comment'}],
        }
        url = 'https://www.linkedin.com/feed/update/urn:li:activity:1/'
        raw_count = 2

    disabled_declaration = {
        'method': 'activate',
        'effect_class': 'page',
        'primitives': ['activate'],
        'allowed_now': [],
    }
    submit_declaration = {
        'method': 'submit_frozen_comment',
        'effect_class': 'outward',
        'primitives': ['submit_frozen_comment'],
        'allowed_now': ['submit_frozen_comment'],
        'postcondition': {
            'kind': 'exact_same_activity_body_own_comment',
            'activity': '1',
            'body_sha256': 'c' * 64,
        },
        'precondition': {
            'kind': 'exact_same_activity_body_draft',
            'draft_sha256': 'd' * 64,
        },
    }

    def observe_declared(_platform, element, _selected):
        return disabled_declaration if element == 'disabled_notification' else submit_declaration

    observe_namespace = {
        'argparse': SimpleNamespace(Namespace=object),
        'SimpleNamespace': SimpleNamespace,
        'Any': object,
        'DECLARED_EFFECTS': contract.DECLARED_EFFECTS,
        'UiDriveError': RuntimeError,
        '_revenue_snapshot': lambda _deps: FakeObserveSnapshot(),
        '_snapshot_revision': lambda _snapshot, scope='base': 'a' * 64,
        '_revenue_platform_config': lambda _platform: {},
        '_selected_mapped_item': lambda _cfg, _element, items: (items[0], 0),
        '_revenue_declared_operation': observe_declared,
        '_encode_ref': lambda **kwargs: f"atspi3.{kwargs['element']}",
        '_target_fingerprint': lambda _item, match_count: 'b' * 64,
        '_public_element': lambda item, **fields: {**item, **fields},
        'operation_card': contract.operation_card,
    }
    exec(observe, observe_namespace)
    observe_result = observe_namespace['_observe_revenue'](
        SimpleNamespace(), SimpleNamespace(platform='linkedin', display=':3')
    )
    observed_by_key = {item['element']: item for item in observe_result['mapped']}
    require(
        'operation_card' not in observed_by_key['disabled_notification']
        and observed_by_key['disabled_notification']['declared_operation'] == disabled_declaration,
        'disabled mapped declaration received mutation authority or lost observation evidence',
    )
    require(
        observed_by_key['selected_comment_submit']['operation_card']['method']
        == 'submit_frozen_comment',
        'valid comment declaration lost its actionable operation card',
    )
    malformed_disabled = {**disabled_declaration, 'primitives': []}
    observe_namespace['_revenue_declared_operation'] = (
        lambda _platform, _element, _selected: malformed_disabled
    )
    try:
        observe_namespace['_observe_revenue'](
            SimpleNamespace(), SimpleNamespace(platform='linkedin', display=':3')
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError('malformed disabled declaration was silently accepted')

    scroll = function_source(UI_DRIVE, '_revenue_scroll_into_view')
    for required in (
        '_resolve_revenue_target(args, deps)',
        'card["method"] != "scroll_into_view"',
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
        'card["method"] != "paste_frozen_text"',
        'card["max_text_chars"] != consumed_max_text_chars',
        'len(text) > consumed_max_text_chars',
        'deps.input.clipboard_paste(text)',
        'stable_post_action_observation',
        '"postcondition_evidence": postcondition',
        'postcondition_sha256 = canonical_sha256(postcondition)',
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
                'postcondition': 'exact_same_activity_body_editor_text_sha256',
                'route_exact': True,
                'activity_exact': True,
                'activity_sources': ['url'],
                'selected_post_body_sha256': 'c' * 64,
                'editor_text_sha256': exact_text_sha256,
                'editor_text_chars': len(exact_text),
                'observed_url': 'https://www.linkedin.com/feed/update/urn:li:activity:1/',
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
            'postcondition': {'kind': 'exact_same_activity_body_editor_text_sha256', 'activity': '1', 'body_sha256': 'c' * 64},
        },
        'canonical_sha256': contract.canonical_sha256,
        'operation_card': contract.operation_card,
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
    paste_card = contract.operation_card(element='selected_post_editor', ref='atspi3.fake',
        declared=paste_namespace['_revenue_declared_operation'](None, None, None))
    paste_result = paste_namespace['_revenue_paste'](
        SimpleNamespace(
            text_sha256=exact_text_sha256,
            max_text_chars=declared_max_text_chars,
            ref='atspi3.fake', operation_card_sha256=paste_card['card_sha256'],
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
                ref='atspi3.fake', operation_card_sha256=paste_card['card_sha256'],
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
        '"canonical_cards": canonical_cards',
        '"--max-text-chars", str(expected_max_text_chars)',
        '_resolve_revenue_ui_private_comment(context)',
        'private_comment["text_bytes"]',
        'SIDE_EFFECT_UNCERTAIN:',
        'validate_semantic_receipt(',
        'sequence.setdefault("comment_binding"',
        'validate_operation_evidence(',
        'postcondition_sha256=result.get("postcondition_sha256")',
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

    private_reader = function_source(SOMA_PROXY, '_read_revenue_ui_private_json')
    private_resolver_source = function_source(SOMA_PROXY, '_resolve_revenue_ui_private_comment')
    with tempfile.TemporaryDirectory() as temp_dir:
        private_root = Path(temp_dir) / 'private'
        transaction_parent = private_root / 'transactions' / 'taey-revenue-1'
        transaction_parent.mkdir(parents=True)
        os.chmod(private_root, 0o700)
        os.chmod(private_root / 'transactions', 0o700)
        os.chmod(transaction_parent, 0o700)
        text = 'Exact private comment\n'
        text_sha256 = hashlib.sha256(text.encode('utf-8')).hexdigest()
        gate_path = Path(temp_dir) / 'gate.json'
        normalized = hashlib.sha256(text.strip().encode()).hexdigest()
        gate = {'receipt_version': 'linkedin_gate_signoff_v1', 'packet_kind': 'comment', 'kind': 'feed_comment',
                'verdict': 'signoff', 'failing_gate': None, 'claims_traced': True,
                'receipt_path': str(gate_path), 'action_id': 'comment-001', 'source_activity_id': '1',
                'source_artifact_sha256': 'a' * 64, 'text_hash': normalized,
                'content_hash': normalized, 'gates': [{'passed': True}]}
        gate_path.write_text(json.dumps(gate, separators=(',', ':'), sort_keys=True))
        gate_sha256 = hashlib.sha256(gate_path.read_bytes()).hexdigest()
        transaction = {
            'schema': 'taey_revenue_ui_private_comment_v1', 'operation': 'comment', 'platform': 'linkedin', 'display': ':18',
            'seat_id': 'taey-revenue-1',
            'event_id': 'event-001',
            'correlation_id': 'correlation-001',
            'action_id': 'comment-001', 'selected_activity': '1', 'selected_post_body_sha256': 'c' * 64,
            'gate_receipt_path': str(gate_path), 'gate_receipt_sha256': gate_sha256,
            'gate_receipt_version': 'linkedin_gate_signoff_v1', 'gate_receipt_kind': 'feed_comment', 'source_artifact_sha256': 'a' * 64, 'like_authorized': True,
            'expected_author_name': 'Jesse', 'text': text, 'text_sha256': text_sha256,
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
            'json': json,
        }
        exec(private_reader + '\n\n' + private_resolver_source, namespace)
        resolve_private = namespace['_resolve_revenue_ui_private_comment']
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
    evidence_manifest = {**resolved, 'manifest_sha256': resolved['transaction_sha256'],
                         'expected_text': text, 'expected_text_sha256': text_sha256}
    for method, effect in (('paste_frozen_text', 'draft'), ('activate_optional_like', 'outward'), ('submit_frozen_comment', 'outward')):
        declared = {'method': method, 'effect_class': effect, 'primitives': [method], 'allowed_now': [method],
                    'postcondition': {'kind': f'{method}_post', 'activity': '1', 'body_sha256': 'c' * 64}}
        declared.update({'max_text_chars': 1800} if method == 'paste_frozen_text' else
                        {'precondition': {'kind': 'submit_pre', 'draft_sha256': text_sha256}} if method == 'submit_frozen_comment' else {})
        card = contract.operation_card(element='selected_target', ref='atspi3.exact', declared=declared)
        post = {'element_key': 'selected_target', 'operation': method, 'effect_class': effect,
                'postcondition': f'{method}_post', 'route_exact': True, 'activity_exact': True,
                'activity_sources': ['url'], 'selected_post_body_sha256': 'c' * 64,
                'observed_url': 'https://www.linkedin.com/feed/update/urn:li:activity:1/'}
        post.update({'editor_text_sha256': text_sha256, 'editor_text_chars': len(text)} if method == 'paste_frozen_text'
                    else {'reaction_state': 'liked'} if method == 'activate_optional_like'
                    else {'editor_empty': True, 'exact_own_comment_count': 1, 'comment_text_sha256': text_sha256, 'comment_text_chars': len(text)})
        pre = ({'element_key': 'selected_target', 'operation': method, 'effect_class': 'outward',
                'precondition': 'submit_pre', 'route_exact': True, 'activity_exact': True,
                'body_sha256_exact': True, 'draft_sha256': text_sha256, 'draft_chars': len(text),
                'own_comment_control_sha256': 'd' * 64, 'existing_exact_own_comment_count': 0}
               if method == 'submit_frozen_comment' else None)
        pre_sha, post_sha = (contract.canonical_sha256(pre) if pre else None, contract.canonical_sha256(post))
        contract.validate_operation_evidence(card=card, manifest=evidence_manifest, precondition=pre,
            postcondition=post, precondition_sha256=pre_sha, postcondition_sha256=post_sha)
        mutations = [(pre, post, pre_sha, 'e' * 64), (pre, {**post, 'observed_url': 'changed'}, pre_sha, post_sha)]
        mutations += [({**pre, 'own_comment_control_sha256': 'not-sha'}, post, contract.canonical_sha256({**pre, 'own_comment_control_sha256': 'not-sha'}), post_sha)] if pre else []
        for bad_pre, bad_post, bad_pre_sha, bad_post_sha in mutations:
            try:
                contract.validate_operation_evidence(card=card, manifest=evidence_manifest, precondition=bad_pre,
                    postcondition=bad_post, precondition_sha256=bad_pre_sha, postcondition_sha256=bad_post_sha)
            except ValueError:
                continue
            raise AssertionError(f'{method} accepted forged evidence')

    prompt = SYSTEM_PROMPT.read_text(encoding='utf-8')
    normalized_prompt = ' '.join(prompt.split())
    require(
        'action="scroll_into_view" only for method scroll_into_view' in normalized_prompt
        and 'action="paste" only for method paste_frozen_text' in normalized_prompt
        and all(method in normalized_prompt for method in ('activate_optional_like', 'submit_frozen_comment'))
        and 'Never supply or reconstruct text or a file path.' in normalized_prompt
        and 'enforces the fresh YAML-owned max_text_chars before paste' in normalized_prompt
        and 'Observe again before any later action.' in normalized_prompt,
        'Taey revenue prompt does not preserve the separate observed one-action sequence',
    )
    print('revenue UI LinkedIn scroll and private paste contracts: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
