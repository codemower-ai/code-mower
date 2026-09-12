"""Offline scenarios synthesize private inputs in memory; never capture Slack traffic."""
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import sys
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from code_mower.slack_ingress import Ingress, Reservation

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    wall = 2000000000
    tick = 10.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.tick


class Store:
    def __init__(self, events):
        self.events = events
        self.records = {}

    def reserve(self, receipt, *, deadline):
        self.events.append('reserve')
        old = self.records.get(receipt.key)
        if old:
            return (Reservation.DUPLICATE if old.fingerprint == receipt.fingerprint
                    else Reservation.CONFLICT)
        self.records[receipt.key] = receipt
        self.events.append('commit')
        return Reservation.ACCEPTED


class Bindings:
    def __init__(self, events):
        self.events = events
        self.grant = json.loads((ROOT / 'tests/fixtures/slack_contracts.json').read_text())['grant']

    def resolve(self, submission, *, deadline):
        self.events.append('resolve')
        return self.grant


class SlackIngressTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.store = Store(self.events)
        self.bindings = Bindings(self.events)
        # Deterministic nonproduction material, generated rather than captured fixtures.
        self.key = bytes(range(32))
        self.ingress = Ingress(signing_secret=self.key, clock=self.clock,
                               bindings=self.bindings, store=self.store,
                               registered_runners=frozenset({'codex_remote', 'claude_remote'}))
        self.form = {name: 'synthetic_' + name for name in
                     ('api_app_id', 'team_id', 'user_id', 'channel_id', 'trigger_id')}
        self.form.update(command='/code-mower', text='start ' + 'example input')

    def headers(self, body, timestamp=None, content_type='application/x-www-form-urlencoded'):
        timestamp = str(self.clock.wall if timestamp is None else timestamp)
        digest = hmac.new(self.key, b'v0:' + timestamp.encode() + b':' + body, hashlib.sha256).hexdigest()
        return (('Content-Type', content_type), ('X-Slack-Request-Timestamp', timestamp),
                ('X-Slack-Signature', 'v0=' + digest))

    def call(self, body=None, headers=None, **kwargs):
        if body is None:
            body = urlencode(self.form).encode()
        response = self.ingress.handle(method=kwargs.pop('method', 'POST'),
            raw_body=body, headers=headers if headers is not None else self.headers(body),
            deadline=kwargs.pop('deadline', 13.0), **kwargs)
        self.events.append('response')
        return response

    def json_call(self, value):
        body = json.dumps(value).encode()
        return self.call(body, self.headers(body, content_type='application/json'))

    def modal(self, operation='start'):
        return dict(type='view_submission', team={'id': self.form['team_id']},
                    user={'id': self.form['user_id']}, api_app_id=self.form['api_app_id'],
                    view=dict(id='synthetic_view', type='modal', callback_id=operation,
                              hash='synthetic_revision', state={'values': {'input': {
                                  'text': {'type': 'plain_text_input', 'value': 'example input'}}}}))

    def test_receipt_before_ack_and_contract_reuse(self):
        response = self.call()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body), {'response_type': 'ephemeral', 'text': 'Received.'})
        self.assertEqual(self.events, ['resolve', 'reserve', 'commit', 'response'])
        receipt = next(iter(self.store.records.values()))
        self.assertEqual(receipt.request['schema'], 'code_mower.slack_ingress.v1')
        self.assertEqual(receipt.intent['operation'], 'dispatch')
        self.assertEqual(receipt.content_expires_at, self.clock.wall + 3600)
        self.assertEqual(receipt.dedupe_expires_at, self.clock.wall + 172800)
        for private in self.form.values():
            self.assertNotIn(private, repr(receipt))
            self.assertNotIn(private.encode(), response.body)
        self.assertNotIn('Received', repr(response))

    def test_all_operations_and_modals(self):
        for operation in ('start', 'status', 'message', 'clarification_reply', 'cancel'):
            with self.subTest(operation=operation):
                self.setUp()
                self.form['text'] = operation + (' example input' if operation not in {'status', 'cancel'} else '')
                self.assertEqual(self.call().status, 200)
        for operation in ('start', 'clarification_reply'):
            self.setUp()
            response = self.call(urlencode({'payload': json.dumps(self.modal(operation))}).encode())
            self.assertEqual((response.status, response.body), (200, b''))
            self.assertEqual(self.events, ['resolve', 'reserve', 'commit', 'response'])

    def test_raw_bytes_version_timestamp_and_constant_time(self):
        body = urlencode(self.form).encode()
        with patch('code_mower.slack_ingress.hmac.compare_digest', wraps=hmac.compare_digest) as compare:
            self.assertEqual(self.call(body).status, 200)
            compare.assert_called_once()
        self.setUp()
        changed = body.replace(b'+', b'%20')
        self.assertNotEqual(changed, body)
        self.assertEqual(self.call(changed, self.headers(body)).status, 401)
        for timestamp in (self.clock.wall - 301, self.clock.wall + 301, '02000000000', '-1', 'NaN'):
            self.assertEqual(self.call(body, self.headers(body, timestamp)).status, 401)
        for timestamp in (self.clock.wall - 300, self.clock.wall + 300):
            self.assertEqual(self.call(body, self.headers(body, timestamp)).status, 200)
        for signature in ('v1=' + '0' * 64, 'v0=' + '0' * 64, 'v0=bad', 'v0=' + 'A' * 64):
            headers = self.headers(body)[:-1] + (('X-Slack-Signature', signature),)
            self.assertEqual(self.call(body, headers).status, 401)
        headers = list(self.headers(body))
        headers[1] = ('X-Slack-Request-Timestamp', str(self.clock.wall + 1))
        self.assertEqual(self.call(body, tuple(headers)).status, 401)

    def test_replay_retry_conflict_and_retention(self):
        body = urlencode(self.form).encode()
        first = self.call(body)
        receipt = next(iter(self.store.records.values()))
        headers = self.headers(body, self.clock.wall + 1) + (('X-Slack-Retry-Num', '2'),)
        second = self.call(body, headers)
        self.assertEqual(first, second)
        self.assertIs(next(iter(self.store.records.values())), receipt)
        self.assertEqual(self.events.count('commit'), 1)
        self.form['text'] = 'start changed input'
        conflict = self.call()
        self.assertEqual(conflict.status, 409)
        self.assertEqual(json.loads(conflict.body)['code'], 'request_conflict')
        self.assertEqual(len(self.store.records), 1)

    def test_malformed_duplicate_unknown_and_oversized_forms(self):
        base = urlencode(self.form).encode()
        bodies = [b'%', b'text=%ZZ', b'text=%FF', b'\xff', b'broken', b'',
                  base + b'&text=duplicate', base + b'&%74ext=duplicate',
                  base + b'&verified=true', b'x' * 65537,
                  b'&'.join(b'x' + str(n).encode() + b'=1' for n in range(33))]
        for text in ('unknown', 'completion', 'status extra', 'cancel extra', 'start', 'message ', 'start ' + 'x' * 16001):
            bodies.append(urlencode(dict(self.form, text=text)).encode())
        bodies.append(urlencode(dict(self.form, command='/unknown')).encode())
        for body in bodies:
            with self.subTest(case=bodies.index(body)):
                self.assertNotEqual(self.call(body).status, 200)
        self.assertEqual(self.store.records, {})

    def test_headers_method_and_deadline_bounds(self):
        body = urlencode(self.form).encode()
        valid = self.headers(body)
        variants = [(), valid + (('x-slack-signature', valid[-1][1]),),
                    valid + (('unknown', 'value'),), valid * 12,
                    valid + (('x-slack-retry-num', '101'),),
                    valid + (('x-slack-retry-num', '-1'),),
                    valid + (('x-slack-retry-reason', 'unexpected'),),
                    valid + (('x-slack-retry-reason', 'x' * 1025),),
                    valid + (('x-slack-retry-num', '1\r\n'),)]
        for headers in variants:
            self.assertNotEqual(self.call(body, headers).status, 200)
        for deadline in (10.0, 9.0, 14.0, float('nan'), float('inf'), True):
            self.assertNotEqual(self.call(deadline=deadline).status, 200)
        self.assertNotEqual(self.call(method='GET').status, 200)
        self.assertEqual(self.store.records, {})

    def test_url_verification_json_bounds_and_unsupported_events(self):
        challenge = 'synthetic_challenge'
        response = self.json_call({'type': 'url_verification', 'challenge': challenge})
        self.assertEqual(json.loads(response.body), {'challenge': challenge})
        self.assertEqual(self.events, ['reserve', 'commit', 'response'])
        receipt = next(iter(self.store.records.values()))
        self.assertIsNone(receipt.request)
        self.assertNotIn(challenge, repr(receipt))
        self.setUp()
        for body in (b'{', b'NaN', b'\xff', b'{"type":"url_verification","type":"url_verification"}',
                     b'[' * 11 + b']' * 11, b'{}', b'[]', b'{"x":1e999}',
                     json.dumps({'x': ['x'] * 33}).encode(),
                     json.dumps({'x': [['x'] * 32] * 32}).encode()):
            self.assertNotEqual(self.call(body, self.headers(body, content_type='application/json')).status, 200)
        for value in ({'type': 'event_callback', 'event': {'type': 'message'}},
                      {'type': 'url_verification', 'challenge': 'x', 'extra': True},
                      {'type': 'url_verification', 'challenge': 'x' * 257},
                      {'type': 'url_verification', 'challenge': '\ud800'}):
            self.assertNotEqual(self.json_call(value).status, 200)
        self.assertEqual(self.store.records, {})

    def test_modals_reject_unknown_fields_routes_actions_and_kinds(self):
        for mutate in (
            lambda v: v.update(type='block_actions'),
            lambda v: v.update(verified=True),
            lambda v: v['view'].update(callback_id='completion'),
            lambda v: v['view'].update(private_metadata='untrusted route'),
            lambda v: v['view']['state']['values'].update(other={}),
            lambda v: v['view'].update(blocks=[]),
            lambda v: v.update(response_urls=[{}]),
            lambda v: v.update(is_enterprise_install=True),
        ):
            value = self.modal()
            mutate(value)
            self.assertNotEqual(self.call(urlencode({'payload': json.dumps(value)}).encode()).status, 200)
        self.assertEqual(self.store.records, {})

    def test_failure_projection_and_late_commit(self):
        for result in ('accepted', None, True):
            with patch.object(self.store, 'reserve', return_value=result):
                self.assertEqual(self.call().status, 503)
        with patch.object(self.store, 'reserve', side_effect=RuntimeError('private diagnostic')):
            response = self.call()
            self.assertEqual(response.status, 503)
            self.assertNotIn(b'private diagnostic', response.body)
            self.assertEqual(self.json_call({'type': 'url_verification', 'challenge': 'test'}).status, 503)
        with patch.object(self.bindings, 'resolve', side_effect=RuntimeError('private diagnostic')):
            self.assertEqual(self.call().status, 403)
        reserve = self.store.reserve
        def late(receipt, *, deadline):
            self.assertEqual(deadline, 13)
            result = reserve(receipt, deadline=deadline)
            self.clock.tick = deadline
            return result
        with patch.object(self.store, 'reserve', side_effect=late):
            self.assertEqual(self.call().status, 503)
        self.assertEqual(len(self.store.records), 1)
        self.clock.tick = 10
        self.assertEqual(self.call().status, 200)
        self.assertEqual(self.events.count('commit'), 1)

    def test_authentication_precedes_parsing_and_dependencies(self):
        bodies = (
            (urlencode(self.form).encode(), 'application/x-www-form-urlencoded'),
            (urlencode({'payload': json.dumps(self.modal())}).encode(),
             'application/x-www-form-urlencoded'),
            (b'{"type":"url_verification","challenge":"synthetic"}', 'application/json'),
            (b'[' * 2000, 'application/json'),
            (b'payload=%FF', 'application/x-www-form-urlencoded'),
        )
        for body, content_type in bodies:
            for failure in ('invalid_signature', 'stale', 'future'):
                with self.subTest(content_type=content_type, failure=failure):
                    self.setUp()
                    timestamp = self.clock.wall + {'invalid_signature': 0, 'stale': -301,
                                                   'future': 301}[failure]
                    headers = self.headers(body, timestamp, content_type)
                    if failure == 'invalid_signature':
                        headers = headers[:-1] + (('X-Slack-Signature', 'v0=' + '0' * 64),)
                    # Explicit call assertions matter: handle catches decoder exceptions.
                    with (patch('code_mower.slack_ingress._form', side_effect=AssertionError) as form,
                          patch('code_mower.slack_ingress._json', side_effect=AssertionError) as decoder,
                          patch.object(self.bindings, 'resolve', side_effect=AssertionError) as resolve,
                          patch.object(self.store, 'reserve', side_effect=AssertionError) as reserve):
                        response = self.call(body, headers)
                        self.assertEqual(response.status, 401)
                        self.assertEqual(json.loads(response.body)['code'], 'unverified_request')
                        for dependency in (form, decoder, resolve, reserve):
                            dependency.assert_not_called()
                    self.assertEqual(self.events, ['response'])
                    self.assertEqual(self.store.records, {})

    def modal_call(self, value):
        return self.call(urlencode({'payload': json.dumps(value)}).encode())

    def exact_binding(self, *, installed_team=None, enterprise='', view_team=''):
        # A server-held one-time context, independent of submitted payload fields.
        expected = (self.form['api_app_id'], self.form['team_id'],
                    installed_team or self.form['team_id'], enterprise, False,
                    self.form['user_id'], 'synthetic_view', 'start', view_team)
        def resolve(submission, *, deadline):
            self.events.append('resolve')
            actual = (submission.app, submission.team, submission.installed_team,
                      submission.enterprise, submission.is_enterprise_install,
                      submission.actor, submission.correlation, submission.operation,
                      submission.view_team)
            if actual != expected:
                raise ValueError('unresolved binding')
            return self.bindings.grant
        return patch.object(self.bindings, 'resolve', side_effect=resolve)

    def test_workspace_in_grid_metadata_is_ephemeral(self):
        for kind in ('command', 'modal'):
            with self.subTest(kind=kind):
                self.setUp()
                enterprise = 'synthetic_enterprise'
                if kind == 'command':
                    self.form.update(enterprise_id=enterprise, enterprise_name='Synthetic org',
                                     is_enterprise_install='false')
                    body = urlencode(self.form).encode()
                else:
                    value = self.modal()
                    value.update(enterprise={'id': enterprise, 'name': 'Synthetic org'},
                                 is_enterprise_install=False)
                    body = urlencode({'payload': json.dumps(value)}).encode()
                with patch.object(self.bindings, 'resolve', wraps=self.bindings.resolve) as resolve:
                    response = self.call(body)
                    self.assertEqual(response.status, 200)
                    submission = resolve.call_args.args[0]
                    self.assertEqual(submission.enterprise, enterprise)
                    self.assertFalse(submission.is_enterprise_install)
                    self.assertEqual(submission.team, self.form['team_id'])
                    self.assertEqual(submission.installed_team, self.form['team_id'])
                receipt = next(iter(self.store.records.values()))
                persisted = json.dumps([receipt.request, receipt.intent])
                for private in (enterprise, 'Synthetic org', submission.team):
                    self.assertNotIn(private, persisted)
                    self.assertNotIn(private, repr(submission))
                    self.assertNotIn(private.encode(), response.body)

    def test_grid_rejects_org_unresolved_and_unbounded_metadata(self):
        for fields in (
            {'is_enterprise_install': 'true'}, {'is_enterprise_install': 'False'},
            {'team_id': ''}, {'enterprise_id': 'x' * 257},
            {'enterprise_name': 'x' * 257}, {'enterprise_id': '', 'enterprise_name': 'name'},
        ):
            with self.subTest(fields=list(fields)):
                self.setUp()
                form = dict(self.form, enterprise_id='synthetic_enterprise',
                            is_enterprise_install='false')
                form.update(fields)
                self.assertEqual(self.call(urlencode(form).encode()).status, 400)
                self.assertEqual(self.events, ['response'])
        for mutate in (
            lambda v: v.update(is_enterprise_install=True),
            lambda v: v.update(is_enterprise_install='false'),
            lambda v: v.update(team=None),
            lambda v: v['team'].update(id=''),
            lambda v: v.update(enterprise={}),
            lambda v: v.update(enterprise={'id': 'x' * 257}),
            lambda v: v.update(enterprise={'id': 'synthetic', 'name': []}),
            lambda v: v.update(enterprise={'id': 'synthetic', 'unknown': True}),
        ):
            self.setUp()
            value = self.modal()
            value.update(enterprise={'id': 'synthetic_enterprise'}, is_enterprise_install=False)
            mutate(value)
            self.assertEqual(self.modal_call(value).status, 400)
            self.assertEqual(self.events, ['response'])

    def test_modal_exact_install_and_slack_connect_correlation(self):
        for connected in (False, True):
            for include_view_team in (False, True):
                self.setUp()
                installed = 'synthetic_installed_team' if connected else self.form['team_id']
                value = self.modal()
                value.update(enterprise={'id': 'synthetic_enterprise'}, is_enterprise_install=False)
                value['view']['app_installed_team_id'] = installed
                view_team = installed if include_view_team else ''
                if include_view_team:
                    value['view']['team_id'] = view_team
                with self.exact_binding(installed_team=installed, view_team=view_team,
                                        enterprise='synthetic_enterprise'):
                    self.assertEqual(self.modal_call(value).status, 200)
                    self.assertEqual(self.modal_call(value).status, 200)
                    if not connected:
                        del value['view']['app_installed_team_id']
                        self.assertEqual(self.modal_call(value).status, 200)
                self.assertEqual(self.events.count('commit'), 1)
                receipt = next(iter(self.store.records.values()))
                self.assertNotIn(installed, json.dumps([receipt.request, receipt.intent]))

    def test_modal_cross_install_and_unknown_correlation_denied(self):
        for mutate in (
            lambda v: v['view'].update(app_installed_team_id='synthetic_other_install'),
            lambda v: v['view'].pop('app_installed_team_id'),
            lambda v: v['team'].update(id='synthetic_other_action_team'),
            lambda v: v.update(api_app_id='synthetic_other_app'),
            lambda v: v['user'].update(id='synthetic_other_actor'),
            lambda v: v['view'].update(id='synthetic_unknown_view'),
            lambda v: v['view'].update(callback_id='clarification_reply'),
            lambda v: v.update(enterprise={'id': 'synthetic_other_enterprise'}),
        ):
            self.setUp()
            value = self.modal()
            value['view']['app_installed_team_id'] = 'synthetic_installed_team'
            mutate(value)
            with self.exact_binding(installed_team='synthetic_installed_team'):
                self.assertEqual(self.modal_call(value).status, 403)
            self.assertEqual(self.events, ['resolve', 'response'])
            self.assertEqual(self.store.records, {})
        for installed in (None, '', [], True, 'x' * 257):
            self.setUp()
            value = self.modal()
            value['view']['app_installed_team_id'] = installed
            self.assertEqual(self.modal_call(value).status, 400)
            self.assertEqual(self.events, ['response'])
        self.setUp()
        value = self.modal()
        value['view'].update(app_installed_team_id='synthetic_install', team_id='synthetic_unrelated')
        self.assertEqual(self.modal_call(value).status, 400)
        self.assertEqual(self.events, ['response'])

    def test_modal_optional_mutable_hash_and_content_conflict(self):
        value = self.modal()
        del value['view']['hash']
        with self.exact_binding():
            self.assertEqual(self.modal_call(value).status, 200)
            receipt = next(iter(self.store.records.values()))
            self.assertEqual(self.modal_call(value).status, 200)
            for revision in ('synthetic_revision', 'synthetic_new_revision'):
                value['view']['hash'] = revision
                self.assertEqual(self.modal_call(value).status, 200)
                self.assertIs(next(iter(self.store.records.values())), receipt)
            value['view']['state']['values']['input']['text']['value'] = 'changed input'
            value['view']['hash'] = 'synthetic_changed_revision'
            self.assertEqual(self.modal_call(value).status, 409)
            del value['view']['hash']
            self.assertEqual(self.modal_call(value).status, 409)
        self.assertEqual(self.events.count('commit'), 1)
        self.assertEqual(len(self.store.records), 1)
        self.assertIs(next(iter(self.store.records.values())), receipt)
        for revision in (None, '', [], True, 'x' * 257):
            self.setUp()
            value = self.modal()
            value['view']['hash'] = revision
            self.assertEqual(self.modal_call(value).status, 400)
            self.assertEqual(self.events, ['response'])

    def test_policy_and_registration_denials(self):
        for mutate in (
            lambda g: g.update(active=False),
            lambda g: g.update(operations=['status']),
            lambda g: g['identity'].update(runner='slack'),
            lambda g: g['identity'].update(runner='unregistered'),
        ):
            self.setUp()
            mutate(self.bindings.grant)
            self.assertEqual(self.call().status, 403)
            self.assertEqual(self.store.records, {})

    def test_full_modal_and_conflicting_retry(self):
        value = self.modal()
        value['view'].update(
            team_id=value['team']['id'], app_id=value['api_app_id'],
            title={'type': 'plain_text', 'text': 'Request', 'emoji': True},
            submit={'type': 'plain_text', 'text': 'Send'}, close=None,
            private_metadata='', external_id='', previous_view_id=None,
            blocks=[{'type': 'input', 'block_id': 'input',
                     'label': {'type': 'plain_text', 'text': 'Text'},
                     'element': {'type': 'plain_text_input', 'action_id': 'text',
                                 'multiline': True}}])
        body = urlencode({'payload': json.dumps(value)}).encode()
        self.assertEqual(self.call(body).status, 200)
        self.assertEqual(self.call(body).status, 200)
        value['view']['state']['values']['input']['text']['value'] = 'changed input'
        self.assertEqual(self.call(urlencode({'payload': json.dumps(value)}).encode()).status, 409)
        self.assertEqual(self.events.count('commit'), 1)
        value['view']['blocks'][0]['element']['multiline'] = 'true'
        self.assertNotEqual(self.call(urlencode({'payload': json.dumps(value)}).encode()).status, 200)

    def test_manifest_package_and_stdlib_import(self):
        from code_mower.package_manifest import PACKAGE_FILES
        manifest = json.loads((ROOT / 'templates/slack/app-manifest.json').read_text())
        self.assertEqual(manifest['oauth_config'], {'scopes': {'bot': ['commands']}})
        self.assertNotIn('event_subscriptions', manifest['settings'])
        self.assertEqual((ROOT / 'templates/slack/app-manifest.json').read_bytes(),
                         (ROOT / 'src/code_mower/templates/slack/app-manifest.json').read_bytes())
        targets = {target for _, target, _ in PACKAGE_FILES}
        self.assertTrue({'src/code_mower/slack_ingress.py', 'templates/slack/app-manifest.json',
                         'docs/slack-ingress.md'} <= targets)
        subprocess.run([sys.executable, '-I', '-c', "import sys; sys.path.insert(0, 'src'); "
                        "import code_mower.slack_ingress; "
                        "assert not any(n.startswith(('pytest', 'httpx', 'slack_sdk', "
                        "'code_mower.remote_session')) for n in sys.modules)"], cwd=ROOT, check=True)


if __name__ == '__main__':
    unittest.main()
