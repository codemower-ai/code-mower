"""Deterministic, no-network/no-Git transport calibration and acceptance controls."""
from dataclasses import replace
import json

import pytest

from code_mower.devin_review import HostedReview, ReviewInput, local_review, normalize
from code_mower.devin_sessions import DevinClient
from code_mower.remote_session import RemoteError, _key


def finding(severity, title, detail):
    return dict(severity=severity, title=title, detail=detail, file='handler.py', line=12)


# Adjudicated synthetic contract controls, not evidence of live model accuracy.
CONTROLS = [
    ('clean-empty', 'PASS', dict(verdict='pass', summary='No defects in the bounded change.', findings=[])),
    ('clean-advisory', 'PASS', dict(verdict='pass', summary='Only an optional readability improvement.',
        findings=[finding('P3', 'Name the retry delay', 'A named constant would clarify the delay.')])),
    ('blocked-auth', 'BLOCKED', dict(verdict='blocked', summary='An authorization check is missing.',
        findings=[finding('P1', 'Check ownership before returning a record',
                          'The handler returns another account record without checking ownership.')])),
    ('blocked-null', 'BLOCKED', dict(verdict='pass', summary='The empty input path crashes.',
        findings=[finding('P2', 'Guard the missing record',
                          'An empty lookup returns None and the next line dereferences it.')])),
]


@pytest.fixture
def review():
    head = 'a' * 40
    return ReviewInput('owner/repo', 9, head, 'independent-human',
        dict(revision='b' * 32, head=head, required=True, state='available',
             expires_at='2099-01-01T00:00:00Z'), ('handler.py',))


def hosted(tmp_path, review, current, output):
    state = {'status': 'running'}
    calls = []
    def runner(method, url, body, headers):
        calls.append((method, body))
        return dict(session_id='review-1', **state, structured_output=output)
    service = HostedReview.devin(tmp_path / 'state',
        DevinClient('org-example', 'test-key', api_runner=runner), review, current)
    return service, state, calls


@pytest.mark.parametrize('name,expected,output', CONTROLS)
def test_calibration_transport_parity(tmp_path, review, name, expected, output):
    local = local_review(review, lambda: review, lambda: (json.dumps(output), 0))
    remote, state, calls = hosted(tmp_path, review, lambda: review, output)
    assert remote.dispatch('Approved bounded review input')['mode'] == 'dry_run'
    assert not calls and not (tmp_path / 'state').exists()
    remote.dispatch('Approved bounded review input', apply=True)
    assert calls[0][1]['structured_output_required'] is True
    assert calls[0][1]['max_acu_limit'] == 1
    state.update(status='exit', status_detail='finished')
    metadata = remote.collect(apply=True)
    evidence = remote.accept()
    assert evidence.accept(lambda: review) == local.accept(lambda: review)
    assert evidence.result.verdict == expected
    assert not evidence.merge_authority and not local.merge_authority
    assert 'summary' not in json.dumps(metadata) and 'handler.py' not in json.dumps(metadata)
    assert len([c for c in calls if c[0] == 'POST']) == 1


@pytest.mark.parametrize('change', ['head', 'context', 'author', 'missing-author', 'expired', 'unavailable'])
def test_stale_inputs_before_after_and_at_consumption(tmp_path, review, change):
    if change == 'head':
        changed = replace(review, head='c' * 40)
    elif change == 'author':
        changed = replace(review, author='devin-ai-integration[bot]')
    elif change == 'missing-author':
        changed = replace(review, author='')
    else:
        update = {'revision': 'c' * 32} if change == 'context' else (
            {'expires_at': '2000-01-01T00:00:00Z'} if change == 'expired' else {'state': 'required_unavailable'})
        changed = replace(review, context={**review.context, **update})
    current = [review]
    output = CONTROLS[0][2]
    local = local_review(review, lambda: current[0], lambda: (json.dumps(output), 0))
    remote, state, calls = hosted(tmp_path, review, lambda: current[0], output)
    remote.dispatch('approved', apply=True)
    state.update(status='exit', status_detail='finished')
    remote.collect(apply=True)
    saved = remote.accept()
    current[0] = changed
    for action in (lambda: local.accept(lambda: current[0]), remote.accept,
                   lambda: saved.accept(lambda: current[0]), lambda: remote.collect(apply=True),
                   lambda: remote.dispatch('approved', apply=True)):
        with pytest.raises(RemoteError, match='review_binding_mismatch'):
            action()
    count = len(calls)
    with pytest.raises(RemoteError):
        local_review(review, lambda: current[0], lambda: pytest.fail('must not execute'))
    assert len(calls) == count
    current[0] = review
    def run():
        current[0] = changed
        return json.dumps(output), 0
    with pytest.raises(RemoteError):
        local_review(review, lambda: current[0], run)


@pytest.mark.parametrize('output', [None, {}, ' ', '{"verdict":"pass","verdict":"blocked"}',
    '```json\n{}\n```\n```json\n{}\n```',
    dict(verdict='blocked', summary='Missing findings.', findings=[]),
    dict(verdict='pass', summary=123, findings=[]),
    dict(verdict='pass', summary='Invalid line type.', findings=[{**CONTROLS[2][2]['findings'][0], 'line': True}])])
def test_malformed_or_ambiguous_output(output):
    with pytest.raises(RemoteError, match='invalid_review_output'):
        normalize(output, {'handler.py'})


@pytest.mark.parametrize('status', ['running', 'error', 'suspended', 'exit'])
def test_completed_evidence_is_invalidated_by_remote_state(tmp_path, review, status):
    remote, state, _ = hosted(tmp_path, review, lambda: review, CONTROLS[0][2])
    remote.dispatch('approved', apply=True)
    state.update(status='exit', status_detail='finished')
    remote.collect(apply=True)
    evidence = remote.accept()
    state.clear()
    state.update(status=status)
    with pytest.raises(RemoteError):
        evidence.accept(lambda: review)
    assert remote.remote.private_result(remote.session) is None


def test_pending_delivery_never_exposes_collected_result(tmp_path, review):
    remote, state, _ = hosted(tmp_path, review, lambda: review, CONTROLS[0][2])
    remote.dispatch('approved', apply=True)
    state.update(status='exit', status_detail='finished')
    remote.collect(apply=True)
    with remote.remote.store.locked(_key(remote.session)) as locked:
        record = locked.read()
        record['operations']['cancel:request'] = {'state': 'pending', 'fingerprint': 'private'}
        locked.write(record)
    assert remote.remote.private_result(remote.session) is None
    with pytest.raises(RemoteError):
        remote.accept()


def test_context_changes_during_remote_completion(tmp_path, review):
    current = [review]
    remote, state, _ = hosted(tmp_path, review, lambda: current[0], CONTROLS[0][2])
    remote.dispatch('approved', apply=True)
    state.update(status='exit', status_detail='finished')
    original = remote.remote.provider.get
    def get(binding):
        result = original(binding)
        current[0] = replace(review, context={**review.context, 'revision': 'c' * 32})
        return result
    remote.remote.provider.get = get
    with pytest.raises(RemoteError, match='review_binding_mismatch'):
        remote.collect(apply=True)
    with pytest.raises(RemoteError):
        remote.accept()


def test_unknown_delivery_does_not_collect(tmp_path, review):
    remote, state, _ = hosted(tmp_path, review, lambda: review, CONTROLS[0][2])
    remote.dispatch('approved', apply=True)
    state.update(status='exit', status_detail='finished')
    with remote.remote.store.locked(_key(remote.session)) as locked:
        record = locked.read()
        record['operations']['message:request'] = {'state': 'pending', 'fingerprint': 'private'}
        locked.write(record)
    assert remote.collect(apply=True)['state'] == 'uncertain'
    assert remote.remote.private_result(remote.session) is None


@pytest.mark.parametrize('output', [None, {}, {'verdict': 'pass'}])
def test_hosted_missing_or_malformed_completion(tmp_path, review, output):
    remote, state, _ = hosted(tmp_path, review, lambda: review, output)
    remote.dispatch('approved', apply=True)
    state.update(status='exit', status_detail='finished')
    remote.collect(apply=True)
    with pytest.raises(RemoteError):
        remote.accept()


def test_failed_refresh_revokes_cached_completion(tmp_path, review):
    remote, state, _ = hosted(tmp_path, review, lambda: review, CONTROLS[0][2])
    remote.dispatch('approved', apply=True)
    state.update(status='exit', status_detail='finished')
    remote.collect(apply=True)
    evidence = remote.accept()
    def unavailable(binding):
        raise RuntimeError('PRIVATE_PROVIDER_CANARY')
    remote.remote.provider.get = unavailable
    with pytest.raises(RemoteError) as error:
        evidence.accept(lambda: review)
    assert 'PRIVATE_PROVIDER_CANARY' not in str(error.value)
    assert remote.remote.private_result(remote.session) is None


def test_noncomplete_then_complete_requires_new_collection(tmp_path, review):
    remote, state, _ = hosted(tmp_path, review, lambda: review, CONTROLS[0][2])
    remote.dispatch('approved', apply=True)
    state.update(status='exit', status_detail='finished')
    remote.collect(apply=True)
    state.clear()
    state.update(status='running')
    with pytest.raises(RemoteError):
        remote.accept()
    state.update(status='exit', status_detail='finished')
    with pytest.raises(RemoteError):
        remote.accept()
    remote.collect(apply=True)
    assert remote.accept().accept(lambda: review).verdict == 'PASS'


def test_resumed_session_cannot_repurpose_review_binding(tmp_path, review):
    remote, state, _ = hosted(tmp_path, review, lambda: review, CONTROLS[0][2])
    remote.dispatch('approved', apply=True)
    state.update(status='exit', status_detail='finished')
    remote.collect(apply=True)
    remote.remote.run('message', remote.session, request='followup', prose='Changed task', apply=True)
    remote.collect(apply=True)
    with pytest.raises(RemoteError, match='review_not_ready'):
        remote.accept()
